"""MCP HTTP Listener for UEFN Editor.

Runs an HTTP server on a background thread inside the UEFN editor.
All unreal.* API calls are dispatched to the main thread via tick callback.

Usage (in UEFN editor console):
    py "path/to/uefn_listener.py"

Or auto-start via init_unreal.py.
"""

import io
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional

import unreal

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = "0.4.0"
DEFAULT_PORT = 8765
MAX_PORT = 8770
TICK_BATCH_LIMIT = 5
HTTP_TIMEOUT_SEC = 30.0
POLL_INTERVAL_SEC = 0.02
STALE_CLEANUP_SEC = 60.0
LOG_RING_SIZE = 200

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Shared state — stored on `unreal` module so re-runs of the script
# share the same objects (queues, metrics, tick handle, etc.).
# ---------------------------------------------------------------------------

def _init_shared_state() -> None:
    """Initialise shared state on the ``unreal`` module (once)."""
    defaults: Dict[str, Any] = {
        "_mcp_server": None,
        "_mcp_server_thread": None,
        "_mcp_tick_handle": None,
        "_mcp_bound_port": 0,
        "_mcp_command_queue": queue.Queue(),
        "_mcp_main_queue": queue.Queue(),
        "_mcp_responses": {},
        "_mcp_responses_lock": threading.Lock(),
        "_mcp_request_counter": 0,
        "_mcp_log_ring": [],
        "_mcp_metrics": {
            "started_at": 0.0,
            "total_requests": 0,
            "total_errors": 0,
            "last_request_at": 0.0,
            "last_command": "",
            "last_error": "",
            "last_client_ping": 0.0,
            "response_times_ms": [],
        },
        "_mcp_status_window": None,
    }
    for attr, default in defaults.items():
        if not hasattr(unreal, attr):
            setattr(unreal, attr, default)

_init_shared_state()

# Convenience aliases for mutable containers — safe because dicts/queues
# are modified in-place, so the alias always points to the shared object.
_command_queue: queue.Queue = unreal._mcp_command_queue
_main_queue: queue.Queue = unreal._mcp_main_queue
_responses: Dict[str, dict] = unreal._mcp_responses
_responses_lock: threading.Lock = unreal._mcp_responses_lock
_log_ring: List[str] = unreal._mcp_log_ring
_metrics: Dict[str, Any] = unreal._mcp_metrics

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log(msg: str, level: str = "info") -> None:
    """Log to UE Output Log and internal ring buffer."""
    entry = f"[MCP] {msg}"
    _log_ring.append(entry)
    if len(_log_ring) > LOG_RING_SIZE:
        _log_ring.pop(0)
    if level == "error":
        unreal.log_error(entry)
    elif level == "warning":
        unreal.log_warning(entry)
    else:
        unreal.log(entry)


# ---------------------------------------------------------------------------
# Main-thread helpers
# ---------------------------------------------------------------------------


def _run_on_main_thread(fn: Callable[[], Any]) -> None:
    """Schedule *fn* to execute on the UE main thread (next tick)."""
    _main_queue.put(fn)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _serialize(obj: Any) -> Any:
    """Convert unreal objects to JSON-serializable types."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _serialize(v) for k, v in obj.items()}
    if isinstance(obj, unreal.Vector):
        return {"x": obj.x, "y": obj.y, "z": obj.z}
    if isinstance(obj, unreal.Rotator):
        return {"pitch": obj.pitch, "yaw": obj.yaw, "roll": obj.roll}
    if isinstance(obj, unreal.Vector2D):
        return {"x": obj.x, "y": obj.y}
    if isinstance(obj, unreal.LinearColor):
        return {"r": obj.r, "g": obj.g, "b": obj.b, "a": obj.a}
    if isinstance(obj, unreal.Color):
        return {"r": obj.r, "g": obj.g, "b": obj.b, "a": obj.a}
    if isinstance(obj, unreal.Transform):
        return {
            "location": _serialize(obj.translation),
            "rotation": _serialize(obj.rotation.rotator()),
            "scale": _serialize(obj.scale3d),
        }
    if isinstance(obj, unreal.AssetData):
        return {
            "asset_name": str(obj.asset_name),
            "asset_class": str(obj.asset_class_path.asset_name) if hasattr(obj, "asset_class_path") else str(getattr(obj, "asset_class", "")),
            "package_name": str(obj.package_name),
            "package_path": str(obj.package_path),
            "object_path": str(obj.get_export_text_name()) if hasattr(obj, "get_export_text_name") else str(obj.object_path) if hasattr(obj, "object_path") else "",
        }
    # Generic unreal.Object
    if hasattr(obj, "get_path_name"):
        return str(obj.get_path_name())
    if hasattr(obj, "get_name"):
        return str(obj.get_name())
    # Enum
    if hasattr(obj, "__class__") and hasattr(obj.__class__, "__qualname__"):
        cls_name = obj.__class__.__qualname__
        if "." in cls_name or cls_name[0].isupper():
            return str(obj)
    try:
        return str(obj)
    except Exception:
        return repr(obj)


def _serialize_actor(actor: unreal.Actor) -> dict:
    """Serialize an actor to a dict with common properties."""
    return {
        "name": actor.get_name(),
        "label": actor.get_actor_label(),
        "class": actor.get_class().get_name(),
        "path": actor.get_path_name(),
        "location": _serialize(actor.get_actor_location()),
        "rotation": _serialize(actor.get_actor_rotation()),
        "scale": _serialize(actor.get_actor_scale3d()),
    }


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

_HANDLERS: Dict[str, Callable] = {}


def _register(name: str):
    """Decorator to register a command handler."""
    def decorator(fn: Callable):
        _HANDLERS[name] = fn
        return fn
    return decorator


def _dispatch(command: str, params: dict) -> dict:
    """Dispatch a command to its handler. Runs on main thread."""
    handler = _HANDLERS.get(command)
    if handler is None:
        raise ValueError(f"Unknown command: {command}. Available: {list(_HANDLERS.keys())}")
    return handler(**params)


# -- System ------------------------------------------------------------------


@_register("ping")
def _cmd_ping() -> dict:
    return {
        "status": "ok",
        "version": PROTOCOL_VERSION,
        "python_version": sys.version,
        "port": unreal._mcp_bound_port,
        "timestamp": time.time(),
        "commands": list(_HANDLERS.keys()),
    }


@_register("status")
def _cmd_status() -> dict:
    """Full listener status with metrics."""
    uptime = time.time() - _metrics["started_at"] if _metrics["started_at"] > 0 else 0.0
    times = _metrics["response_times_ms"]
    avg_ms = sum(times) / len(times) if times else 0.0
    return {
        "running": unreal._mcp_server is not None,
        "version": PROTOCOL_VERSION,
        "port": unreal._mcp_bound_port,
        "uptime_sec": round(uptime, 1),
        "total_requests": _metrics["total_requests"],
        "total_errors": _metrics["total_errors"],
        "avg_response_ms": round(avg_ms, 2),
        "last_request_at": _metrics["last_request_at"],
        "last_command": _metrics["last_command"],
        "last_error": _metrics["last_error"],
        "queue_size": _command_queue.qsize(),
        "commands": list(_HANDLERS.keys()),
    }


@_register("shutdown")
def _cmd_shutdown() -> dict:
    """Schedule listener shutdown after current request completes.

    Uses a short timer on a daemon thread to avoid deadlock — the HTTP
    handler that is processing this very request must finish first.
    """
    def _deferred_stop() -> None:
        time.sleep(0.5)
        _run_on_main_thread(stop_listener)

    threading.Thread(target=_deferred_stop, daemon=True).start()
    _log("Shutdown scheduled in 0.5s")
    return {"status": "shutting_down", "port": unreal._mcp_bound_port}


@_register("get_log")
def _cmd_get_log(last_n: int = 50) -> dict:
    return {"lines": _log_ring[-last_n:]}


@_register("execute_python")
def _cmd_execute_python(code: str) -> dict:
    """Execute arbitrary Python code on the main thread.

    Assign to `result` to return a value. Use print() for stdout.
    Pre-populated globals: unreal, actor_sub, asset_sub, level_sub.
    """
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr

    exec_globals: Dict[str, Any] = {
        "__builtins__": __builtins__,
        "unreal": unreal,
        "tk": tk,
        "get_tk_root": _get_tk_root,
        "result": None,
    }
    # Pre-populate subsystems (best-effort)
    for attr, cls_name in [
        ("actor_sub", "EditorActorSubsystem"),
        ("asset_sub", "EditorAssetSubsystem"),
        ("level_sub", "LevelEditorSubsystem"),
    ]:
        try:
            cls = getattr(unreal, cls_name)
            exec_globals[attr] = unreal.get_editor_subsystem(cls)
        except Exception:
            pass

    try:
        sys.stdout, sys.stderr = stdout_buf, stderr_buf
        exec(code, exec_globals)
    except Exception:
        traceback.print_exc(file=stderr_buf)
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr

    return {
        "result": _serialize(exec_globals.get("result")),
        "stdout": stdout_buf.getvalue(),
        "stderr": stderr_buf.getvalue(),
    }


# -- Actors ------------------------------------------------------------------


@_register("get_all_actors")
def _cmd_get_all_actors(class_filter: str = "") -> dict:
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = actor_sub.get_all_level_actors()
    if class_filter:
        actors = [a for a in actors if a.get_class().get_name() == class_filter]
    return {"actors": [_serialize_actor(a) for a in actors], "count": len(actors)}


@_register("get_selected_actors")
def _cmd_get_selected_actors() -> dict:
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = actor_sub.get_selected_level_actors()
    return {"actors": [_serialize_actor(a) for a in actors], "count": len(actors)}


@_register("spawn_actor")
def _cmd_spawn_actor(
    asset_path: str = "",
    actor_class: str = "",
    location: Optional[List[float]] = None,
    rotation: Optional[List[float]] = None,
) -> dict:
    loc = unreal.Vector(*location) if location else unreal.Vector(0, 0, 0)
    rot = unreal.Rotator(*rotation) if rotation else unreal.Rotator(0, 0, 0)

    if asset_path:
        asset = unreal.EditorAssetLibrary.load_asset(asset_path)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_path}")
        actor = unreal.EditorLevelLibrary.spawn_actor_from_object(asset, loc, rot)
    elif actor_class:
        cls = getattr(unreal, actor_class, None)
        if cls is None:
            raise ValueError(f"Class not found: {actor_class}")
        actor = unreal.EditorLevelLibrary.spawn_actor_from_class(cls, loc, rot)
    else:
        raise ValueError("Provide either asset_path or actor_class")

    if actor is None:
        raise RuntimeError("Failed to spawn actor")
    return {"actor": _serialize_actor(actor)}


@_register("delete_actors")
def _cmd_delete_actors(actor_paths: List[str]) -> dict:
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    all_actors = actor_sub.get_all_level_actors()
    deleted = []
    for path in actor_paths:
        for actor in all_actors:
            if actor.get_path_name() == path or actor.get_actor_label() == path:
                actor_sub.destroy_actor(actor)
                deleted.append(path)
                break
    return {"deleted": deleted, "count": len(deleted)}


@_register("set_actor_transform")
def _cmd_set_actor_transform(
    actor_path: str,
    location: Optional[List[float]] = None,
    rotation: Optional[List[float]] = None,
    scale: Optional[List[float]] = None,
) -> dict:
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    all_actors = actor_sub.get_all_level_actors()
    target = None
    for a in all_actors:
        if a.get_path_name() == actor_path or a.get_actor_label() == actor_path:
            target = a
            break
    if target is None:
        raise ValueError(f"Actor not found: {actor_path}")

    if location is not None:
        target.set_actor_location(unreal.Vector(*location), False, False)
    if rotation is not None:
        target.set_actor_rotation(unreal.Rotator(*rotation), False)
    if scale is not None:
        target.set_actor_scale3d(unreal.Vector(*scale))
    return {"actor": _serialize_actor(target)}


@_register("get_actor_properties")
def _cmd_get_actor_properties(actor_path: str, properties: List[str]) -> dict:
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    all_actors = actor_sub.get_all_level_actors()
    target = None
    for a in all_actors:
        if a.get_path_name() == actor_path or a.get_actor_label() == actor_path:
            target = a
            break
    if target is None:
        raise ValueError(f"Actor not found: {actor_path}")

    result = {}
    for prop in properties:
        try:
            result[prop] = _serialize(target.get_editor_property(prop))
        except Exception as e:
            result[prop] = f"<error: {e}>"
    return {"actor_path": actor_path, "properties": result}


@_register("set_actor_properties")
def _cmd_set_actor_properties(actor_path: str, properties: Dict[str, Any]) -> dict:
    """Set properties on an actor via set_editor_property."""
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    all_actors = actor_sub.get_all_level_actors()
    target = None
    for a in all_actors:
        if a.get_path_name() == actor_path or a.get_actor_label() == actor_path:
            target = a
            break
    if target is None:
        raise ValueError(f"Actor not found: {actor_path}")

    set_results = {}
    for prop, value in properties.items():
        try:
            target.set_editor_property(prop, value)
            set_results[prop] = "ok"
        except Exception as e:
            set_results[prop] = f"<error: {e}>"
    return {"actor_path": actor_path, "properties": set_results}


@_register("select_actors")
def _cmd_select_actors(actor_paths: List[str], add_to_selection: bool = False) -> dict:
    """Select actors in the viewport by path or label."""
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    all_actors = actor_sub.get_all_level_actors()

    to_select = []
    found = []
    for path in actor_paths:
        for a in all_actors:
            if a.get_path_name() == path or a.get_actor_label() == path:
                to_select.append(a)
                found.append(path)
                break

    if add_to_selection:
        current = actor_sub.get_selected_level_actors()
        to_select = list(current) + to_select

    actor_sub.set_selected_level_actors(to_select)
    return {"selected": found, "count": len(found)}


@_register("focus_selected")
def _cmd_focus_selected() -> dict:
    """Move viewport camera to focus on selected actors (like pressing F)."""
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    selected = actor_sub.get_selected_level_actors()
    if not selected:
        raise ValueError("No actors selected")

    # Calculate bounding center of selected actors
    xs, ys, zs = [], [], []
    for a in selected:
        loc = a.get_actor_location()
        xs.append(loc.x)
        ys.append(loc.y)
        zs.append(loc.z)

    center_x = sum(xs) / len(xs)
    center_y = sum(ys) / len(ys)
    center_z = sum(zs) / len(zs)

    # Pull camera back from center
    spread = max(
        max(xs) - min(xs),
        max(ys) - min(ys),
        max(zs) - min(zs),
        200.0,
    )
    cam_dist = spread * 1.5
    cam_loc = unreal.Vector(center_x - cam_dist * 0.5, center_y - cam_dist * 0.5, center_z + cam_dist * 0.5)
    cam_rot = unreal.Rotator(-35, 45, 0)

    unreal.EditorLevelLibrary.set_level_viewport_camera_info(cam_loc, cam_rot)
    return {
        "center": {"x": center_x, "y": center_y, "z": center_z},
        "camera": _serialize(cam_loc),
        "actors_count": len(selected),
    }




@_register("get_editor_log")
def _cmd_get_editor_log(last_n: int = 100, filter_str: str = "") -> dict:
    """Read recent lines from the UE Output Log file."""
    log_path = unreal.Paths.project_log_dir()
    log_file = None
    try:
        import os
        log_dir = str(log_path)
        # Find the most recent .log file
        log_files = [f for f in os.listdir(log_dir) if f.endswith(".log")]
        if log_files:
            log_files.sort(key=lambda f: os.path.getmtime(os.path.join(log_dir, f)), reverse=True)
            log_file = os.path.join(log_dir, log_files[0])
    except Exception:
        pass

    if not log_file:
        return {"lines": [], "error": "Log file not found"}

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        lines = all_lines[-last_n:]
        if filter_str:
            lines = [l for l in lines if filter_str.lower() in l.lower()]
        return {"lines": [l.rstrip() for l in lines], "count": len(lines), "file": log_file}
    except Exception as e:
        return {"lines": [], "error": str(e)}


# -- Assets -----------------------------------------------------------------


@_register("list_assets")
def _cmd_list_assets(directory: str = "/Game/", recursive: bool = True, class_filter: str = "") -> dict:
    assets = unreal.EditorAssetLibrary.list_assets(directory, recursive=recursive)
    if class_filter:
        filtered = []
        for asset_path in assets:
            data = unreal.EditorAssetLibrary.find_asset_data(asset_path)
            if data is not None:
                cls = str(data.asset_class_path.asset_name) if hasattr(data, "asset_class_path") else str(getattr(data, "asset_class", ""))
                if cls == class_filter:
                    filtered.append(str(asset_path))
        assets = filtered
    else:
        assets = [str(a) for a in assets]
    return {"assets": assets, "count": len(assets)}


@_register("get_asset_info")
def _cmd_get_asset_info(asset_path: str) -> dict:
    data = unreal.EditorAssetLibrary.find_asset_data(asset_path)
    if data is None:
        raise ValueError(f"Asset not found: {asset_path}")
    return {"asset": _serialize(data)}


@_register("get_selected_assets")
def _cmd_get_selected_assets() -> dict:
    selected = unreal.EditorUtilityLibrary.get_selected_assets()
    return {
        "assets": [_serialize(a) for a in selected],
        "count": len(selected),
    }


@_register("rename_asset")
def _cmd_rename_asset(old_path: str, new_path: str) -> dict:
    success = unreal.EditorAssetLibrary.rename_asset(old_path, new_path)
    return {"success": success, "old_path": old_path, "new_path": new_path}


@_register("delete_asset")
def _cmd_delete_asset(asset_path: str) -> dict:
    success = unreal.EditorAssetLibrary.delete_asset(asset_path)
    return {"success": success, "asset_path": asset_path}


@_register("duplicate_asset")
def _cmd_duplicate_asset(source_path: str, dest_path: str) -> dict:
    result = unreal.EditorAssetLibrary.duplicate_asset(source_path, dest_path)
    return {"success": result is not None, "source": source_path, "dest": dest_path}


@_register("does_asset_exist")
def _cmd_does_asset_exist(asset_path: str) -> dict:
    exists = unreal.EditorAssetLibrary.does_asset_exist(asset_path)
    return {"exists": exists, "asset_path": asset_path}


@_register("save_asset")
def _cmd_save_asset(asset_path: str) -> dict:
    success = unreal.EditorAssetLibrary.save_asset(asset_path)
    return {"success": success, "asset_path": asset_path}


@_register("search_assets")
def _cmd_search_assets(class_name: str = "", directory: str = "/Game/", recursive: bool = True) -> dict:
    # UEFN doesn't allow setting ARFilter properties on instances.
    # Fall back to list_assets + filter by class.
    assets = unreal.EditorAssetLibrary.list_assets(directory, recursive=recursive)
    results = []
    for asset_path in assets:
        data = unreal.EditorAssetLibrary.find_asset_data(str(asset_path))
        if data is None:
            continue
        if class_name:
            cls = str(data.asset_class_path.asset_name) if hasattr(data, "asset_class_path") else str(getattr(data, "asset_class", ""))
            if cls != class_name:
                continue
        results.append(_serialize(data))
    return {"assets": results, "count": len(results)}


# -- Project -----------------------------------------------------------------


@_register("get_project_info")
def _cmd_get_project_info() -> dict:
    """Get project name and content root path."""
    world = unreal.EditorLevelLibrary.get_editor_world()
    project_name = ""
    content_root = ""
    if world:
        # World path is like /ProjectName/LevelName.LevelName
        parts = world.get_path_name().split("/")
        if len(parts) >= 2:
            project_name = parts[1]
            content_root = f"/{project_name}/"
    return {
        "project_name": project_name,
        "content_root": content_root,
        "project_dir": str(unreal.Paths.project_dir()),
    }


# -- Level -------------------------------------------------------------------


@_register("save_current_level")
def _cmd_save_current_level() -> dict:
    success = unreal.EditorLevelLibrary.save_current_level()
    return {"success": success}


@_register("get_level_info")
def _cmd_get_level_info() -> dict:
    world = unreal.EditorLevelLibrary.get_editor_world()
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = actor_sub.get_all_level_actors()
    return {
        "world_name": world.get_name() if world else "None",
        "actor_count": len(actors),
    }


# -- Viewport ----------------------------------------------------------------


@_register("get_viewport_camera")
def _cmd_get_viewport_camera() -> dict:
    loc, rot = unreal.EditorLevelLibrary.get_level_viewport_camera_info()
    return {"location": _serialize(loc), "rotation": _serialize(rot)}


@_register("set_viewport_camera")
def _cmd_set_viewport_camera(
    location: Optional[List[float]] = None,
    rotation: Optional[List[float]] = None,
) -> dict:
    cur_loc, cur_rot = unreal.EditorLevelLibrary.get_level_viewport_camera_info()
    loc = unreal.Vector(*location) if location else cur_loc
    rot = unreal.Rotator(*rotation) if rotation else cur_rot
    unreal.EditorLevelLibrary.set_level_viewport_camera_info(loc, rot)
    return {"location": _serialize(loc), "rotation": _serialize(rot)}


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------


class _MCPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for MCP commands."""

    def _send_json(self, code: int, body: bytes) -> None:
        """Send a JSON response, silently ignoring broken connections."""
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass  # client disconnected (e.g. heartbeat timeout) — safe to ignore

    def do_GET(self) -> None:
        """Health check and tool manifest."""
        _metrics["last_client_ping"] = time.time()
        body = json.dumps({
            "status": "ok",
            "version": PROTOCOL_VERSION,
            "port": unreal._mcp_bound_port,
            "commands": list(_HANDLERS.keys()),
        }).encode()
        self._send_json(200, body)

    def do_POST(self) -> None:
        """Execute a command."""
        _metrics["last_client_ping"] = time.time()
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            self._send_json(400, json.dumps({"success": False, "error": f"Invalid JSON: {e}"}).encode())
            return

        command = body.get("command", "")
        params = body.get("params", {})
        if not command:
            self._send_json(400, json.dumps({"success": False, "error": "Missing 'command' field"}).encode())
            return

        unreal._mcp_request_counter += 1
        req_id = f"req_{unreal._mcp_request_counter}_{time.time_ns()}"

        _command_queue.put((req_id, command, params))

        # Poll for result
        deadline = time.time() + HTTP_TIMEOUT_SEC
        while time.time() < deadline:
            with _responses_lock:
                if req_id in _responses:
                    result = _responses.pop(req_id)
                    break
            time.sleep(POLL_INTERVAL_SEC)
        else:
            self._send_json(504, json.dumps({"success": False, "error": f"Command '{command}' timed out"}).encode())
            return

        self._send_json(200, json.dumps(result).encode())

    def log_message(self, fmt: str, *args: Any) -> None:
        """Suppress default stderr logging."""
        pass


# ---------------------------------------------------------------------------
# Tick callback (main thread)
# ---------------------------------------------------------------------------


def _tick_handler(delta_time: float) -> None:
    """Process queued commands and main-thread tasks."""
    # Drain general-purpose main-thread queue
    while not _main_queue.empty():
        try:
            fn = _main_queue.get_nowait()
            fn()
        except queue.Empty:
            break
        except Exception as e:
            _log(f"Main-thread task error: {e}", "error")

    # Process MCP commands
    processed = 0
    while not _command_queue.empty() and processed < TICK_BATCH_LIMIT:
        try:
            req_id, command, params = _command_queue.get_nowait()
        except queue.Empty:
            break

        t0 = time.time()
        try:
            result = _dispatch(command, params)
            response = {"success": True, "result": result}
        except Exception as e:
            _log(f"Command '{command}' failed: {e}", "error")
            response = {"success": False, "error": str(e), "traceback": traceback.format_exc()}
            _metrics["total_errors"] += 1
            _metrics["last_error"] = str(e)

        elapsed_ms = (time.time() - t0) * 1000
        _metrics["total_requests"] += 1
        _metrics["last_request_at"] = time.time()
        _metrics["last_command"] = command
        _metrics["response_times_ms"].append(elapsed_ms)
        if len(_metrics["response_times_ms"]) > 100:
            _metrics["response_times_ms"].pop(0)

        with _responses_lock:
            _responses[req_id] = response
        processed += 1

    # Clean up stale responses
    now = time.time()
    with _responses_lock:
        stale = [k for k in _responses if float(k.split("_")[2]) / 1e9 < now - STALE_CLEANUP_SEC]
        for k in stale:
            del _responses[k]


# ---------------------------------------------------------------------------
# Shared tkinter root — one per process, all windows are Toplevel
# ---------------------------------------------------------------------------


def _get_tk_root() -> tk.Tk:
    """Return a tk.Tk root, reusing an pre-existing one if possible.

    Must be called from the tkinter thread only.
    All visible windows should use tk.Toplevel(root).
    """
    # Check if someone already created a Tk root in this process
    if hasattr(unreal, "_mcp_tk_root") and unreal._mcp_tk_root is not None:
        try:
            unreal._mcp_tk_root.winfo_exists()
            return unreal._mcp_tk_root
        except Exception:
            unreal._mcp_tk_root = None

    # Try to find an existing Tk instance (created by another script)
    try:
        existing = tk._default_root  # noqa: SLF001 — tkinter internal
        if existing is not None and existing.winfo_exists():
            unreal._mcp_tk_root = existing
            return existing
    except Exception:
        pass

    # No root exists — create a hidden one
    root = tk.Tk()
    root.withdraw()
    unreal._mcp_tk_root = root
    return root


# ---------------------------------------------------------------------------
# Status window (tkinter)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Remote tunnel (Cloudflare + mcp_http_server). Never tunnels this listener.
# ---------------------------------------------------------------------------

_CREATE_NO_WINDOW = 0x08000000


def _mcp_tools_dir() -> str:
    env = os.environ.get("UEFN_MCP_TOOLS", "").strip()
    if env and os.path.isdir(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isfile(os.path.join(here, "start_remote_mcp.py")):
        return here
    desktop = os.path.join(os.path.expanduser("~"), "Desktop", "UEFN_Fun", "tools", "uefn-mcp-server")
    if os.path.isdir(desktop):
        return desktop
    return here


def _host_python() -> str:
    local = os.path.join(
        os.path.expanduser("~"), "AppData", "Local", "Programs", "Python", "Python312", "python.exe"
    )
    if os.path.isfile(local):
        return local
    return "python"


def _hidden_popen(args, cwd=None, capture=False):
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    kw = dict(cwd=cwd, startupinfo=si, creationflags=_CREATE_NO_WINDOW)
    if capture:
        kw["stdout"] = subprocess.DEVNULL
        kw["stderr"] = subprocess.DEVNULL
    return subprocess.Popen(args, **kw)


def _read_tunnel_status() -> dict:
    path = os.path.join(_mcp_tools_dir(), ".tunnel_status.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
    except Exception:
        pass
    return False


def _tunnel_running() -> bool:
    st = _read_tunnel_status()
    if st.get("enabled") and st.get("url"):
        pid = st.get("pid")
        if pid is None or _pid_alive(pid):
            return True
    return False


def _start_remote_tunnel() -> None:
    tools = _mcp_tools_dir()
    script = os.path.join(tools, "start_remote_mcp.py")
    if not os.path.isfile(script):
        _log(f"start_remote_mcp.py not found in {tools}", "error")
        return
    if _tunnel_running():
        return
    _hidden_popen([_host_python(), script, "--port", "8799"], cwd=tools, capture=True)
    _log("Remote tunnel starting (8799 only; listener stays local)")


def _stop_remote_tunnel() -> None:
    st = _read_tunnel_status()
    pid = st.get("pid")
    if pid and _pid_alive(pid):
        _hidden_popen(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture=True)
    status_path = os.path.join(_mcp_tools_dir(), ".tunnel_status.json")
    try:
        with open(status_path, "w", encoding="utf-8") as fh:
            json.dump({
                "enabled": False,
                "url": None,
                "public_host": None,
                "port": 8799,
                "pid": None,
                "listener_not_tunneled": True,
                "error": None,
            }, fh, indent=2)
    except OSError:
        pass
    _log("Remote tunnel stopped")


class MCPStatusWindow:
    """Status window for the MCP listener — dark card UI with live activity."""

    BG = "#0b0e14"
    CARD = "#12161f"
    EDGE = "#1e2431"
    FG = "#e8ebf2"
    DIM = "#8b93a7"
    FAINT = "#5a6275"
    GREEN = "#3ddc84"
    RED = "#ff5c5c"
    YELLOW = "#ffcf5c"
    ACCENT = "#4ea1ff"
    BTN = "#1a2029"
    BTN_HOVER = "#242c38"
    FONT = ("Segoe UI", 9)
    FONT_SMALL = ("Segoe UI", 8)
    FONT_LABEL = ("Segoe UI", 7)
    FONT_BOLD = ("Segoe UI", 9, "bold")
    FONT_TITLE = ("Segoe UI", 13, "bold")
    FONT_VALUE = ("Segoe UI", 11, "bold")
    UPDATE_MS = 1000

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._window: Optional[tk.Toplevel] = None
        self._labels: Dict[str, tk.Label] = {}
        self._listener_dot = None
        self._listener_text: Optional[tk.Label] = None
        self._client_dot = None
        self._client_text: Optional[tk.Label] = None
        self._btn_toggle: Optional[tk.Button] = None
        self._port_var: Optional[tk.StringVar] = None
        self._port_entry: Optional[tk.Entry] = None
        self._spark: Optional[tk.Canvas] = None
        self._tunnel_dot = None
        self._tunnel_text: Optional[tk.Label] = None
        self._btn_tunnel: Optional[tk.Button] = None

    def start(self) -> None:
        """Open the status window in a background thread."""
        if self._thread and self._thread.is_alive() and self._window is not None:
            try:
                self._window.lift()
                self._window.focus_force()
            except Exception:
                pass
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        root = _get_tk_root()
        self._create_window()
        root.mainloop()

    # -- building blocks -----------------------------------------------------

    def _card(self, parent):
        """A flat card: 1px edge frame wrapping a fill frame."""
        outer = tk.Frame(parent, bg=self.EDGE)
        inner = tk.Frame(outer, bg=self.CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return outer, inner

    def _dot(self, parent, color):
        c = tk.Canvas(parent, width=11, height=11, bg=self.CARD, highlightthickness=0)
        item = c.create_oval(2, 2, 9, 9, fill=color, outline="")
        return (c, item)

    def _set_dot(self, dot, color) -> None:
        if dot:
            dot[0].itemconfig(dot[1], fill=color)

    def _button(self, parent, text, command):
        b = tk.Button(
            parent, text=text, command=command, bg=self.BTN, fg=self.FG,
            activebackground=self.BTN_HOVER, activeforeground=self.FG,
            relief="flat", bd=0, font=self.FONT, padx=14, pady=5, cursor="hand2",
        )
        b.bind("<Enter>", lambda _e: b.configure(bg=self.BTN_HOVER))
        b.bind("<Leave>", lambda _e: b.configure(bg=self.BTN))
        return b

    # -- window --------------------------------------------------------------

    def _create_window(self) -> None:
        """Build the Toplevel status window. Safe to call multiple times."""
        root = getattr(unreal, "_mcp_tk_root", None)
        if root is None:
            return

        window = tk.Toplevel(root)
        self._window = window
        self._labels = {}
        window.title("UEFN MCP Listener")
        window.geometry("292x420")
        window.attributes("-topmost", True)
        window.configure(bg=self.BG)
        window.resizable(False, False)

        # -- header --
        head = tk.Frame(window, bg=self.BG)
        head.pack(fill="x", padx=14, pady=(12, 9))
        logo = tk.Canvas(head, width=18, height=18, bg=self.BG, highlightthickness=0)
        logo.create_oval(3, 3, 15, 15, outline=self.ACCENT, width=2)
        logo.create_oval(7, 7, 11, 11, fill=self.ACCENT, outline="")
        logo.pack(side="left")
        tk.Label(head, text="UEFN MCP", font=self.FONT_TITLE, fg=self.FG, bg=self.BG).pack(
            side="left", padx=(8, 0)
        )
        tk.Label(head, text=f"v{PROTOCOL_VERSION}", font=self.FONT_SMALL, fg=self.FAINT,
                 bg=self.BG).pack(side="right")

        # -- status card --
        outer, card = self._card(window)
        outer.pack(fill="x", padx=14, pady=(0, 8))
        row1 = tk.Frame(card, bg=self.CARD)
        row1.pack(fill="x", padx=12, pady=(10, 4))
        self._listener_dot = self._dot(row1, self.GREEN)
        self._listener_dot[0].pack(side="left")
        self._listener_text = tk.Label(row1, text="Listening", font=self.FONT_BOLD,
                                       fg=self.FG, bg=self.CARD)
        self._listener_text.pack(side="left", padx=(7, 0))
        self._port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self._port_entry = tk.Entry(
            row1, textvariable=self._port_var, font=self.FONT, width=6,
            bg=self.BG, fg=self.DIM, insertbackground=self.FG,
            disabledbackground=self.CARD, disabledforeground=self.DIM,
            relief="flat", justify="right", state="disabled",
        )
        self._port_entry.pack(side="right")
        tk.Label(row1, text="PORT", font=self.FONT_LABEL, fg=self.FAINT,
                 bg=self.CARD).pack(side="right", padx=(0, 4))

        row2 = tk.Frame(card, bg=self.CARD)
        row2.pack(fill="x", padx=12, pady=(0, 10))
        self._client_dot = self._dot(row2, self.FAINT)
        self._client_dot[0].pack(side="left")
        self._client_text = tk.Label(row2, text="Waiting for a client...", font=self.FONT,
                                     fg=self.DIM, bg=self.CARD)
        self._client_text.pack(side="left", padx=(7, 0))

        row3 = tk.Frame(card, bg=self.CARD)
        row3.pack(fill="x", padx=12, pady=(0, 10))
        self._tunnel_dot = self._dot(row3, self.FAINT)
        self._tunnel_dot[0].pack(side="left")
        self._tunnel_text = tk.Label(row3, text="Tunnel off", font=self.FONT,
                                    fg=self.DIM, bg=self.CARD)
        self._tunnel_text.pack(side="left", padx=(7, 0))
        self._btn_tunnel = self._button(row3, "Enable", self._on_tunnel_toggle)
        self._btn_tunnel.pack(side="right")

        # -- metrics card (2x2) --
        outer2, grid = self._card(window)
        outer2.pack(fill="x", padx=14, pady=(0, 8))
        cells = [("UPTIME", "uptime"), ("REQUESTS", "requests"),
                 ("ERRORS", "errors"), ("AVG REPLY", "avg_time")]
        for i, (label_text, key) in enumerate(cells):
            cell = tk.Frame(grid, bg=self.CARD)
            cell.grid(row=i // 2, column=i % 2, sticky="w", padx=(14, 4),
                      pady=(10 if i < 2 else 4, 10 if i >= 2 else 4))
            val = tk.Label(cell, text="—", font=self.FONT_VALUE, fg=self.FG,
                           bg=self.CARD, anchor="w")
            val.pack(anchor="w")
            tk.Label(cell, text=label_text, font=self.FONT_LABEL, fg=self.FAINT,
                     bg=self.CARD).pack(anchor="w")
            self._labels[key] = val
        grid.columnconfigure(0, weight=1, minsize=128)
        grid.columnconfigure(1, weight=1)

        # -- activity sparkline --
        outer3, spark_holder = self._card(window)
        outer3.pack(fill="x", padx=14, pady=(0, 8))
        tk.Label(spark_holder, text="ACTIVITY", font=self.FONT_LABEL, fg=self.FAINT,
                 bg=self.CARD).pack(anchor="w", padx=12, pady=(8, 0))
        self._spark = tk.Canvas(spark_holder, height=30, bg=self.CARD, highlightthickness=0)
        self._spark.pack(fill="x", padx=12, pady=(2, 10))

        # -- buttons --
        btns = tk.Frame(window, bg=self.BG)
        btns.pack(fill="x", padx=14, pady=(0, 4))
        self._btn_toggle = self._button(btns, "Stop", self._on_toggle)
        self._btn_toggle.pack(side="left", expand=True, fill="x")
        self._button(btns, "Restart", self._on_restart).pack(
            side="left", expand=True, fill="x", padx=(8, 0)
        )

        tk.Label(window, text="Safe to close — the listener keeps running",
                 font=self.FONT_SMALL, fg=self.FAINT, bg=self.BG).pack(pady=(3, 9))

        self._update()
        window.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- refresh loop --------------------------------------------------------

    def _update(self) -> None:
        if not self._window:
            return

        running = unreal._mcp_server is not None

        self._set_dot(self._listener_dot, self.GREEN if running else self.RED)
        if self._listener_text:
            self._listener_text.configure(text="Listening" if running else "Stopped")
        if self._btn_toggle:
            self._btn_toggle.configure(text="Stop" if running else "Start")

        # client heartbeat
        last_ping = _metrics.get("last_client_ping", 0.0)
        if self._client_text:
            if last_ping > 0:
                ago = int(time.time() - last_ping)
                if ago < 15:
                    self._set_dot(self._client_dot, self.GREEN)
                    self._client_text.configure(text="Client connected", fg=self.FG)
                else:
                    if ago < 60:
                        ago_str = f"{ago}s"
                    elif ago < 3600:
                        ago_str = f"{ago // 60}m"
                    else:
                        ago_str = f"{ago // 3600}h"
                    self._set_dot(self._client_dot, self.FAINT)
                    self._client_text.configure(text=f"Client lost {ago_str} ago", fg=self.DIM)
            elif running:
                self._set_dot(self._client_dot, self.YELLOW)
                self._client_text.configure(text="Waiting for a client...", fg=self.DIM)
            else:
                self._set_dot(self._client_dot, self.FAINT)
                self._client_text.configure(text="Not connected", fg=self.DIM)

        # port entry: editable when stopped, locked when running
        if self._port_entry:
            if running:
                self._port_entry.configure(state="disabled")
                self._port_var.set(str(unreal._mcp_bound_port))
            else:
                self._port_entry.configure(state="normal")

        # metrics
        if running and _metrics["started_at"] > 0:
            uptime = int(time.time() - _metrics["started_at"])
            h, rem = divmod(uptime, 3600)
            m, s = divmod(rem, 60)
            self._labels["uptime"].configure(text=f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s")
        else:
            self._labels["uptime"].configure(text="—")

        self._labels["requests"].configure(text=str(_metrics["total_requests"]))

        errs = _metrics["total_errors"]
        self._labels["errors"].configure(text=str(errs), fg=self.RED if errs > 0 else self.FG)

        times = _metrics["response_times_ms"]
        if times:
            self._labels["avg_time"].configure(text=f"{sum(times) / len(times):.0f} ms")
        else:
            self._labels["avg_time"].configure(text="—")

        # remote tunnel (host Cloudflare + HTTP MCP)
        st = _read_tunnel_status()
        tun_on = bool(st.get("enabled") and st.get("url"))
        if self._tunnel_text:
            if tun_on:
                self._set_dot(self._tunnel_dot, self.GREEN)
                self._tunnel_text.configure(text="Tunnel on", fg=self.FG)
            elif st.get("error") == "starting" or (st.get("enabled") and not st.get("url")):
                self._set_dot(self._tunnel_dot, self.YELLOW)
                self._tunnel_text.configure(text="Tunnel starting…", fg=self.DIM)
            else:
                self._set_dot(self._tunnel_dot, self.FAINT)
                self._tunnel_text.configure(text="Tunnel off", fg=self.DIM)
        if self._btn_tunnel:
            self._btn_tunnel.configure(text="Disable" if tun_on or st.get("enabled") else "Enable")

        # sparkline: most recent response times as bars, right-aligned
        if self._spark:
            self._spark.delete("all")
            w = self._spark.winfo_width()
            if w < 20:
                w = 248
            h = 30
            data = times[-48:]
            if data:
                peak = max(data) or 1.0
                bar_w = 3
                gap = 2
                x = w - len(data) * (bar_w + gap)
                if x < 0:
                    x = 0
                for v in data:
                    bh = max(2, int((v / peak) * (h - 4)))
                    self._spark.create_rectangle(x, h - bh, x + bar_w, h,
                                                 fill=self.ACCENT, outline="")
                    x += bar_w + gap
            else:
                self._spark.create_line(0, h - 2, w, h - 2, fill=self.EDGE)

        self._window.after(self.UPDATE_MS, self._update)

    # -- actions -------------------------------------------------------------

    def _on_tunnel_toggle(self) -> None:
        st = _read_tunnel_status()
        if st.get("enabled"):
            threading.Thread(target=_stop_remote_tunnel, daemon=True).start()
        else:
            threading.Thread(target=_start_remote_tunnel, daemon=True).start()

    def _on_toggle(self) -> None:
        if unreal._mcp_server is not None:
            _run_on_main_thread(stop_listener)
        else:
            try:
                port = int(self._port_var.get())
            except (ValueError, TypeError):
                port = 0
            _run_on_main_thread(lambda: start_listener(port=port, show_status=False))

    def _on_restart(self) -> None:
        _run_on_main_thread(restart_listener)

    def _on_close(self) -> None:
        if self._window:
            self._window.destroy()
            self._window = None


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------


class _MCPServer(HTTPServer):
    """HTTPServer WITHOUT allow_reuse_address.

    On Windows, SO_REUSEADDR lets a new socket bind a port that another socket
    is ACTIVELY listening on — the OS then routes connections to the original
    (possibly dead) socket and they get refused. HTTPServer enables it by
    default; disabling it makes a genuinely-occupied port fail loudly instead
    of silently stacking dead listeners. (This exact failure wedged the
    listener on 2026-07-21.)
    """
    allow_reuse_address = False


def _find_free_port() -> int:
    """Find a free port in the configured range.

    NOTE: no SO_REUSEADDR on the probe — with it, on Windows, the bind test
    "succeeds" even when the port is actively held, defeating the check.
    """
    for port in range(DEFAULT_PORT, MAX_PORT + 1):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            continue
    raise RuntimeError(f"No free port in range {DEFAULT_PORT}-{MAX_PORT}")


def start_listener(port: int = 0, show_status: bool = True) -> int:
    """Start the MCP listener. Returns the bound port.

    Args:
        port: Port to bind to. 0 = auto-detect free port.
        show_status: Open the status window.
    """
    if unreal._mcp_server is not None:
        thread = unreal._mcp_server_thread
        if thread is not None and thread.is_alive():
            _log(f"Listener already running on port {unreal._mcp_bound_port}", "warning")
            if show_status and unreal._mcp_status_window:
                unreal._mcp_status_window.start()
            return unreal._mcp_bound_port
        # ZOMBIE: server object exists but its serve thread is dead (e.g. killed
        # by sleep/hibernate). Force-clean so we fall through to a fresh start.
        # Do NOT call shutdown() here — on a dead loop it blocks forever.
        _log("Stale listener detected (dead serve thread) — force-cleaning", "warning")
        try:
            unreal._mcp_server.server_close()
        except Exception:
            pass
        unreal._mcp_server = None
        unreal._mcp_server_thread = None
        unreal._mcp_bound_port = 0

    if port == 0:
        port = _find_free_port()

    unreal._mcp_server = _MCPServer(("127.0.0.1", port), _MCPHandler)
    unreal._mcp_bound_port = port

    unreal._mcp_server_thread = threading.Thread(
        target=unreal._mcp_server.serve_forever, daemon=True,
    )
    unreal._mcp_server_thread.start()

    if unreal._mcp_tick_handle is None:
        unreal._mcp_tick_handle = unreal.register_slate_post_tick_callback(_tick_handler)

    _metrics["started_at"] = time.time()

    _log(f"Listener started on http://127.0.0.1:{port}")
    _log(f"Registered {len(_HANDLERS)} command handlers")

    if show_status:
        win = unreal._mcp_status_window
        # Reuse only if thread alive AND window visible
        if win is not None and win.is_alive() and getattr(win, "_window", None) is not None:
            win.start()
        else:
            # Create fresh window
            unreal._mcp_status_window = MCPStatusWindow()
            unreal._mcp_status_window.start()

    return port


def stop_listener() -> None:
    """Stop the HTTP server. The tick callback stays alive for _main_queue."""
    if unreal._mcp_server is None:
        _log("Listener is not running", "warning")
        return

    # shutdown() waits on the serve loop to acknowledge — on a DEAD loop it
    # blocks forever (this hung the Restart button on 2026-07-21). Only call it
    # when the serve thread is actually alive; server_close() below always
    # frees the socket either way.
    thread = unreal._mcp_server_thread
    if thread is not None and thread.is_alive():
        unreal._mcp_server.shutdown()
        thread.join(timeout=3.0)
    try:
        unreal._mcp_server.server_close()
    except Exception:
        pass

    unreal._mcp_server = None
    unreal._mcp_server_thread = None
    _log(f"Listener stopped (was on port {unreal._mcp_bound_port})")
    unreal._mcp_bound_port = 0
    _metrics["started_at"] = 0.0
    _metrics["last_client_ping"] = 0.0


def cleanup() -> None:
    """Full cleanup: stop listener AND unregister tick callback."""
    stop_listener()
    if unreal._mcp_tick_handle is not None:
        unreal.unregister_slate_post_tick_callback(unreal._mcp_tick_handle)
        unreal._mcp_tick_handle = None


def restart_listener(port: int = 0) -> int:
    """Restart the MCP listener."""
    stop_listener()
    time.sleep(0.5)
    return start_listener(port, show_status=False)


# ---------------------------------------------------------------------------
# Auto-start when script is executed directly
# ---------------------------------------------------------------------------

try:
    # If a previous HTTP server exists, close its socket to free the port.
    if unreal._mcp_server is not None:
        _log("Previous listener detected — replacing")
        try:
            unreal._mcp_server.server_close()
        except Exception:
            pass
        unreal._mcp_server = None
        unreal._mcp_server_thread = None
        unreal._mcp_bound_port = 0

    # Unregister old tick handle so we don't get duplicates
    _old_tick = unreal._mcp_tick_handle
    if _old_tick is not None:
        unreal.unregister_slate_post_tick_callback(_old_tick)
        unreal._mcp_tick_handle = None

    # NEVER touch the old tkinter window — two tk.Tk() crashes tcl.
    # If the old window is still alive, start_listener will reuse it.
    start_listener()
except Exception as _e:
    unreal.log_error(f"[MCP] Failed to start listener: {_e}")
    import traceback
    traceback.print_exc()
