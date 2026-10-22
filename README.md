<div align="center">

# Hyprland MCP Server

</div>

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-Linux-lightgrey.svg)
![Build Status](https://img.shields.io/badge/build-passing-brightgreen.svg)
![MCP Server](https://badge.mcpx.dev?type=server 'MCP Server')

</div>

https://github.com/user-attachments/assets/b4fc1d26-ec04-451a-b7c9-7f87b44d9c9e

A lightweight, unofficial [Model Context Protocol (MCP)](https://github.com/modelcontextprotocol) server for the [Hyprland](https://wiki.hypr.land/) Wayland compositor. It is built on Hyprland's **Lua API** (Hyprland 0.55+): every query and action runs as a small Lua chunk through `hyprctl repl`, using `hl.get_*` for state and `hl.dsp.*` for dispatchers.

This lets language models query and control windows, workspaces, monitors, groups, input, notifications and runtime config in natural language.

> **Note:** This project is experimental and in beta. It needs Hyprland **0.55 or newer** with a Lua config (`hyprctl status` should show `configProvider: lua`). For older, hyprlang-based Hyprland versions, use the hyprctl-based server from commit `13d5195`.

## 🚀 Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/stefanoamorelli/hyprmcp.git
cd hyprmcp
```

### 2. Install the server in your MCP client

For example, in Claude Desktop, edit `~/.config/Claude/claude_desktop_config.json`:

```json
"Hyprland MCP Server": {
  "command": "uv",
  "args": ["run", "--directory", "<global path of the repo>", "hyprmcp"]
}
```

Or with Claude Code:

```bash
claude mcp add hyprland -- uv run --directory <global path of the repo> hyprmcp
```

The server finds the running Hyprland instance on its own. To pin one, set `HYPRLAND_INSTANCE_SIGNATURE` in the server's `env`.

## 🧰 Available Tools

Window arguments accept Hyprland selectors (`class:<regex>`, `title:<regex>`, `pid:<pid>`, `address:0x...`, `activewindow`, ...). Regexes must match the whole value, e.g. `class:.*firefox.*`. When you leave the window out, the active window is used.

### Queries

| Tool | Description |
|------|-------------|
| `get_desktop_overview` | Compact tree of monitors → workspaces → windows, with focus state. |
| `get_version` | Hyprland version and build info. |
| `list_monitors` / `get_monitor` | All monitors, or one (default: focused). |
| `list_workspaces` / `get_workspace` | All workspaces, or one (default: active). |
| `list_windows` | Windows, filtered by workspace, monitor, class, title, tag, floating or substring search. |
| `get_window` | One window by selector (default: active). |
| `get_focus_history` | Last focused window and workspace, and any urgent window. |
| `list_layers` | Layer-shell surfaces (bars, wallpapers, launchers). |
| `get_cursor` | Cursor position and the monitor under it. |
| `get_config` | Reads config options by dotted key (`general.gaps_out`). |
| `get_config_errors` | Current config errors. |
| `get_input_state` | Active submap and loaded plugins. |
| `list_devices` | Keyboards, mice, tablets, ... |
| `list_binds` | Registered keybinds, with optional search. |
| `list_notifications` | Active Hyprland notifications. |
| `get_splash` | The current splash message. |

### Windows & focus

| Tool | Description |
|------|-------------|
| `focus` | Focus a window, workspace, monitor, direction, the last window, or an urgent one. |
| `move_window` | Move a window to a workspace or monitor, in a direction, or to/by x,y. |
| `resize_window` | Resize a window to, or by, x,y. |
| `swap_window` | Swap with a window in a direction, a target window, or next/prev. |
| `set_window_state` | Toggle floating, pinned or pseudotiled. |
| `set_fullscreen` / `set_fullscreen_state` | Fullscreen or maximize, or set separate internal and client fullscreen states. |
| `center_window`, `set_window_zorder` | Center a floating window, or raise or lower it. |
| `cycle_windows` | Focus the next or previous window (optionally tiled or floating only). |
| `tag_window`, `set_window_property` | Tags and dynamic window-rule props (opacity, border color, ...). |
| `send_shortcut` | Send a key combo to a window without focusing it. |
| `close_window`, `signal_window` | Close a window gracefully or force-kill it, or send it a POSIX signal. |
| `group_window`, `group_focus_index` | Tabbed groups: toggle, next/prev, lock, join, leave, deny. |

### Workspaces, monitors & input

| Tool | Description |
|------|-------------|
| `toggle_special_workspace` | Show or hide a scratchpad. |
| `rename_workspace`, `move_workspace_to_monitor`, `swap_monitor_workspaces` | Workspace management. |
| `set_dpms` | Turn monitors on or off. |
| `send_layout_message` | Layout messages (`togglesplit`, `swapwithmaster`, ...). |
| `move_cursor` | Move the cursor to x,y or to a window corner. |
| `switch_keyboard_layout`, `set_submap` | Switch the xkb layout or the keybind submap. |

### Apps, config & escape hatches

| Tool | Description |
|------|-------------|
| `launch` | Run a command, optionally with window rules for the window it opens. |
| `notify`, `dismiss_notifications` | Hyprland notifications. |
| `set_config` | Change options at runtime (`{"general.gaps_out": 10}`). |
| `add_window_rule`, `add_workspace_rule` | Runtime window and workspace rules. |
| `reload_config` | Reload the config from disk. |
| `enter_kill_mode` | Kill the next window that is clicked. |
| `run_lua_code` | Run arbitrary Lua against the `hl` API; results come back as JSON. |
| `dispatch_lua` | Dispatch a raw `hl.dsp.*` expression. |

Runtime changes (`set_config`, rules) last until the next config reload and are never written to your config files.

## 💡 Example Usage

**User Prompt:** "Move Firefox to workspace 3 and make it float."

**MCP Server Action:** `move_window(window="class:firefox", workspace=3)` → `hl.dispatch(hl.dsp.window.move({ window = "class:firefox", workspace = 3 }))`, then `set_window_state("floating", "enable", "class:firefox")`.

**User Prompt:** "What's on my second monitor?"

**MCP Server Action:** `get_desktop_overview()` returns each monitor with its workspaces and windows.

## 🛠️ Development

```bash
uv run --with 'mcp[cli]' mcp dev hyprmcp/server.py
```

Ensure that Hyprland is running.

## 📚 References

- [Hyprland Documentation](https://wiki.hypr.land/)
- [Hyprland Dispatchers (Lua)](https://wiki.hypr.land/Configuring/Basics/Dispatchers/)
- [Model Context Protocol](https://github.com/modelcontextprotocol)
- [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)

## 📜 License

[MIT License](LICENSE) © 2025 Stefano Amorelli
