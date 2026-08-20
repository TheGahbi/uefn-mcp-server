"""Bridge to Epic's FIRST-PARTY UEFN MCP server (UE 6.0 / UEFN, 2026-08).

UEFN now ships its own MCP server in the `AIAssistant` experimental plugin. It listens
on plain HTTP and speaks JSON-RPC 2.0:

    POST http://127.0.0.1:8000/mcp        protocolVersion 2025-06-18

Discovery notes that cost real time (keep them):
  * UEFN opens ports 1962, 1963, 8000, 23430. Only 8000 serves /mcp.
  * GET /mcp returns 405, not 404 -- the route exists, it just wants POST. A 404 on "/"
    is normal and does NOT mean the server is missing.
  * An `Mcp-Session-Id` header is REQUIRED for every call after `initialize`. Read it from
    the initialize RESPONSE HEADERS, then send `notifications/initialized`.
  * It exposes only three tools -- list_toolsets / describe_toolset / call_tool -- as a
    gateway over 29 toolsets / 384 tools, so the client's tool list stays small.
  * `describe_toolset` takes `toolset_name` (NOT `toolset`).
  * `call_tool` takes `toolset_name` + `tool_name` (WITHOUT the toolset prefix) + `arguments`.

Why bridge instead of replace: Epic's server is typed, validated and maintained, but it
does NOT expose every toolset in the registry (no SlateInspector, PCG, Plugin,
AIAssistant, AgentSkill, GameFeatures, ...), and it has no arbitrary-code escape hatch.
`execute_python` still reaches strictly more surface. Use Epic's where it covers the job.
"""

import json
import urllib.error
import urllib.request

EPIC_MCP_URL = "http://127.0.0.1:8000/mcp"
_session_id = None


def _post(payload, session_id=None, timeout=180.0):
    req = urllib.request.Request(
        EPIC_MCP_URL, data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if session_id:
        req.add_header("Mcp-Session-Id", session_id)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.headers, resp.read().decode("utf-8", "replace")


def session(force=False):
    """Return a live Mcp-Session-Id, creating one if needed."""
    global _session_id
    if _session_id and not force:
        return _session_id
    headers, _ = _post({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "uefn-mcp-bridge", "version": "1.0"},
        },
    })
    _session_id = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
    # Required by spec; Epic's server tolerates its absence but be correct.
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, _session_id)
    return _session_id


def is_available():
    """True if Epic's MCP answers an initialize handshake."""
    try:
        return bool(session(force=True))
    except Exception:
        return False


def _unwrap(body):
    d = json.loads(body)
    if "error" in d:
        return {"__error__": d["error"]}
    text = "".join(c.get("text", "") for c in d.get("result", {}).get("content", []))
    try:
        return json.loads(text)
    except Exception:
        return text


def _rpc(method, params, retry=True):
    try:
        _, body = _post({"jsonrpc": "2.0", "id": 7, "method": method, "params": params},
                        session())
    except urllib.error.HTTPError:
        if not retry:
            raise
        # Session expires; one silent re-handshake then give up.
        _, body = _post({"jsonrpc": "2.0", "id": 7, "method": method, "params": params},
                        session(force=True))
    return _unwrap(body)


def list_toolsets():
    return _rpc("tools/call", {"name": "list_toolsets", "arguments": {}})


def describe_toolset(toolset_name):
    return _rpc("tools/call", {"name": "describe_toolset",
                               "arguments": {"toolset_name": toolset_name}})


def call(full_tool_name, arguments=None):
    """Call an Epic toolset tool by its FULL dotted name.

    e.g. call("ValkyrieToolset.VerseToolset.BuildAll")
         call("ValkyrieToolset.EntityToolset.GetComponents", {"entity": {"refPath": "..."}})
    """
    toolset, _, tool = full_tool_name.rpartition(".")
    return _rpc("tools/call", {
        "name": "call_tool",
        "arguments": {"toolset_name": toolset, "tool_name": tool,
                      "arguments": arguments or {}},
    })


def build_verse():
    """Compile all Verse. Returns a list of structured diagnostics (empty == success).

    Each diagnostic: severity (Error|Warning|Information|Hint), code, message, filePath,
    span{startLine,startCharacter,endLine,endCharacter}.
    """
    return call("ValkyrieToolset.VerseToolset.BuildAll")


# ---------------------------------------------------------------------------
# Convenience wrappers over the toolsets that used to require hand-rolled hacks
# ---------------------------------------------------------------------------

ENTITY_CLASS = "/EntityFramework/_Verse/VNI/Entity.entity"


def _ref(path):
    return {"refPath": path}


def xform(location=None, rotation=None, scale=None):
    """Build a ToolsetTransform. Unset fields mean identity (create) / unchanged (modify)."""
    t = {}
    if location: t["location"] = {"x": location[0], "y": location[1], "z": location[2]}
    if rotation: t["rotation"] = {"pitch": rotation[0], "yaw": rotation[1], "roll": rotation[2]}
    if scale:    t["scale"]    = {"x": scale[0], "y": scale[1], "z": scale[2]}
    return t


# -- Verse files -------------------------------------------------------------
# NOTE: paths are Verse MODULE paths (/MyProject/Folder/file.verse), NOT filesystem
# paths. ListFiles(path="", bRecursive=False) shows the mounted roots.

def verse_list(path="", recursive=False):
    return call("ValkyrieToolset.VerseToolset.ListFiles",
                {"path": path, "bRecursive": recursive})


def verse_read(path):
    return call("ValkyrieToolset.VerseToolset.ReadFile", {"path": path})


def verse_write(path, content, create=True):
    return call("ValkyrieToolset.VerseToolset.WriteFile",
                {"path": path, "content": content, "bCreateIfMissing": create})


def verse_replace(path, old, new, all_occurrences=False):
    return call("ValkyrieToolset.VerseToolset.Replace",
                {"path": path, "oldString": old, "newString": new,
                 "bReplaceAll": all_occurrences})


def verse_grep(pattern, path=""):
    return call("ValkyrieToolset.VerseToolset.Grep", {"pattern": pattern, "path": path})


# -- Entities ----------------------------------------------------------------

def entity_find(recursive=True, name_filter=""):
    return call("ValkyrieToolset.EntityToolset.FindEntities",
                {"bRecursive": recursive, "nameFilter": name_filter})


def entity_create(name, location=None, rotation=None, scale=None,
                  entity_class=ENTITY_CLASS):
    """Create an entity WITH its transform in one call.

    ☠ Do NOT pass EntityLevel.level_entity as entity_class -- it becomes the level
    root and DeleteEntity then refuses forever. The default Entity.entity is right.
    ☠ /Script/Entity.BaseEntity is abstract and will be rejected.
    """
    return call("ValkyrieToolset.EntityToolset.CreateEntity",
                {"entityClass": _ref(entity_class), "name": name,
                 "transform": xform(location, rotation, scale)})


def entity_add_component(entity_ref, component_class):
    return call("ValkyrieToolset.EntityToolset.AddComponent",
                {"entity": entity_ref, "componentClass": _ref(component_class)})


def entity_components(entity_ref):
    return call("ValkyrieToolset.EntityToolset.GetComponents", {"entity": entity_ref})


def entity_transform(entity_ref):
    return call("ValkyrieToolset.EntityToolset.GetEntityTransform", {"entity": entity_ref})


def entity_set_transform(entity_ref, location=None, rotation=None, scale=None):
    return call("ValkyrieToolset.EntityToolset.SetEntityTransform",
                {"entity": entity_ref, "transform": xform(location, rotation, scale)})


def entity_component_classes(name_filter=""):
    return call("ValkyrieToolset.EntityToolset.ListComponentClasses",
                {"nameFilter": name_filter})


def entity_delete(entity_ref, display_name=None):
    """Delete an entity, falling back to its EntityProxyActor for ROOT entities.

    ★ DeleteEntity refuses root entities with "Cannot delete the root entity." The
    working escape hatch (proven 2026-08-20) is to remove the backing actor via
    SceneTools.remove_from_scene. Pass display_name so we can find that actor.
    """
    r = call("ValkyrieToolset.EntityToolset.DeleteEntity", {"entity": entity_ref})
    if not (isinstance(r, str) and "root entity" in r.lower()):
        return r
    if not display_name:
        return {"__error__": "root entity; pass display_name to use the proxy-actor fallback"}
    actors = call("editor_toolset.toolsets.scene.SceneTools.find_actors",
                  {"collision_channels": []})
    for a in (actors.get("returnValue", []) if isinstance(actors, dict) else []):
        if a.get("class", {}).get("refPath", "").endswith("EntityProxyActor") \
                and a.get("label") == display_name:
            return call("editor_toolset.toolsets.scene.SceneTools.remove_from_scene",
                        {"actor": _ref(a["actorPath"])})
    return {"__error__": f"no EntityProxyActor labelled {display_name!r}"}


# -- Session / playtest ------------------------------------------------------

def session_start():   return call("ValkyrieToolset.SessionToolset.StartSession")
def session_stop():    return call("ValkyrieToolset.SessionToolset.StopSession")
def game_start():      return call("ValkyrieToolset.SessionToolset.StartGame")
def game_stop():       return call("ValkyrieToolset.SessionToolset.StopGame")
def session_push():    return call("ValkyrieToolset.SessionToolset.PushChanges")
def session_status():  return call("ValkyrieToolset.SessionToolset.GetSessionStatus")
def game_state():      return call("ValkyrieToolset.SessionToolset.GetGameState")
def client_logs():     return call("ValkyrieToolset.SessionToolset.GetClientLogEntries")


# -- Devices (was: T3D clipboard hacks) --------------------------------------

def device_list_assets(name_filter=""):
    return call("ValkyrieToolset.DeviceToolset.ListDeviceAssets", {"nameFilter": name_filter})


def device_place(asset_path, location=None, rotation=None, scale=None):
    return call("ValkyrieToolset.DeviceToolset.PlaceDevice",
                {"assetPath": asset_path, "transform": xform(location, rotation, scale)})


def device_list_properties(device_path):
    return call("ValkyrieToolset.DeviceToolset.ListDeviceProperties",
                {"device": _ref(device_path)})


def device_set_property(device_path, name, value):
    return call("ValkyrieToolset.DeviceToolset.SetDeviceProperty",
                {"device": _ref(device_path), "propertyName": name, "value": value})


def device_binding_options(device_path):
    return call("ValkyrieToolset.DeviceToolset.GetBindingOptions", {"devicePath": device_path})


def device_add_event_binding(source_path, source_event, target_path, target_function):
    return call("ValkyrieToolset.DeviceToolset.AddEventBinding",
                {"sourceDevicePath": source_path, "sourceEvent": source_event,
                 "targetDevicePath": target_path, "targetFunction": target_function})


# -- Verse Fields (was: ctypes patch at descriptor offset 200) ---------------

def verse_field_list(widget_blueprint):
    return call("VerseFieldsToolset.VerseFieldsToolset.ListVerseFields",
                {"widgetBlueprint": _ref(widget_blueprint)})


def verse_field_add(widget_blueprint, field_name, field_type, default_value="",
                    visibility="public", write_access="public", is_var=True):
    return call("VerseFieldsToolset.VerseFieldsToolset.AddVerseField",
                {"widgetBlueprint": _ref(widget_blueprint), "fieldName": field_name,
                 "fieldType": field_type, "defaultValue": default_value,
                 "visibility": visibility, "writeAccess": write_access, "bIsVar": is_var})


def verse_field_bind_widget(widget_blueprint, verse_field_name, target_widget,
                            widget_property_path, mode="", conversion_name=""):
    """Bind a widget property to a Verse field -- including via a CONVERSION function.

    conversion_name is what our hand-rolled path needed a ctypes GraphName patch for.
    Discover valid names with mvvm_list_conversion_functions().
    """
    args = {"widgetBlueprint": _ref(widget_blueprint), "verseFieldName": verse_field_name,
            "targetWidget": target_widget, "widgetPropertyPath": widget_property_path}
    if mode: args["mode"] = mode
    if conversion_name: args["conversionName"] = conversion_name
    return call("VerseFieldsToolset.VerseFieldsToolset.BindWidgetPropertyToVerseField", args)


# -- MVVM (was: authoring the object + ctypes GraphName at offsets 256/264) --

def mvvm_list_bindings(widget_blueprint):
    return call("MVVMToolset.MVVMToolset.ListWidgetViewBindings",
                {"widgetBlueprint": _ref(widget_blueprint)})


def mvvm_list_conversion_functions(widget_blueprint):
    return call("MVVMToolset.MVVMToolset.ListConversionFunctions",
                {"widgetBlueprint": _ref(widget_blueprint)})


def mvvm_create_binding(widget_blueprint, source_context, source_path,
                        destination_context, destination_path, conversion_name=""):
    return call("MVVMToolset.MVVMToolset.CreateViewBinding",
                {"widgetBlueprint": _ref(widget_blueprint),
                 "sourceContext": source_context, "sourcePropertyPath": source_path,
                 "destinationContext": destination_context,
                 "destinationPropertyPath": destination_path,
                 "conversionName": conversion_name})


def mvvm_add_viewmodel(widget_blueprint, viewmodel_class):
    return call("MVVMToolset.MVVMToolset.AddViewModelToWidget",
                {"widgetBlueprint": _ref(widget_blueprint),
                 "viewModelClass": _ref(viewmodel_class)})


def mvvm_fixup(widget_blueprint):
    """Regenerate binding graphs to match stored binding data. Repairs broken MVVM state."""
    return call("MVVMToolset.MVVMToolset.FixupMVVMData",
                {"widgetBlueprint": _ref(widget_blueprint)})


# -- Widget animations (was: "binding creation needs the Sequencer UI") ------

def widget_anim_list(widget_blueprint):
    return call("WidgetAnimationToolset.WidgetAnimationToolset.ListWidgetAnimations",
                {"widgetBlueprint": _ref(widget_blueprint)})


def widget_anim_create(widget_blueprint, animation_name, length_seconds=None):
    args = {"widgetBlueprint": _ref(widget_blueprint), "animationName": animation_name}
    if length_seconds is not None:
        args["lengthSeconds"] = length_seconds
    return call("WidgetAnimationToolset.WidgetAnimationToolset.CreateWidgetAnimation", args)


def widget_anim_add_widget(widget_blueprint, animation, object_to_bind):
    """Bind a widget INTO an animation -- the step we previously believed was UI-only."""
    return call("WidgetAnimationToolset.WidgetAnimationToolset.AddWidgetToAnimation",
                {"widgetBlueprint": _ref(widget_blueprint), "animation": _ref(animation),
                 "objectToBind": object_to_bind})


def widget_anim_bindings(animation):
    return call("WidgetAnimationToolset.WidgetAnimationToolset.GetWidgetAnimationBindings",
                {"animation": _ref(animation)})
