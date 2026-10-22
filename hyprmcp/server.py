"""MCP server for Hyprland, built on Hyprland's Lua API (0.55+).

Every query and action is executed as a Lua chunk via ``hyprctl repl``. Values
coming from the client are never spliced into Lua source as raw text: they are
converted to Lua literals by :func:`lua`, so selectors, titles and commands
cannot break out of their string.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Literal

from mcp.types import ToolAnnotations

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp.exceptions import ToolError

mcp = MCPServer(
    "Hyprland MCP",
    instructions=(
        "Query and control the Hyprland Wayland compositor through its Lua API.\n"
        "Window selectors: 'class:<regex>', 'initialclass:<regex>', 'title:<regex>', "
        "'initialtitle:<regex>', 'tag:<regex>', 'pid:<pid>', 'stableid:<id>', "
        "'address:0x...', 'activewindow', 'floating', 'tiled'. Regexes must match the "
        "whole value, e.g. 'class:.*firefox.*'. Omit the window to target the active window.\n"
        "Workspace selectors: an ID (1, 2), relative ('+1', '-1'), per monitor "
        "('m+1', 'm~3'), including empty ('r+1'), open ('e+1'), 'name:<name>', "
        "'previous', 'empty', 'special' or 'special:<name>'.\n"
        "Monitor selectors: ID, name (e.g. 'DP-1'), 'desc:<description>', "
        "'current', a direction, or relative '+1' / '-1'.\n"
        "Start with get_desktop_overview to see the current layout."
    ),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True)
ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

Toggle = Literal["toggle", "enable", "disable"]
Direction = Literal["left", "right", "up", "down"]
Selector = str | int


class HyprlandError(ToolError):
    """An error whose message is shown to the model."""


# --------------------------------------------------------------------------- #
# Lua plumbing
# --------------------------------------------------------------------------- #

# Serialises Lua values (including HL.* userdata) to JSON. Top-level HL objects
# are expanded fully; HL objects nested inside them are emitted as small
# references so that e.g. a window's workspace doesn't drag in every window.
LUA_PRELUDE = r"""
local __FIELDS = {
  ["HL.Window"] = {"address","stable_id","class","title","initial_class","initial_title",
    "pid","at","size","workspace","monitor","floating","fullscreen","fullscreen_client",
    "fullscreen_handler","pinned","pin_fullscreened","mapped","hidden","visible","active",
    "xwayland","accepts_input","focus_history_id","group","swallowing","tags","content_type",
    "inhibiting_idle","tearing_hint","allowed_over_fullscreen","xdg_tag","xdg_description",
    "layout"},
  ["HL.Workspace"] = {"id","name","config_name","monitor","windows","groups","visible",
    "active","special","is_empty","is_persistent","has_urgent","has_fullscreen",
    "fullscreen_mode","fullscreen_window","last_window","tiled_layout"},
  ["HL.Monitor"] = {"id","name","description","serial","x","y","width","height",
    "physical_width","physical_height","refresh_rate","scale","transform","focused",
    "dpms_status","vrr_active","is_mirror","mirrors","cm","reserved","active_workspace",
    "active_special_workspace","available_modes"},
  ["HL.LayerSurface"] = {"address","namespace","layer","x","y","w","h","pid","mapped",
    "interactivity","above_fullscreen","monitor"},
  ["HL.Group"] = {"size","current_index","current","locked","denied","members"},
  ["HL.Keybind"] = {"key","display_key","keycode","modmask","handler","arg","submap",
    "description","enabled","locked","release","repeating","mouse","non_consuming",
    "transparent","ignore_mods","long_press","click","drag","catchall"},
  ["HL.Vec2"] = {"x","y"},
  ["HL.Box"] = {"x","y","w","h"},
}
local __REFS = {
  ["HL.Window"] = {"address","class","title"},
  ["HL.Workspace"] = {"id","name"},
  ["HL.Monitor"] = {"id","name"},
}
local __ALWAYS = { ["HL.Group"] = true, ["HL.Vec2"] = true, ["HL.Box"] = true }
local __ESC = { ['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }

local function __str(s)
  return '"' .. s:gsub('[%c"\\]', function(c)
    return __ESC[c] or string.format('\\u%04x', c:byte())
  end) .. '"'
end

local function __tname(v)
  local mt = getmetatable(v)
  return type(mt) == "table" and rawget(mt, "__name") or nil
end

local __J
local function __obj(u, fields, depth, nest)
  local parts = {}
  for _, f in ipairs(fields) do
    local ok, v = pcall(function() return u[f] end)
    if ok and v ~= nil and type(v) ~= "function" then
      parts[#parts + 1] = __str(f) .. ":" .. __J(v, depth + 1, nest + 1)
    end
  end
  return "{" .. table.concat(parts, ",") .. "}"
end

__J = function(v, depth, nest)
  depth, nest = depth or 0, nest or 0
  local t = type(v)
  if v == nil then return "null"
  elseif t == "boolean" then return tostring(v)
  elseif t == "number" then
    if v ~= v or v == math.huge or v == -math.huge then return "null" end
    if math.type(v) == "integer" then return string.format("%d", v) end
    return string.format("%.14g", v)
  elseif t == "string" then return __str(v)
  elseif nest > 12 then return '"<max depth>"'
  elseif t == "userdata" then
    local name = __tname(v)
    local fields = (depth == 0 or __ALWAYS[name]) and __FIELDS[name] or __REFS[name] or __FIELDS[name]
    if fields then return __obj(v, fields, depth, nest) end
    return __str(tostring(v))
  elseif t == "table" then
    local n, count = #v, 0
    for _ in pairs(v) do count = count + 1 end
    local parts = {}
    if count == n then
      for i = 1, n do parts[i] = __J(v[i], depth, nest + 1) end
      return "[" .. table.concat(parts, ",") .. "]"
    end
    for k, x in pairs(v) do
      if type(x) ~= "function" then
        parts[#parts + 1] = __str(tostring(k)) .. ":" .. __J(x, depth, nest + 1)
      end
    end
    return "{" .. table.concat(parts, ",") .. "}"
  end
  return __str(tostring(v))
end

local function __dispatch(d)
  if d == nil then error("invalid dispatcher arguments", 0) end
  local r = hl.dispatch(d)
  if type(r) == "table" and r.ok == false then
    error(tostring(r.error or r.code or "dispatch failed"), 0)
  end
  return r
end
"""


def lua(value: Any) -> str:
    """Converts a Python value into a Lua literal."""
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise HyprlandError("NaN and infinity are not supported")
        return repr(value)
    if isinstance(value, str):
        out = []
        for b in value.encode("utf-8"):
            ch = chr(b)
            if 0x20 <= b < 0x7F and ch not in '"\\':
                out.append(ch)
            else:
                out.append(f"\\{b:03d}")
        return '"' + "".join(out) + '"'
    if isinstance(value, (list, tuple)):
        return "{" + ",".join(lua(v) for v in value) + "}"
    if isinstance(value, dict):
        return "{" + ",".join(f"[{lua(str(k))}]={lua(v)}" for k, v in value.items()) + "}"
    raise HyprlandError(f"cannot convert {type(value).__name__} to Lua")


def _hyprland_env() -> dict[str, str]:
    """The environment for hyprctl. MCP clients often start servers with a minimal
    environment, so find the running instance ourselves if it wasn't passed in."""
    env = os.environ.copy()
    runtime = env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if "HYPRLAND_INSTANCE_SIGNATURE" not in env:
        sockets = Path(runtime, "hypr").glob("*/.socket.sock")
        newest = max(sockets, key=lambda p: p.stat().st_mtime, default=None)
        if newest is not None:
            env["HYPRLAND_INSTANCE_SIGNATURE"] = newest.parent.name
    return env


def _hyprctl(*args: str) -> str:
    try:
        result = subprocess.run(
            ["hyprctl", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_hyprland_env(),
            timeout=15,
        )
    except FileNotFoundError:
        raise HyprlandError("hyprctl not found in PATH") from None
    except subprocess.TimeoutExpired:
        raise HyprlandError("hyprctl timed out") from None
    out = result.stdout.strip()
    if result.returncode != 0:
        detail = (result.stderr.strip() or out).removeprefix("error: ")
        raise HyprlandError(detail or f"hyprctl exited with code {result.returncode}")
    return out


_BODY_OFFSET = LUA_PRELUDE.count("\n") + 1


def _clean_lua_error(message: str) -> str:
    """Strips Lua chunk names and maps line numbers back to the caller's code."""
    message = message.lstrip("=").removeprefix("[C]:-1: ")
    return re.sub(
        r'\[string "[^"]*"\]:(\d+):',
        lambda m: f"line {max(int(m.group(1)) - _BODY_OFFSET, 1)}:",
        message,
    )


def run_lua(body: str) -> Any:
    """Runs a Lua chunk inside Hyprland and returns its result decoded from JSON.

    Multiple return values come back as a list.
    """
    chunk = (
        LUA_PRELUDE
        + "local __r = table.pack((function()\n"
        + body
        + "\nend)())\n"
        + "if __r.n <= 1 then return __J(__r[1]) end\n"
        + "local __a = {} for __i = 1, __r.n do __a[__i] = __r[__i] end\n"
        + "return __J(__a)"
    )
    try:
        out = _hyprctl("repl", chunk)
    except HyprlandError as e:
        raise HyprlandError(_clean_lua_error(str(e))) from None
    if out.startswith("error:"):
        raise HyprlandError(_clean_lua_error(out.removeprefix("error:").strip()))
    try:
        return json.loads(out) if out else None
    except json.JSONDecodeError:
        return out


def hyprctl_json(*args: str) -> Any:
    out = _hyprctl("-j", *args)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


def args_table(**kwargs: Any) -> str:
    """Builds a Lua table literal from keyword arguments, dropping ``None``s."""
    return lua({k: v for k, v in kwargs.items() if v is not None})


def dispatch(dispatcher: str, *positional: Any, **kwargs: Any) -> dict:
    """Builds ``hl.dsp.<dispatcher>(...)`` and dispatches it.

    Pass either positional arguments or keyword arguments (which become a table).
    """
    if positional:
        call_args = ",".join(lua(p) for p in positional)
    elif kwargs:
        call_args = args_table(**kwargs)
    else:
        call_args = ""
    return run_lua(f"return __dispatch(hl.dsp.{dispatcher}({call_args}))")


def _exactly_one(**kwargs: Any) -> str:
    given = [k for k, v in kwargs.items() if v is not None and v is not False]
    if len(given) != 1:
        raise HyprlandError(f"provide exactly one of: {', '.join(kwargs)} (got {given or 'none'})")
    return given[0]



# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=READ_ONLY)
def get_version() -> dict:
    """Returns the Hyprland version, plus build details from hyprctl."""
    return {"lua_version": run_lua("return hl.version()"), "build": hyprctl_json("version")}


@mcp.tool(annotations=READ_ONLY)
def get_desktop_overview() -> Any:
    """Compact tree of monitors -> workspaces -> windows, with focus state.

    The best first call to understand what is on screen.
    """
    return run_lua(
        """
        local active = hl.get_active_window()
        local monitors = {}
        for _, m in ipairs(hl.get_monitors()) do
          local aws, sws = m.active_workspace, m.active_special_workspace
          local mon = {
            id = m.id, name = m.name, description = m.description, focused = m.focused,
            resolution = m.width .. "x" .. m.height .. "@" .. string.format("%.2f", m.refresh_rate),
            position = {x = m.x, y = m.y}, scale = m.scale,
            active_workspace = aws and aws.id, active_special_workspace = sws and sws.name,
            workspaces = {},
          }
          monitors[m.id] = mon
          monitors[#monitors + 1] = mon
        end
        for _, ws in ipairs(hl.get_workspaces()) do
          local entry = { id = ws.id, name = ws.name, special = ws.special, visible = ws.visible,
                          layout = ws.tiled_layout, windows = {} }
          for _, w in ipairs(hl.get_workspace_windows(ws)) do
            entry.windows[#entry.windows + 1] = {
              address = w.address, class = w.class, title = w.title, pid = w.pid,
              floating = w.floating, fullscreen = w.fullscreen, pinned = w.pinned,
              at = w.at, size = w.size, focused = active ~= nil and w.address == active.address,
              grouped = w.group ~= nil,
            }
          end
          local mon = ws.monitor and monitors[ws.monitor.id]
          if mon then mon.workspaces[#mon.workspaces + 1] = entry end
        end
        local list = {}
        for i = 1, #monitors do
          table.sort(monitors[i].workspaces, function(a, b) return a.id < b.id end)
          list[i] = monitors[i]
        end
        return {
          monitors = list,
          active_window = active and {address = active.address, class = active.class, title = active.title},
          submap = hl.get_current_submap(),
        }
        """
    )


@mcp.tool(annotations=READ_ONLY)
def list_monitors() -> Any:
    """Lists all monitors with their properties (modes, scale, position, workspaces...)."""
    return run_lua("return hl.get_monitors()")


@mcp.tool(annotations=READ_ONLY)
def get_monitor(monitor: Selector | None = None) -> Any:
    """Returns one monitor. Defaults to the focused monitor."""
    if monitor is None:
        return run_lua("return hl.get_active_monitor()")
    return run_lua(f"return hl.get_monitor({lua(monitor)})")


@mcp.tool(annotations=READ_ONLY)
def list_workspaces() -> Any:
    """Lists all existing workspaces."""
    return run_lua("return hl.get_workspaces()")


@mcp.tool(annotations=READ_ONLY)
def get_workspace(workspace: Selector | None = None, monitor: Selector | None = None) -> Any:
    """Returns one workspace by selector, or the active workspace (of `monitor`, if given)."""
    if workspace is not None:
        return run_lua(f"return hl.get_workspace({lua(workspace)})")
    return run_lua(f"return hl.get_active_workspace({lua(monitor)})")


@mcp.tool(annotations=READ_ONLY)
def list_windows(
    workspace: Selector | None = None,
    monitor: Selector | None = None,
    class_name: str | None = None,
    title: str | None = None,
    tag: str | None = None,
    floating: bool | None = None,
    mapped: bool | None = None,
    search: str | None = None,
) -> Any:
    """Lists windows, optionally filtered. `class_name`, `title` and `tag` must match exactly;
    `search` is a case-insensitive substring match on class or title."""
    filters = args_table(
        workspace=workspace, monitor=monitor, **{"class": class_name},
        title=title, tag=tag, floating=floating, mapped=mapped,
    )
    windows = run_lua(f"return hl.get_windows({filters})")
    if search and isinstance(windows, list):
        needle = search.lower()
        windows = [
            w for w in windows
            if needle in str(w.get("class", "")).lower() or needle in str(w.get("title", "")).lower()
        ]
    return windows


@mcp.tool(annotations=READ_ONLY)
def get_window(window: Selector | None = None) -> Any:
    """Returns one window by selector (see server instructions). Defaults to the active window."""
    if window is None:
        return run_lua("return hl.get_active_window()")
    return run_lua(f"return hl.get_window({lua(window)})")


@mcp.tool(annotations=READ_ONLY)
def get_focus_history() -> Any:
    """Returns the previously focused window and workspace, and any urgent window."""
    return run_lua(
        "return { last_window = hl.get_last_window(), last_workspace = hl.get_last_workspace(),"
        " urgent_window = hl.get_urgent_window() }"
    )


@mcp.tool(annotations=READ_ONLY)
def list_layers(monitor: Selector | None = None, namespace: str | None = None) -> Any:
    """Lists layer-shell surfaces (bars, wallpapers, launchers, notifications...)."""
    return run_lua(f"return hl.get_layers({args_table(monitor=monitor, namespace=namespace)})")


@mcp.tool(annotations=READ_ONLY)
def get_cursor() -> Any:
    """Returns the cursor position in global layout coordinates and the monitor under it."""
    return run_lua("return { position = hl.get_cursor_pos(), monitor = hl.get_monitor_at_cursor() }")


@mcp.tool(annotations=READ_ONLY)
def get_config(keys: list[str]) -> dict:
    """Reads config options by dotted key, e.g. ['general.gaps_out', 'decoration.rounding']."""
    return run_lua(
        f"local out = {{}} for _, k in ipairs({lua(keys)}) do "
        "local ok, v, err = pcall(hl.get_config, k) "
        "if not ok then out[k] = {error = tostring(v)} "
        "elseif err then out[k] = {error = err} else out[k] = {value = v} end end "
        "return out"
    )


@mcp.tool(annotations=READ_ONLY)
def get_input_state() -> Any:
    """Returns the active submap and the list of loaded plugins."""
    return run_lua("return { submap = hl.get_current_submap(), plugins = hl.get_loaded_plugins() }")


@mcp.tool(annotations=READ_ONLY)
def list_devices() -> Any:
    """Lists connected keyboards, mice, tablets, touch devices and switches."""
    return hyprctl_json("devices")


@mcp.tool(annotations=READ_ONLY)
def list_binds(search: str | None = None) -> Any:
    """Lists registered keybinds. `search` filters (case-insensitive) on key, dispatcher, arg or description."""
    binds = hyprctl_json("binds")
    if search and isinstance(binds, list):
        needle = search.lower()
        binds = [b for b in binds if needle in json.dumps(b).lower()]
    return binds


@mcp.tool(annotations=READ_ONLY)
def get_config_errors() -> Any:
    """Returns current config errors. Check after set_config or reload_config."""
    errors = hyprctl_json("configerrors")
    return [e for e in errors if e] if isinstance(errors, list) else errors


@mcp.tool(annotations=READ_ONLY)
def list_notifications() -> Any:
    """Lists active Hyprland notifications."""
    return run_lua(
        "local out = {} for i, n in ipairs(hl.notification.get()) do out[i] = {"
        " text = n:get_text(), timeout = n:get_timeout(), elapsed = n:get_elapsed(),"
        " paused = n:is_paused(), alive = n:is_alive() } end return out"
    )


@mcp.tool(annotations=READ_ONLY)
def get_splash() -> str:
    """Returns the current random splash message."""
    return _hyprctl("splash")


# --------------------------------------------------------------------------- #
# Focus & windows
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=ACTION)
def focus(
    window: Selector | None = None,
    workspace: Selector | None = None,
    monitor: Selector | None = None,
    direction: Direction | None = None,
    last: bool = False,
    urgent_or_last: bool = False,
    on_current_monitor: bool | None = None,
) -> Any:
    """Moves focus. Provide exactly one target: a window, workspace, monitor, direction,
    `last` (previously focused window) or `urgent_or_last`.

    `on_current_monitor` only applies to workspaces: pull the workspace to the current monitor.
    """
    target = _exactly_one(
        window=window, workspace=workspace, monitor=monitor, direction=direction,
        last=last, urgent_or_last=urgent_or_last,
    )
    args: dict[str, Any] = {target: locals()[target]}
    if target == "workspace":
        args["on_current_monitor"] = on_current_monitor
    return dispatch("focus", **args)


@mcp.tool(annotations=ACTION)
def move_window(
    window: Selector | None = None,
    workspace: Selector | None = None,
    monitor: Selector | None = None,
    direction: Direction | None = None,
    x: float | None = None,
    y: float | None = None,
    relative: bool | None = None,
    follow: bool | None = None,
) -> Any:
    """Moves a window (default: active) to a workspace, a monitor, in a direction, or to/by x,y.

    `follow` (workspace/monitor) keeps focus on the moved window. `relative` makes x,y an offset.
    Floating windows only for x,y.
    """
    has_coords = x is not None or y is not None
    target = _exactly_one(workspace=workspace, monitor=monitor, direction=direction, coordinates=has_coords or None)
    if target == "coordinates":
        if x is None or y is None:
            raise HyprlandError("both x and y are required")
        return dispatch("window.move", x=x, y=y, relative=relative, window=window)
    if target == "direction":
        return dispatch("window.move", direction=direction, window=window)
    return dispatch("window.move", **{target: locals()[target]}, follow=follow, window=window)


@mcp.tool(annotations=ACTION)
def resize_window(x: float, y: float, relative: bool = False, window: Selector | None = None) -> Any:
    """Resizes a window to x*y pixels, or by x,y pixels when `relative` is true."""
    return dispatch("window.resize", x=x, y=y, relative=relative, window=window)


@mcp.tool(annotations=ACTION)
def swap_window(
    direction: Direction | None = None,
    target: Selector | None = None,
    next: bool = False,
    prev: bool = False,
) -> Any:
    """Swaps the active window with another: in a direction, with a target window, or next/prev."""
    which = _exactly_one(direction=direction, target=target, next=next, prev=prev)
    return dispatch("window.swap", **{which: locals()[which]})


@mcp.tool(annotations=ACTION)
def set_window_state(
    state: Literal["floating", "pinned", "pseudo"],
    action: Toggle = "toggle",
    window: Selector | None = None,
) -> Any:
    """Toggles/enables/disables floating, pinned (visible on all workspaces) or pseudotiled state."""
    dispatcher = {"floating": "window.float", "pinned": "window.pin", "pseudo": "window.pseudo"}[state]
    return dispatch(dispatcher, action=action, window=window)


@mcp.tool(annotations=ACTION)
def set_fullscreen(
    mode: Literal["fullscreen", "maximized"] = "fullscreen",
    action: Literal["toggle", "set", "unset"] = "toggle",
    window: Selector | None = None,
) -> Any:
    """Sets a window's fullscreen or maximized state."""
    return dispatch("window.fullscreen", mode=mode, action=action, window=window)


@mcp.tool(annotations=ACTION)
def set_fullscreen_state(
    internal: Literal[-1, 0, 1, 2],
    client: Literal[-1, 0, 1, 2],
    window: Selector | None = None,
) -> Any:
    """Decouples Hyprland's fullscreen state from the one reported to the app.

    Values: -1 keep current, 0 none, 1 maximized, 2 fullscreen. E.g. internal=2, client=0
    fullscreens a browser without triggering its presentation mode.
    """
    return dispatch("window.fullscreen_state", internal=internal, client=client, action="set", window=window)


@mcp.tool(annotations=ACTION)
def center_window(window: Selector | None = None) -> Any:
    """Centers a floating window on its monitor."""
    return dispatch("window.center", window=window)


@mcp.tool(annotations=ACTION)
def set_window_zorder(mode: Literal["top", "bottom"], window: Selector | None = None) -> Any:
    """Raises a floating window to the top or lowers it to the bottom of the stack."""
    return dispatch("window.alter_zorder", mode=mode, window=window)


@mcp.tool(annotations=ACTION)
def tag_window(tag: str | None = None, clear: bool = False, window: Selector | None = None) -> Any:
    """Toggles a tag on a window ('+tag' adds, '-tag' removes), or clears all tags with `clear`."""
    if clear:
        return dispatch("window.clear_tags", window=window)
    if not tag:
        raise HyprlandError("tag is required unless clear is true")
    return dispatch("window.tag", tag=tag, window=window)


@mcp.tool(annotations=ACTION)
def set_window_property(prop: str, value: str | int | float | bool, window: Selector | None = None) -> Any:
    """Sets a dynamic window-rule effect on a window, e.g. prop='opacity' value='0.8',
    prop='no_anim' value='1', prop='border_color' value='rgb(ff0000)'."""
    return dispatch("window.set_prop", prop=prop, value=str(value).lower() if isinstance(value, bool) else str(value), window=window)


@mcp.tool(annotations=DESTRUCTIVE)
def close_window(window: Selector | None = None, force: bool = False) -> Any:
    """Closes a window gracefully. `force` kills the owning process with SIGKILL instead."""
    return dispatch("window.kill" if force else "window.close", window=window)


@mcp.tool(annotations=DESTRUCTIVE)
def signal_window(signal: int, window: Selector | None = None) -> Any:
    """Sends a POSIX signal (number, e.g. 15 for SIGTERM, 19 for SIGSTOP) to a window's process."""
    return dispatch("window.signal", signal=signal, window=window)


@mcp.tool(annotations=ACTION)
def cycle_windows(
    previous: bool = False,
    only: Literal["tiled", "floating"] | None = None,
) -> Any:
    """Focuses the next (or previous) window on the workspace, optionally only tiled/floating ones."""
    return dispatch(
        "window.cycle_next",
        next=not previous,
        tiled=True if only == "tiled" else None,
        floating=True if only == "floating" else None,
    )


@mcp.tool(annotations=ACTION)
def send_shortcut(mods: str, key: str, window: Selector | None = None) -> Any:
    """Sends a key combo to a window without focusing it, e.g. mods='CTRL', key='S'."""
    return dispatch("send_shortcut", mods=mods, key=key, window=window)


# --------------------------------------------------------------------------- #
# Groups
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=ACTION)
def group_window(
    action: Literal["toggle", "next", "prev", "lock", "unlock", "toggle_lock", "deny", "allow", "join", "join_or_create", "leave"],
    direction: Direction | None = None,
    window: Selector | None = None,
) -> Any:
    """Manages tabbed window groups.

    toggle: make/dissolve a group. next/prev: switch tab. lock/unlock/toggle_lock: stop new windows
    joining. deny/allow: keep a window out of groups. join/join_or_create: move the window into the
    group in `direction`. leave: take the window out of its group.
    """
    match action:
        case "toggle":
            return dispatch("group.toggle", window=window)
        case "next" | "prev":
            return dispatch(f"group.{action}", window=window)
        case "lock" | "unlock" | "toggle_lock":
            lock = {"lock": "enable", "unlock": "disable", "toggle_lock": "toggle"}[action]
            return dispatch("group.lock", action=lock, window=window)
        case "deny" | "allow":
            return dispatch("window.deny_from_group", action="enable" if action == "deny" else "disable")
        case "join" | "join_or_create":
            if direction is None:
                raise HyprlandError("direction is required")
            key = "into_group" if action == "join" else "into_or_create_group"
            return dispatch("window.move", **{key: direction}, window=window)
        case "leave":
            return dispatch("window.move", out_of_group=direction or True, window=window)


@mcp.tool(annotations=ACTION)
def group_focus_index(index: int, window: Selector | None = None) -> Any:
    """Focuses the window at `index` in the group."""
    return dispatch("group.active", index=index, window=window)


# --------------------------------------------------------------------------- #
# Workspaces & monitors
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=ACTION)
def toggle_special_workspace(name: str = "") -> Any:
    """Shows/hides a special workspace (scratchpad). Empty name = the default 'special'."""
    return dispatch("workspace.toggle_special", name) if name else dispatch("workspace.toggle_special")


@mcp.tool(annotations=ACTION)
def rename_workspace(workspace: Selector, name: str) -> Any:
    """Renames a workspace."""
    return dispatch("workspace.rename", workspace=workspace, name=name)


@mcp.tool(annotations=ACTION)
def move_workspace_to_monitor(monitor: Selector, workspace: Selector | None = None) -> Any:
    """Moves a workspace (default: active) to another monitor."""
    return dispatch("workspace.move", workspace=workspace, monitor=monitor)


@mcp.tool(annotations=ACTION)
def swap_monitor_workspaces(monitor1: Selector, monitor2: Selector) -> Any:
    """Swaps the active workspaces of two monitors."""
    return dispatch("workspace.swap_monitors", monitor1=monitor1, monitor2=monitor2)


@mcp.tool(annotations=ACTION)
def set_dpms(action: Toggle = "toggle", monitor: Selector | None = None) -> Any:
    """Turns monitor output on/off (DPMS). Applies to all monitors unless one is given.

    Runs on a short timer, as Hyprland recommends, so it returns before the screen changes.
    """
    args = args_table(action=action, monitor=monitor)
    return run_lua(
        f"hl.timer(function() hl.dispatch(hl.dsp.dpms({args})) end, {{timeout = 300, type = 'oneshot'}}) "
        "return 'scheduled'"
    )


@mcp.tool(annotations=ACTION)
def send_layout_message(message: str) -> Any:
    """Sends a message to the active layout, e.g. 'togglesplit' (dwindle) or 'swapwithmaster' (master)."""
    return dispatch("layout", message)


# --------------------------------------------------------------------------- #
# Cursor & input
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=ACTION)
def move_cursor(x: float | None = None, y: float | None = None, corner: Literal[0, 1, 2, 3] | None = None, window: Selector | None = None) -> Any:
    """Moves the cursor to global x,y, or to a window corner (0 bottom-left, 1 bottom-right,
    2 top-right, 3 top-left)."""
    if corner is not None:
        return dispatch("cursor.move_to_corner", corner=corner, window=window)
    if x is None or y is None:
        raise HyprlandError("provide x and y, or corner")
    return dispatch("cursor.move", x=x, y=y)


@mcp.tool(annotations=ACTION)
def switch_keyboard_layout(layout: Literal["next", "prev"] | int = "next", device: str = "all") -> str:
    """Switches the xkb layout of a keyboard (or 'all') to next/prev or a layout index."""
    return _hyprctl("switchxkblayout", device, str(layout))


@mcp.tool(annotations=ACTION)
def set_submap(name: str = "reset") -> Any:
    """Enters a keybind submap, or 'reset' to return to the global map."""
    return dispatch("submap", name)


# --------------------------------------------------------------------------- #
# Apps, notifications, config
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=ACTION)
def launch(command: str, rules: dict[str, Any] | None = None) -> Any:
    """Runs a shell command (via sh -c). `rules` applies window-rule effects to the window it opens,
    e.g. {"float": true, "workspace": "3 silent", "size": [800, 600], "move": [0, 0]}."""
    if rules:
        return run_lua(f"hl.exec_cmd({lua(command)}, {lua(rules)}) return 'launched'")
    return run_lua(f"hl.exec_cmd({lua(command)}) return 'launched'")


@mcp.tool(annotations=ACTION)
def notify(
    text: str,
    timeout_ms: int = 5000,
    icon: Literal["warning", "info", "hint", "error", "confused", "ok", "none"] = "info",
    color: str | None = None,
    font_size: float | None = None,
) -> Any:
    """Shows a Hyprland notification. `color` like 'rgb(ff8800)'."""
    icons = {"warning": 0, "info": 1, "hint": 2, "error": 3, "confused": 4, "ok": 5, "none": -1}
    opts = args_table(text=text, timeout=timeout_ms, icon=icons[icon], color=color, font_size=font_size)
    return run_lua(f"hl.notification.create({opts}) return 'shown'")


@mcp.tool(annotations=ACTION)
def dismiss_notifications(amount: int | None = None) -> str:
    """Dismisses all notifications, or the oldest `amount` of them."""
    return _hyprctl("dismissnotify", *([str(amount)] if amount is not None else []))


@mcp.tool(annotations=ACTION)
def set_config(options: dict[str, Any]) -> Any:
    """Changes config options at runtime (not persisted to disk). Keys are dotted, e.g.
    {"general.gaps_out": 10, "decoration.blur.enabled": false, "general.col.active_border":
    "rgba(33ccffee)"}. Reverted by reload_config."""
    merged: dict[str, Any] = {}
    for key, value in options.items():
        node, parts = merged, key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise HyprlandError(f"conflicting keys at {key!r}")
        node[parts[-1]] = value
    return run_lua(f"hl.config({lua(merged)}) return 'ok'")


@mcp.tool(annotations=ACTION)
def add_window_rule(
    match: dict[str, str | int | bool],
    effects: dict[str, Any],
    name: str | None = None,
) -> Any:
    """Adds a runtime window rule (lost on reload). `match` e.g. {"class": "^firefox$"};
    `effects` e.g. {"float": true, "opacity": "0.9", "workspace": "2"}."""
    spec = {**effects, "match": match, **({"name": name} if name else {})}
    return run_lua(f"hl.window_rule({lua(spec)}) return 'ok'")


@mcp.tool(annotations=ACTION)
def add_workspace_rule(workspace: str, rules: dict[str, Any]) -> Any:
    """Adds a runtime workspace rule (lost on reload), e.g. workspace='1',
    rules={"monitor": "DP-1", "gaps_out": 0, "persistent": true}."""
    return run_lua(f"hl.workspace_rule({lua({**rules, 'workspace': workspace})}) return 'ok'")


@mcp.tool(annotations=DESTRUCTIVE)
def reload_config(config_only: bool = False) -> str:
    """Reloads the config from disk, discarding runtime changes. `config_only` skips monitor reload."""
    return _hyprctl("reload", *(["config-only"] if config_only else []))


@mcp.tool(annotations=DESTRUCTIVE)
def enter_kill_mode() -> str:
    """Enters kill mode: the next window the user clicks is killed (ESC cancels)."""
    return _hyprctl("kill")


# --------------------------------------------------------------------------- #
# Escape hatches
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=DESTRUCTIVE)
def run_lua_code(code: str) -> Any:
    """Runs arbitrary Lua inside Hyprland and returns the result as JSON.

    Use `return` to get values back; HL objects (windows, monitors...) are serialised.
    The full `hl` API is available, e.g.:
      return hl.get_windows({ floating = true })
      hl.dispatch(hl.dsp.window.float({ action = "toggle", window = "class:mpv" }))
    """
    return run_lua(code)


@mcp.tool(annotations=DESTRUCTIVE)
def dispatch_lua(dispatcher: str) -> Any:
    """Dispatches a raw Lua dispatcher expression and reports failure, e.g.
    'hl.dsp.window.move({ workspace = "special:magic" })'."""
    return run_lua(f"return __dispatch({dispatcher})")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
