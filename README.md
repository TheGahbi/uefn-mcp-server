# UEFN MCP Server

Control [UEFN](https://dev.epicgames.com/documentation/en-us/fortnite/unreal-editor-for-fortnite) (Unreal Editor for Fortnite) from [Claude Code](https://docs.anthropic.com/en/docs/claude-code) via the [Model Context Protocol](https://modelcontextprotocol.io/).

```
Claude Code  <--stdio-->  MCP Server (mcp_server.py)  <--HTTP-->  Listener (uefn_listener.py, inside UEFN)
```

## What Claude can do with this

- 🎨 **Build materials from scratch** — author full node graphs (world-aligned / tri-planar projection, texture objects, constants, engine material functions), generate and import textures, recompile and save. A finished, publish-safe material in one conversation.
- 🧱 **Place and configure devices** — duplicate any Creative device, set its options (speed values, descriptions, XP awards, ...), position, rotate, and label it. Ten devices or hundreds — same effort.
- 📜 **Place Verse devices** — materialize a verse device for any compiled class directly into the level, no dragging from the Content Browser.
- 🔗 **Link devices to Verse scripts** — wire `@editable` slots to level devices and apply Verse tags entirely from script. The full technique (proxy subobjects, `SavedActor`, T3D clipboard authoring) is documented in [docs/verse_device_linking.md](docs/verse_device_linking.md).
- 🏗️ **Edit the scene** — spawn, move, duplicate, and delete actors; add components to placed actors; select and focus things for the user.
- 📷 **See its own work** — drive the viewport camera and capture screenshots, enabling look-verify-fix loops without touching your mouse.
- 🐛 **Debug alongside you** — read the editor Output Log, query project/level/asset state, search the Content Browser.
- 🔨 **Compile, save, and push by itself** — trigger the Verse build (Ctrl+Shift+B), Save All, and Push Changes from script, then read its own compile errors from the log and fix them until the build is green. See [docs/editor_actions.md](docs/editor_actions.md).
- 🐍 **Everything else** — `execute_python` runs arbitrary editor Python with the full `unreal` module (UEFN exposes 37,000+ types), so anything the editor's Python can reach, Claude can do.

## v0.4.0 - now bridges Epic's official UEFN MCP

UEFN (UE 6.0+) ships its own MCP server in the experimental `AIAssistant` plugin, on
`http://127.0.0.1:8000/mcp`. It exposes **29 toolsets / 384 tools** behind a 3-tool gateway.

**This server now wraps it, so you get both surfaces from one MCP:**

| | |
|---|---|
| **60 tools total** | 32 bridged from Epic + 28 original |
| `build_verse` | Epic's `BuildAll`. Returns structured diagnostics with file + line/character spans. **`[]` means success.** This replaces the old "user presses Ctrl+Shift+B" step entirely. |
| `verse_*` | read/write/replace/list Verse source (module paths, not filesystem paths) |
| `entity_*` | Scene Graph: create with a world transform in one call, add components, set transform, delete (incl. a fallback for undeletable ROOT entities) |
| `device_*` | place devices, set properties, wire event bindings |
| `verse_field_*` | add Verse fields to widgets, bind widget properties (incl. conversion functions) |
| `mvvm_*` | create view bindings, list conversion functions, repair MVVM state |
| `widget_animation_*` | create animations and bind widgets into them |
| `session_control` | start/stop session and game, push changes, read client logs |
| `epic_call_tool` | generic access to any of the 384 Epic tools by dotted name |

### Why keep this server at all?

Epic's MCP is typed and maintained, but it is a **curated subset**. It does not expose
`SlateInspectorToolset`, `PCGToolset`, `PluginToolset`, `AIAssistantToolset`,
`AgentSkillToolset`, `DataflowAgentToolset`, `GameFeaturesToolset`, `GameplayCueToolset`,
`NiagaraToolset_Blueprint` and roughly eight more that `ToolsetRegistry` actually has -
and it has no arbitrary-code path.

`execute_python` still reaches strictly more surface: full `unreal` reflection, ctypes,
asset bytes, the filesystem, and the hand-built recipes this project is built on
(complete Niagara system authoring, T3D clipboard editing, widget-tree construction,
AnimSequence generation).

**Rule of thumb:** prefer the Epic-bridged tool where one exists - it is validated and
stable. Fall back to `execute_python` for the long tail.

### Requirements for the bridged tools

A recent UEFN (UE 6.0+) with a project open. If Epic's endpoint is not answering, every
bridged tool returns a clear error instead of failing silently; the original 28 tools keep
working regardless.

Note: UEFN now ships with Python **disabled by default**
(`LogPython: Python disabled via CVar 'Engine.Python.IsEnabledByDefault'`), which stops
`init_unreal.py` auto-starting the listener. Enable it in Project Settings, or call
`epic_call_tool` with `ValkyrieToolset.ValkyriePythonToolset.EnablePythonInUEFN`.

## Under the hood

- **60 structured tools** - 28 native (actors, assets, levels, viewport, project info, editor log) plus 32 bridged from Epic's official UEFN MCP
- **Zero C++ compilation** — pure Python, works across UEFN versions
- **Main-thread safe** — all `unreal.*` calls dispatched via editor tick callback
- **Live status window** — dark card UI with connection dots, metrics, and a real-time activity sparkline

## Quick Start

### 0. Let Claude do the setup

Open Claude Code and ask: *"Help me set up UEFN MCP server"* — it will install dependencies, create config files, and walk you through the rest.

If you prefer to do it manually, follow steps 1-5 below.

### 1. Enable Python in UEFN

1. Open your project in UEFN
2. Go to **Project > Project Settings**
3. Search for **Python** and check the box for **Python Editor Script Plugin**

### 2. Start the listener inside UEFN

Use **Tools > Execute Python Script** in the UEFN menu bar, then select the `uefn_listener.py` file.

A **status window** will appear showing:
- **Listener status** — green when running, red when stopped
- **MCP Server status** — green when Claude Code is connected (heartbeat every 10s)
- **Port** — editable when listener is stopped
- **Metrics** — uptime, request count, errors, last command, avg response time
- **Controls** — Stop / Start / Restart buttons

You can safely close this window — the listener continues running in the background.

### 3. Install MCP SDK

On your system (not inside UEFN):

```bash
pip install mcp
```

### 4. Configure Claude Code

Create `.mcp.json` in your project root (or add to `~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "uefn": {
      "command": "python",
      "args": ["C:/path/to/uefn-mcp-server/mcp_server.py"]
    }
  }
}
```

### 5. Restart Claude Code

Claude Code picks up `.mcp.json` on startup. After restart, you'll have 28 UEFN tools available.

### Try it

Ask Claude Code:
- *"List all actors in the level"*
- *"Spawn a cube at position 100, 200, 300"*
- *"What assets are in /Game/Materials/?"*
- *"Move the viewport camera to look at the origin"*

## Auto-start (optional)

To start the listener automatically when UEFN opens your project:

```bash
# Copy both files to your UEFN project's Content/Python/ directory
cp uefn_listener.py  <YourUEFNProject>/Content/Python/uefn_listener.py
cp init_unreal.py     <YourUEFNProject>/Content/Python/init_unreal.py
```

UEFN automatically executes `init_unreal.py` on project open.

## Tools

| Category | Tools |
|----------|-------|
| **System** | `ping`, `execute_python`, `get_log`, `get_editor_log`, `shutdown` |
| **Actors** | `get_all_actors`, `get_selected_actors`, `spawn_actor`, `delete_actors`, `set_actor_transform`, `get_actor_properties`, `set_actor_properties`, `select_actors`, `focus_selected` |
| **Assets** | `list_assets`, `get_asset_info`, `get_selected_assets`, `rename_asset`, `delete_asset`, `duplicate_asset`, `does_asset_exist`, `save_asset`, `search_assets` |
| **Project** | `get_project_info` |
| **Level** | `save_current_level`, `get_level_info` |
| **Viewport** | `get_viewport_camera`, `set_viewport_camera` |

The `execute_python` tool is the most powerful — it runs arbitrary Python code inside the editor with full access to the `unreal` module:

```python
# Pre-populated variables: unreal, actor_sub, asset_sub, level_sub, tk, get_tk_root
# Assign to `result` to return a value

actors = actor_sub.get_all_level_actors()
result = [a.get_actor_label() for a in actors]
```

> **Tkinter note:** When creating UI windows via `execute_python`, use `get_tk_root()` + `tk.Toplevel(root)`. Never call `tk.Tk()` — multiple instances crash the editor.

## Architecture

The system uses two independently running Python processes:

| Component | File | Runs in | Python | Dependencies |
|-----------|------|---------|--------|--------------|
| **Listener** | `uefn_listener.py` | UEFN editor process | 3.11+ (embedded) | stdlib only |
| **MCP Server** | `mcp_server.py` | External process | 3.10+ (system) | `mcp` SDK |

**Why two processes?**
- All `unreal.*` calls must happen on the editor's main thread (tick callback)
- The MCP SDK needs pip-installable packages that can't be added to UEFN's embedded Python
- Each component can restart independently

See [docs/architecture.md](docs/architecture.md) for details.

## Configuration

### Custom port

```json
{
  "mcpServers": {
    "uefn": {
      "command": "python",
      "args": ["path/to/mcp_server.py", "--port", "8766"]
    }
  }
}
```

Or via environment variable:

```json
{
  "mcpServers": {
    "uefn": {
      "command": "python",
      "args": ["path/to/mcp_server.py"],
      "env": { "UEFN_MCP_PORT": "8766" }
    }
  }
}
```

## Bonus Tools

Scripts that run inside the UEFN editor to introspect the Python API.
Run via **Tools > Execute Python Script** in the UEFN menu bar.

| Script | Description |
|--------|-------------|
| [`tools/dump_uefn_api.py`](tools/dump_uefn_api.py) | Dump all classes, enums, structs, functions to JSON |
| [`tools/generate_uefn_stub.py`](tools/generate_uefn_stub.py) | Generate `.pyi` type stub for IDE autocomplete (37K+ types) |
| [`tests/test_feasibility.py`](tests/test_feasibility.py) | Verify UEFN sandbox supports HTTP/threading for MCP |

## Documentation

| Document | Description |
|----------|-------------|
| [Setup Guide](docs/setup.md) | Detailed installation and configuration |
| [Tools Reference](docs/tools_reference.md) | All 28 tools with parameters, examples, and responses |
| [Architecture](docs/architecture.md) | How the two-component system works internally |
| [Troubleshooting](docs/troubleshooting.md) | Common issues and solutions — including script-caused editor crashes |
| [UEFN Python Capabilities](docs/uefn_python_capabilities.md) | Full API capabilities map — 37K types across 30 domains |
| [Editor Actions](docs/editor_actions.md) | Scripted Save All / Verse build / Push Changes + the compile-error self-check loop |
| [AI Context Pack](ai-context-pack/START_HERE.md) | Hand this folder to an AI agent — operating rules, mental model, and transferable lessons, written for an AI to read |

## Requirements

- UEFN editor with Python scripting enabled (Project Settings)
- Python 3.10+ on host system
- `pip install mcp`
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI

## License

MIT
