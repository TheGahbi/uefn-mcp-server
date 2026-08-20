"""MCP Server for UEFN Editor.

External process that bridges Claude Code (stdio) to the UEFN HTTP listener.
Requires: pip install mcp

Usage:
    python mcp_server.py
    python mcp_server.py --port 8765

Claude Code config (~/.claude/settings.json or project .mcp.json):
    {
      "mcpServers": {
        "uefn": {
          "command": "python",
          "args": ["/path/to/mcp_server.py"]
        }
      }
    }
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as _e:
    # The #1 cause of "works on my machine but not theirs": the `mcp` package is
    # not installed in THIS interpreter. Claude Code launches whatever `python`
    # (or the configured command) resolves to — which may differ from the one you
    # ran `pip install mcp` against. Fail LOUD with the exact fix instead of a
    # cryptic "server failed to start".
    sys.stderr.write(
        "\n[uefn-mcp] FATAL: the 'mcp' package is not installed in this Python.\n"
        f"           Interpreter: {sys.executable}\n"
        f"           Version:     {sys.version.split()[0]}\n"
        "           Fix (installs into THIS exact interpreter):\n"
        f'               "{sys.executable}" -m pip install mcp\n'
        "           Then in your .mcp.json set the SAME interpreter as \"command\":\n"
        f'               "command": "{sys.executable}"\n'
        f"           Original error: {_e}\n\n"
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_PORT = int(os.environ.get("UEFN_MCP_PORT", "8765"))
MAX_PORT = 8770
REQUEST_TIMEOUT = 30.0

_discovered_port: Optional[int] = None

# ---------------------------------------------------------------------------
# Port discovery
# ---------------------------------------------------------------------------


def _discover_port() -> int:
    """Find the listener by scanning the port range.

    Tries the last known port first, then scans DEFAULT_PORT..MAX_PORT.
    Caches the result so subsequent calls are instant.
    """
    global _discovered_port

    # Fast path: already discovered and still alive
    if _discovered_port is not None:
        if _ping_port(_discovered_port):
            return _discovered_port
        _discovered_port = None

    # Scan the range
    for port in range(DEFAULT_PORT, MAX_PORT + 1):
        if _ping_port(port):
            _discovered_port = port
            return port

    raise ConnectionError(
        f"UEFN listener not found on ports {DEFAULT_PORT}-{MAX_PORT}. "
        "Start it in the UEFN editor console: py \"path/to/uefn_listener.py\""
    )


def _ping_port(port: int) -> bool:
    """Quick check if a listener responds on the given port."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            body = json.loads(resp.read().decode())
            return body.get("status") == "ok"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def _send_command(command: str, params: Optional[dict] = None, timeout: float = REQUEST_TIMEOUT) -> dict:
    """Send a command to the UEFN listener and return the result.

    Auto-discovers the listener port by scanning the range.

    Raises:
        ConnectionError: Listener is not running.
        RuntimeError: Command failed on the UEFN side.
        TimeoutError: Command timed out.
    """
    global _discovered_port

    port = _discover_port()
    url = f"http://127.0.0.1:{port}"

    payload = json.dumps({"command": command, "params": params or {}}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        # Port may have changed — invalidate cache and retry once
        if _discovered_port is not None:
            _discovered_port = None
            return _send_command(command, params, timeout)
        raise ConnectionError(
            "UEFN listener is not running. "
            "Start it in the UEFN editor console: py \"path/to/uefn_listener.py\""
        ) from e
    except Exception as e:
        if "timed out" in str(e).lower():
            raise TimeoutError(f"Command '{command}' timed out after {timeout}s") from e
        raise

    if not body.get("success", False):
        error_msg = body.get("error", "Unknown error")
        tb = body.get("traceback", "")
        raise RuntimeError(f"UEFN command '{command}' failed: {error_msg}\n{tb}".strip())

    return body.get("result", {})


def _check_connection() -> str:
    """Quick connection check, returns status message."""
    try:
        port = _discover_port()
        return f"Connected to UEFN on port {port}"
    except ConnectionError:
        return "NOT CONNECTED - UEFN listener is not running"
    except Exception as e:
        return f"Connection error: {e}"


# ---------------------------------------------------------------------------
# Heartbeat — periodic ping so the listener knows we're alive
# ---------------------------------------------------------------------------

_HEARTBEAT_INTERVAL = 10.0


def _heartbeat_loop() -> None:
    """Ping the listener periodically."""
    time.sleep(3.0)  # wait for listener to be ready
    while True:
        try:
            port = _discover_port()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}",
                method="GET",
            )
            urllib.request.urlopen(req, timeout=2.0)
        except Exception:
            pass
        time.sleep(_HEARTBEAT_INTERVAL)


threading.Thread(target=_heartbeat_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "uefn-mcp",
    instructions=(
        "MCP server for controlling UEFN (Unreal Editor for Fortnite). "
        "Provides tools to manage actors, assets, levels, and viewport in the UEFN editor. "
        "The 'execute_python' tool is the most powerful — it runs arbitrary Python code "
        "inside the editor with full access to the `unreal` module. "
        "Use structured tools for common operations and execute_python for everything else.\n\n"
        "IMPORTANT: When creating tkinter UI windows via execute_python, NEVER call tk.Tk(). "
        "Use `root = get_tk_root()` to get the shared root, then `tk.Toplevel(root)` for windows. "
        "Multiple tk.Tk() instances will crash the editor."
    ),
)


# -- System tools ------------------------------------------------------------


@mcp.tool()
def ping() -> str:
    """Check if the UEFN editor listener is running and responsive."""
    result = _send_command("ping")
    return json.dumps(result, indent=2)


@mcp.tool()
def execute_python(code: str) -> str:
    """Execute arbitrary Python code inside the UEFN editor.

    The code runs on the main editor thread with full access to the `unreal` module.
    Pre-populated variables: unreal, actor_sub, asset_sub, level_sub, tk, get_tk_root.
    Assign to `result` variable to return a value. Use print() for stdout output.

    IMPORTANT — tkinter windows:
        Use get_tk_root() to get the shared tk.Tk() root, then create windows with
        tk.Toplevel(root). NEVER create a new tk.Tk() — multiple Tk instances crash
        the editor. The root is shared across all scripts in the process.

    Examples:
        # Get world name
        result = unreal.EditorLevelLibrary.get_editor_world().get_name()

        # List all static mesh actors
        actors = actor_sub.get_all_level_actors()
        result = [a.get_actor_label() for a in actors if a.get_class().get_name() == 'StaticMeshActor']

        # Create a material
        mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            'M_Test', '/Game/Materials', unreal.Material, unreal.MaterialFactoryNew()
        )
        result = str(mat.get_path_name())

        # Create a tkinter window (ALWAYS use Toplevel, never tk.Tk!)
        import threading
        def show_window():
            root = get_tk_root()
            win = tk.Toplevel(root)
            win.title("My Tool")
            win.attributes("-topmost", True)
            tk.Label(win, text="Hello from UEFN").pack(padx=20, pady=20)
            root.mainloop()
        threading.Thread(target=show_window, daemon=True).start()
        result = "Window opened"
    """
    result = _send_command("execute_python", {"code": code})
    parts = []
    if result.get("stdout"):
        parts.append(f"stdout:\n{result['stdout']}")
    if result.get("stderr"):
        parts.append(f"stderr:\n{result['stderr']}")
    if result.get("result") is not None:
        parts.append(f"result: {json.dumps(result['result'], indent=2)}")
    return "\n".join(parts) if parts else "(no output)"


@mcp.tool()
def get_log(last_n: int = 50) -> str:
    """Get recent MCP listener log entries from the UEFN editor."""
    result = _send_command("get_log", {"last_n": last_n})
    return "\n".join(result.get("lines", []))


@mcp.tool()
def shutdown() -> str:
    """Gracefully stop the UEFN listener, freeing the port.

    The listener will finish the current request, then shut down.
    After this call the listener must be restarted from the UEFN console.
    """
    result = _send_command("shutdown", timeout=5.0)
    return json.dumps(result, indent=2)


# -- Actor tools -------------------------------------------------------------


@mcp.tool()
def get_all_actors(class_filter: str = "") -> str:
    """List all actors in the current level.

    Args:
        class_filter: Optional class name to filter by (e.g. 'StaticMeshActor', 'PointLight').
    """
    result = _send_command("get_all_actors", {"class_filter": class_filter})
    return json.dumps(result, indent=2)


@mcp.tool()
def get_selected_actors() -> str:
    """Get currently selected actors in the UEFN viewport."""
    result = _send_command("get_selected_actors")
    return json.dumps(result, indent=2)


@mcp.tool()
def spawn_actor(
    asset_path: str = "",
    actor_class: str = "",
    location: Optional[list[float]] = None,
    rotation: Optional[list[float]] = None,
) -> str:
    """Spawn an actor in the current level.

    Provide either asset_path OR actor_class (not both).

    Args:
        asset_path: Asset path to spawn from (e.g. '/Engine/BasicShapes/Cube').
        actor_class: Unreal class name (e.g. 'PointLight', 'CameraActor').
        location: [x, y, z] coordinates. Defaults to origin.
        rotation: [pitch, yaw, roll] in degrees. Defaults to zero.
    """
    params: dict[str, Any] = {}
    if asset_path:
        params["asset_path"] = asset_path
    if actor_class:
        params["actor_class"] = actor_class
    if location is not None:
        params["location"] = location
    if rotation is not None:
        params["rotation"] = rotation
    result = _send_command("spawn_actor", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def delete_actors(actor_paths: list[str]) -> str:
    """Delete actors from the current level by path or label.

    Args:
        actor_paths: List of actor path names or labels to delete.
    """
    result = _send_command("delete_actors", {"actor_paths": actor_paths})
    return json.dumps(result, indent=2)


@mcp.tool()
def set_actor_transform(
    actor_path: str,
    location: Optional[list[float]] = None,
    rotation: Optional[list[float]] = None,
    scale: Optional[list[float]] = None,
) -> str:
    """Set an actor's transform (location, rotation, and/or scale).

    Args:
        actor_path: Actor path name or label.
        location: [x, y, z] world coordinates.
        rotation: [pitch, yaw, roll] in degrees.
        scale: [x, y, z] scale factors.
    """
    params: dict[str, Any] = {"actor_path": actor_path}
    if location is not None:
        params["location"] = location
    if rotation is not None:
        params["rotation"] = rotation
    if scale is not None:
        params["scale"] = scale
    result = _send_command("set_actor_transform", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def get_actor_properties(actor_path: str, properties: list[str]) -> str:
    """Read specific properties from an actor.

    Note: UEFN uses Fort*-prefixed actor classes (e.g. FortStaticMeshActor instead of
    StaticMeshActor). Some standard UE5 property names may not exist on Fort* actors.
    Properties that fail to read will return an error string instead of a value.

    Args:
        actor_path: Actor path name or label.
        properties: List of property names to read (e.g. ['static_mesh_component', 'mobility']).
    """
    result = _send_command("get_actor_properties", {"actor_path": actor_path, "properties": properties})
    return json.dumps(result, indent=2)


@mcp.tool()
def set_actor_properties(actor_path: str, properties: dict[str, Any]) -> str:
    """Set properties on an actor via set_editor_property().

    Note: UEFN uses Fort*-prefixed actor classes (e.g. FortStaticMeshActor instead of
    StaticMeshActor). Not all properties are writable — some are read-only or don't exist
    on Fort* actors. For methods like set_actor_hidden_in_game(), use execute_python instead.
    Each property reports 'ok' or an error individually.

    Args:
        actor_path: Actor path name or label.
        properties: Dict of property names to values (e.g. {'cast_shadow': False}).
    """
    result = _send_command("set_actor_properties", {"actor_path": actor_path, "properties": properties})
    return json.dumps(result, indent=2)


@mcp.tool()
def select_actors(actor_paths: list[str], add_to_selection: bool = False) -> str:
    """Select actors in the UEFN viewport.

    Args:
        actor_paths: List of actor path names or labels to select.
        add_to_selection: If True, add to current selection instead of replacing.
    """
    result = _send_command("select_actors", {"actor_paths": actor_paths, "add_to_selection": add_to_selection})
    return json.dumps(result, indent=2)


@mcp.tool()
def focus_selected() -> str:
    """Move the viewport camera to focus on the currently selected actors (like pressing F)."""
    result = _send_command("focus_selected")
    return json.dumps(result, indent=2)



@mcp.tool()
def get_editor_log(last_n: int = 100, filter_str: str = "") -> str:
    """Read recent lines from the Unreal Editor Output Log.

    Args:
        last_n: Number of recent lines to return.
        filter_str: Optional filter — only lines containing this string (case-insensitive).
    """
    result = _send_command("get_editor_log", {"last_n": last_n, "filter_str": filter_str})
    lines = result.get("lines", [])
    if result.get("error"):
        return f"Error: {result['error']}"
    return "\n".join(lines)


# -- Asset tools -------------------------------------------------------------


@mcp.tool()
def list_assets(directory: str = "/Game/", recursive: bool = True, class_filter: str = "") -> str:
    """List assets in a directory.

    Args:
        directory: Content directory path (e.g. '/Game/', '/Game/Materials/').
        recursive: Include subdirectories.
        class_filter: Optional class name filter (e.g. 'Material', 'StaticMesh').
    """
    result = _send_command("list_assets", {"directory": directory, "recursive": recursive, "class_filter": class_filter})
    return json.dumps(result, indent=2)


@mcp.tool()
def get_asset_info(asset_path: str) -> str:
    """Get detailed info about an asset.

    Args:
        asset_path: Full asset path (e.g. '/Game/Materials/M_Base').
    """
    result = _send_command("get_asset_info", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def get_selected_assets() -> str:
    """Get assets currently selected in the Content Browser."""
    result = _send_command("get_selected_assets")
    return json.dumps(result, indent=2)


@mcp.tool()
def rename_asset(old_path: str, new_path: str) -> str:
    """Rename or move an asset.

    Args:
        old_path: Current asset path.
        new_path: New asset path.
    """
    result = _send_command("rename_asset", {"old_path": old_path, "new_path": new_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def delete_asset(asset_path: str) -> str:
    """Delete an asset.

    Args:
        asset_path: Asset path to delete.
    """
    result = _send_command("delete_asset", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def duplicate_asset(source_path: str, dest_path: str) -> str:
    """Duplicate an asset to a new path.

    Args:
        source_path: Source asset path.
        dest_path: Destination asset path.
    """
    result = _send_command("duplicate_asset", {"source_path": source_path, "dest_path": dest_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def does_asset_exist(asset_path: str) -> str:
    """Check if an asset exists at the given path.

    Args:
        asset_path: Asset path to check.
    """
    result = _send_command("does_asset_exist", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def save_asset(asset_path: str) -> str:
    """Save a modified asset.

    Args:
        asset_path: Asset path to save.
    """
    result = _send_command("save_asset", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def search_assets(class_name: str = "", directory: str = "/Game/", recursive: bool = True) -> str:
    """Search for assets using the Asset Registry.

    Args:
        class_name: Filter by class name (e.g. 'Material', 'Texture2D').
        directory: Directory to search in.
        recursive: Include subdirectories.
    """
    result = _send_command("search_assets", {"class_name": class_name, "directory": directory, "recursive": recursive})
    return json.dumps(result, indent=2)


# -- Project tools -----------------------------------------------------------


@mcp.tool()
def get_project_info() -> str:
    """Get the UEFN project name and content root path.

    Use the returned content_root as the base path for asset operations
    (e.g. list_assets, search_assets, create assets via execute_python).
    In UEFN the content root is '/{ProjectName}/', NOT '/Game/'.
    """
    result = _send_command("get_project_info")
    return json.dumps(result, indent=2)


# -- Level tools -------------------------------------------------------------


@mcp.tool()
def save_current_level() -> str:
    """Save the current level."""
    result = _send_command("save_current_level")
    return json.dumps(result, indent=2)


@mcp.tool()
def get_level_info() -> str:
    """Get info about the current level (name, actor count)."""
    result = _send_command("get_level_info")
    return json.dumps(result, indent=2)


# -- Viewport tools ----------------------------------------------------------


@mcp.tool()
def get_viewport_camera() -> str:
    """Get the current viewport camera position and rotation."""
    result = _send_command("get_viewport_camera")
    return json.dumps(result, indent=2)


@mcp.tool()
def set_viewport_camera(
    location: Optional[list[float]] = None,
    rotation: Optional[list[float]] = None,
) -> str:
    """Move the viewport camera to a position.

    Args:
        location: [x, y, z] world coordinates.
        rotation: [pitch, yaw, roll] in degrees.
    """
    params: dict[str, Any] = {}
    if location is not None:
        params["location"] = location
    if rotation is not None:
        params["rotation"] = rotation
    result = _send_command("set_viewport_camera", params)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# -- Epic first-party MCP bridge ---------------------------------------------
# UEFN (UE 6.0+) ships its own MCP server on http://127.0.0.1:8000/mcp exposing
# 29 toolsets / 384 tools. These tools proxy to it. See epic_bridge.py for the
# handshake details. Our execute_python still reaches MORE surface (Epic's server
# hides SlateInspector/PCG/Plugin/AIAssistant/... and has no arbitrary-code path),
# so prefer Epic's where it covers the job and fall back to execute_python.

try:
    import epic_bridge as _epic
except Exception:  # bridge is optional; never break the server over it
    _epic = None


def _need_epic():
    if _epic is None:
        return "epic_bridge.py not importable next to mcp_server.py"
    if not _epic.is_available():
        return ("Epic's MCP did not answer on http://127.0.0.1:8000/mcp. "
                "It ships with UEFN UE 6.0+ (AIAssistant plugin); make sure a "
                "project is open in a recent UEFN.")
    return None


@mcp.tool()
def build_verse() -> str:
    """Compile all Verse in the open project via Epic's first-party MCP.

    This is the ONLY scripted Verse build that works. It replaces the old
    "user presses Ctrl+Shift+B" / build_verse.ps1 mouse-click workaround.

    Returns a JSON list of diagnostics -- an EMPTY list means the build succeeded.
    Each entry carries: severity (Error|Warning|Information|Hint), code, message,
    filePath, and span{startLine,startCharacter,endLine,endCharacter}, so compiler
    errors come back structured instead of needing the editor log scraped.
    """
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.build_verse(), indent=2)


@mcp.tool()
def epic_list_toolsets() -> str:
    """List the toolsets exposed by Epic's first-party UEFN MCP (29 toolsets / 384 tools)."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    r = _epic.list_toolsets()
    return r if isinstance(r, str) else json.dumps(r, indent=2)


@mcp.tool()
def epic_describe_toolset(toolset_name: str) -> str:
    """Describe one Epic toolset: every tool name plus its full JSON input/output schema.

    Args:
        toolset_name: e.g. 'ValkyrieToolset.EntityToolset', 'MVVMToolset.MVVMToolset',
                      'WidgetAnimationToolset.WidgetAnimationToolset'.
                      Use epic_list_toolsets() to discover names.
    """
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    r = _epic.describe_toolset(toolset_name)
    return r if isinstance(r, str) else json.dumps(r, indent=2)


@mcp.tool()
def epic_call_tool(tool_name: str, arguments_json: str = "{}") -> str:
    """Call any tool on Epic's first-party UEFN MCP by its FULL dotted name.

    Args:
        tool_name: full name including toolset, e.g.
                   'ValkyrieToolset.EntityToolset.CreateEntity'
                   'ValkyrieToolset.SessionToolset.StartSession'
                   'MVVMToolset.MVVMToolset.CreateViewBinding'
        arguments_json: JSON object of arguments. Get the exact schema from
                   epic_describe_toolset() first -- Epic's errors are self-documenting
                   and will print the expected schema if you get it wrong.

    Notable tools this unlocks (all previously needed hand-rolled workarounds):
        ValkyrieToolset.VerseToolset.*       ReadFile/WriteFile/Replace/Grep/BuildAll
        ValkyrieToolset.EntityToolset.*      CreateEntity(with transform)/AddComponent/
                                             SetEntityTransform -- plain world-space XYZ,
                                             no mangled __verse_0x... property names
        ValkyrieToolset.DeviceToolset.*      PlaceDevice/AddEventBinding
        ValkyrieToolset.SessionToolset.*     StartSession/PushChanges/GetClientLogEntries
        VerseFieldsToolset.*                 AddVerseField/BindWidgetPropertyToVerseField
        MVVMToolset.*                        CreateViewBinding/ListConversionFunctions
        WidgetAnimationToolset.*             CreateWidgetAnimation/AddWidgetToAnimation
    """
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"arguments_json is not valid JSON: {e}"}, indent=2)
    r = _epic.call(tool_name, args)
    return r if isinstance(r, str) else json.dumps(r, indent=2)


# -- Epic MCP: first-class wrappers -------------------------------------------
# Convenience tools over the Epic toolsets that previously needed hand-rolled
# workarounds. Anything not covered here is still reachable via epic_call_tool.


@mcp.tool()
def verse_list_files(path: str = "", recursive: bool = False) -> str:
    """List Verse files/directories. Call with path="" first to see the mounted roots.

    IMPORTANT: paths are Verse MODULE paths like '/MyProject/Folder/file.verse',
    NOT filesystem paths. Passing a Windows path is rejected.
    """
    err = _need_epic()
    if err: return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.verse_list(path, recursive), indent=2)


@mcp.tool()
def verse_read_file(path: str) -> str:
    """Read a Verse source file by its module path (e.g. '/MyProject/thing.verse')."""
    err = _need_epic()
    if err: return json.dumps({"error": err}, indent=2)
    r = _epic.verse_read(path)
    return r.get("returnValue", json.dumps(r)) if isinstance(r, dict) else str(r)


@mcp.tool()
def verse_write_file(path: str, content: str, create_if_missing: bool = True) -> str:
    """Write a Verse source file (module path). Follow with build_verse() to compile."""
    err = _need_epic()
    if err: return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.verse_write(path, content, create_if_missing), indent=2)


@mcp.tool()
def verse_replace(path: str, old_string: str, new_string: str,
                  replace_all: bool = False) -> str:
    """Exact string replacement inside a Verse file. Cheaper than rewriting the file."""
    err = _need_epic()
    if err: return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.verse_replace(path, old_string, new_string, replace_all), indent=2)


@mcp.tool()
def entity_find(name_filter: str = "", recursive: bool = True) -> str:
    """List Scene Graph entities in the open level, with their class and refPath."""
    err = _need_epic()
    if err: return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.entity_find(recursive, name_filter), indent=2)


@mcp.tool()
def entity_create(name: str, x: float = 0.0, y: float = 0.0, z: float = 0.0,
                  pitch: float = 0.0, yaw: float = 0.0, roll: float = 0.0,
                  scale: float = 1.0) -> str:
    """Create a Scene Graph entity WITH its world transform in one call.

    Plain world-space XYZ -- no mangled __verse_0x... property names, no LUF sign
    flip. Returns the entity refPath, which the other entity_* tools take.
    """
    err = _need_epic()
    if err: return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.entity_create(name, (x, y, z), (pitch, yaw, roll),
                                          (scale, scale, scale)), indent=2)


@mcp.tool()
def entity_add_component(entity_ref_path: str, component_class_path: str) -> str:
    """Add a component to an entity.

    Args:
        entity_ref_path: refPath from entity_create/entity_find.
        component_class_path: e.g.
            '/VerseEngineAssets/_Verse/VNI/VerseEngineAssets.BasicShapes_cube'
            '/EntityFramework/_Verse/VNI/Component.mesh_component'
            Use entity_list_component_classes() to search the 179 available.
    """
    err = _need_epic()
    if err: return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.entity_add_component({"refPath": entity_ref_path},
                                                 component_class_path), indent=2)


@mcp.tool()
def entity_list_component_classes(name_filter: str = "") -> str:
    """Search the 179 Scene Graph component classes by substring."""
    err = _need_epic()
    if err: return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.entity_component_classes(name_filter), indent=2)


@mcp.tool()
def entity_get_components(entity_ref_path: str) -> str:
    """List the components actually on an entity. Use this to VERIFY an add landed."""
    err = _need_epic()
    if err: return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.entity_components({"refPath": entity_ref_path}), indent=2)


@mcp.tool()
def entity_set_transform(entity_ref_path: str, x: float, y: float, z: float,
                         pitch: float = 0.0, yaw: float = 0.0, roll: float = 0.0,
                         scale: float = 1.0) -> str:
    """Move/rotate/scale an entity in plain world space."""
    err = _need_epic()
    if err: return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.entity_set_transform({"refPath": entity_ref_path},
                                                 (x, y, z), (pitch, yaw, roll),
                                                 (scale, scale, scale)), indent=2)


@mcp.tool()
def entity_delete(entity_ref_path: str, display_name: str = "") -> str:
    """Delete an entity, with the ROOT-entity escape hatch built in.

    Epic's DeleteEntity refuses root entities ("Cannot delete the root entity").
    Pass display_name and this falls back to removing the backing EntityProxyActor
    via SceneTools.remove_from_scene, which does work.
    """
    err = _need_epic()
    if err: return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.entity_delete({"refPath": entity_ref_path},
                                          display_name or None), indent=2)


@mcp.tool()
def session_control(action: str) -> str:
    """Drive a UEFN playtest session.

    Args:
        action: one of start_session | stop_session | start_game | stop_game |
                push_changes | status | game_state | client_logs
    """
    err = _need_epic()
    if err: return json.dumps({"error": err}, indent=2)
    fns = {"start_session": _epic.session_start, "stop_session": _epic.session_stop,
           "start_game": _epic.game_start, "stop_game": _epic.game_stop,
           "push_changes": _epic.session_push, "status": _epic.session_status,
           "game_state": _epic.game_state, "client_logs": _epic.client_logs}
    fn = fns.get(action)
    if not fn:
        return json.dumps({"error": f"unknown action {action!r}",
                           "valid": sorted(fns)}, indent=2)
    return json.dumps(fn(), indent=2)


# -- Epic MCP: devices / verse fields / MVVM / widget animations --------------
# Covers the capabilities that previously required hand-rolled hacks:
# T3D clipboard, ctypes patches at offsets 200 and 256/264, 'Sequencer UI only'.


@mcp.tool()
def device_list_assets(name_filter: str = '') -> str:
    """Search placeable Fortnite device assets by substring."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.device_list_assets(name_filter), indent=2)


@mcp.tool()
def device_place(asset_path: str, x: float = 0.0, y: float = 0.0, z: float = 0.0, yaw: float = 0.0, scale: float = 1.0) -> str:
    """Place a device at a world transform. Replaces the T3D clipboard trick."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.device_place(asset_path, (x, y, z), (0.0, yaw, 0.0), (scale, scale, scale)), indent=2)


@mcp.tool()
def device_list_properties(device_path: str) -> str:
    """List the settable properties on a placed device."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.device_list_properties(device_path), indent=2)


@mcp.tool()
def device_set_property(device_path: str, property_name: str, value: str) -> str:
    """Set one property on a placed device."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.device_set_property(device_path, property_name, value), indent=2)


@mcp.tool()
def device_binding_options(device_path: str) -> str:
    """List events and functions a device exposes for event binding."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.device_binding_options(device_path), indent=2)


@mcp.tool()
def device_add_event_binding(source_device_path: str, source_event: str, target_device_path: str, target_function: str) -> str:
    """Wire a device event to another device function. Call device_binding_options first for valid names."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.device_add_event_binding(source_device_path, source_event, target_device_path, target_function), indent=2)


@mcp.tool()
def verse_field_list(widget_blueprint: str) -> str:
    """List Verse fields on a Widget Blueprint (asset path)."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.verse_field_list(widget_blueprint), indent=2)


@mcp.tool()
def verse_field_add(widget_blueprint: str, field_name: str, field_type: str, default_value: str = '', visibility: str = 'public', write_access: str = 'public', is_var: bool = True) -> str:
    """Add a Verse field to a Widget Blueprint. Officially supported now; this used to need a ctypes patch at descriptor offset 200. field_type is a Verse type: logic|int|float|string|message|event|color|color_alpha|material|texture."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.verse_field_add(widget_blueprint, field_name, field_type, default_value, visibility, write_access, is_var), indent=2)


@mcp.tool()
def verse_field_bind_widget(widget_blueprint: str, verse_field_name: str, target_widget: str, widget_property_path: str, conversion_name: str = '', mode: str = '') -> str:
    """Bind a widget property to a Verse field, optionally through a CONVERSION function. conversion_name is the piece that previously needed hand-authoring MVVMBlueprintViewConversionFunction plus a ctypes GraphName patch at offsets 256/264. Discover names with mvvm_list_conversion_functions."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.verse_field_bind_widget(widget_blueprint, verse_field_name, target_widget, widget_property_path, mode, conversion_name), indent=2)


@mcp.tool()
def mvvm_list_bindings(widget_blueprint: str) -> str:
    """List MVVM view bindings. NOTE a conversion binding serializes with an EMPTY SourcePath - its real source is on a pin of the conversion node. Do not report those as sourceless."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.mvvm_list_bindings(widget_blueprint), indent=2)


@mcp.tool()
def mvvm_list_conversion_functions(widget_blueprint: str) -> str:
    """List conversion functions available for bindings on this Widget Blueprint."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.mvvm_list_conversion_functions(widget_blueprint), indent=2)


@mcp.tool()
def mvvm_create_binding(widget_blueprint: str, source_context: str, source_property_path: str, destination_context: str, destination_property_path: str, conversion_name: str = '') -> str:
    """Create an MVVM property binding, optionally through a conversion function."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.mvvm_create_binding(widget_blueprint, source_context, source_property_path, destination_context, destination_property_path, conversion_name), indent=2)


@mcp.tool()
def mvvm_fixup(widget_blueprint: str) -> str:
    """Regenerate MVVM binding graphs to match stored data. Repairs broken binding state."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.mvvm_fixup(widget_blueprint), indent=2)


@mcp.tool()
def widget_animation_list(widget_blueprint: str) -> str:
    """List the animations on a Widget Blueprint."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.widget_anim_list(widget_blueprint), indent=2)


@mcp.tool()
def widget_animation_create(widget_blueprint: str, animation_name: str, length_seconds: float = 0.0) -> str:
    """Create a NEW widget animation. Previously believed impossible from script - add_possessable returns an empty Guid because CanPossessObject needs a playback context. Epic's tool does it."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.widget_anim_create(widget_blueprint, animation_name, length_seconds or None), indent=2)


@mcp.tool()
def widget_animation_add_widget(widget_blueprint: str, animation: str, object_to_bind: str) -> str:
    """Bind a widget INTO an animation - the step that used to require the Sequencer UI."""
    err = _need_epic()
    if err:
        return json.dumps({"error": err}, indent=2)
    return json.dumps(_epic.widget_anim_add_widget(widget_blueprint, animation, object_to_bind), indent=2)

def _doctor() -> int:
    """Self-diagnosis for 'it works for them but not me'. Run:  python mcp_server.py --check"""
    print("UEFN MCP - setup check")
    print("-" * 48)
    print(f"Python interpreter : {sys.executable}")
    print(f"Python version     : {sys.version.split()[0]}")
    try:
        import mcp  # noqa: F401
        print("mcp package        : OK (importable in this interpreter)")
    except ImportError:
        print("mcp package        : MISSING  <-- this is why the server won't start")
        print(f'  Fix: "{sys.executable}" -m pip install mcp')
        print(f'  And set  "command": "{sys.executable}"  in your .mcp.json')
        return 1
    print(f"Scanning for listener on ports {DEFAULT_PORT}-{MAX_PORT} ...")
    for port in range(DEFAULT_PORT, MAX_PORT + 1):
        if _ping_port(port):
            print(f"UEFN listener      : FOUND on port {port}")
            print("\nAll good. Use this in .mcp.json:")
            print(f'  {{ "command": "{sys.executable}", "args": ["{os.path.abspath(__file__)}"] }}')
            return 0
    print("UEFN listener      : NOT FOUND")
    print("  Open your project in UEFN and run uefn_listener.py via Tools > Execute Python Script.")
    print("  (The server still starts; it will connect once the listener is up.)")
    print("\nUse this in .mcp.json:")
    print(f'  {{ "command": "{sys.executable}", "args": ["{os.path.abspath(__file__)}"] }}')
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv or "--doctor" in sys.argv:
        sys.exit(_doctor())

    # Allow --port override (skips auto-discovery, uses fixed port)
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--port" and i < len(sys.argv) - 1:
            _discovered_port = int(sys.argv[i + 1])

    mcp.run()
