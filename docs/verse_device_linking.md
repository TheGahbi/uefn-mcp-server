# Linking level devices to Verse `@editable` slots — fully scripted

UEFN protects the properties that store verse-device wiring, so naive
`set_editor_property` calls fail. This guide documents the working paths,
discovered by reading the editor's own T3D serialization (which ignores
scripting protection). Everything below runs through `execute_python`.

## The anatomy of a placed verse device

```
VerseDevice_C actor
 ├─ ScriptClassPath = "/<Project>/_Verse.<Folder>-<class_name>"      (protected)
 ├─ Script ──► instance subobject "<Folder>-<class_name>_0"          (the live verse object)
 │              ├─ __verse_0xHASH_SlotName ──► proxy subobject       (one per @editable)
 │              │      └─ SavedActor ──► the linked LEVEL DEVICE     ← the actual link
```

Key facts:

- Verse classes compiled from `Content/<Folder>/x.verse` are named `<Folder>-x`
  in the project's `_Verse` package (e.g. `System-my_manager`).
- Each `@editable` becomes a hashed property (`__verse_0x6617CCAB_Pad01`) on the
  instance. The hash is not guessable — harvest the names (below).
- The per-slot **proxy objects and their `SavedActor` property are NOT protected.**
  That is the back door.
- An unset slot still has a proxy; its `SavedActor` is `None`.
- Array editables serialize as `__verse_0xHASH_Name(0)="<type>'inline_name'"` with
  one inline proxy per element — same `SavedActor` pattern per element.

## The universal read/author channel: T3D via clipboard

`EDIT COPY` / `EDIT PASTE` console commands work in UEFN:

```python
actor_sub.set_selected_level_actors([actor])
unreal.SystemLibrary.execute_console_command(world, "EDIT COPY")
# -> full T3D text (INCLUDING protected properties) is now on the system clipboard
```

Read/write the clipboard host-side (`Get-Clipboard` / `Set-Clipboard` in PowerShell),
edit the text, deselect all, then `EDIT PASTE` to materialize the edited actor.

## Recipe 1 — place a verse device by script

1. `EDIT COPY` any existing verse device; save the clipboard text as a template.
2. Transform the text:
   - empty the verse-instance subobject block (keep its `Begin Object`/`End Object` shell),
   - delete the `SavedActorGuid=` line (the editor mints a fresh one),
   - string-replace: the actor's UAID name, the instance subobject name,
     `ScriptClassPath`, `ActorLabel`, and the `LabelOverride` inside `PlayerOptionData`,
   - set the root component's `RelativeLocation`.
3. `Set-Clipboard`, deselect all actors, `EDIT PASTE`.
   The editor reconstructs a healthy instance of the NEW class, proxies included.

## Recipe 2 — link the `@editable` slots (pure Python)

1. Harvest the hashed slot names: `EDIT COPY` the placed device and regex the
   clipboard for `__verse_0x\w+_\w+`.
2. Wire each slot:

```python
inst = unreal.find_object(None, device.get_path_name() + ".System-my_manager_0")
proxy = inst.get_editor_property("__verse_0xHASH_SlotName")
proxy.modify()
proxy.set_editor_property("SavedActor", target_level_device_actor)
```

3. `modify()` the device and instance too, then
   `unreal.EditorLevelLibrary.save_current_level()`.
4. Verify: read `SavedActor` back, and binary-grep the device's saved file under
   `Content/__ExternalActors__/` for the target device class.

## Recipe 3 — apply verse tags by script

For sets of same-type devices, one tag + a runtime query beats many editables:

```python
sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
handles = sds.k2_gather_subobject_data_for_instance(actor)
params = unreal.AddNewSubobjectParams(
    parent_handle=handles[0],
    new_class=unreal.VerseTagMarkupComponent,
    conform_transform_to_parent=False,
)
sds.add_new_subobject(params)
comp = actor.get_components_by_class(unreal.VerseTagMarkupComponent)[0]

tag_cls = unreal.load_object(None, "/<Project>/_Verse.<Folder>-my_tag")  # after Build Verse Code
ti = unreal.VerseTagTypeInfo()
ti.set_editor_property("InternalTag", tag_cls)
container = unreal.VerseTagTypeInfoContainer()
container.set_editor_property("InternalTags", [ti])
comp.set_editor_property("InternalTags", container)
```

(The tag structs echo as `{}` in Python but the fields respond to
`get/set_editor_property` — they only *look* opaque.)

Verse side: `FindCreativeObjectsWithTag(my_tag)` (pass the tag **type** — the
instance form `my_tag{}` is deprecated) + cast + sort.

## Verse effect gotchas you will hit

- No `return` keyword — a function's last expression is its value.
- Device calls like `GetTransform()` carry `no_rollback`: never call them (even via
  helpers) inside a failable `if (...)` condition — hoist into `X := F()` first.
- For-loop **sources** are failure contexts too: annotate helper functions
  `<transacts>` or hoist, otherwise `for (X : GetThings()):` fails to compile.

## Building Verse by script

**Verse > Build Verse Code** (Ctrl+Shift+B) has no python or console API — but it
CAN be scripted via foreground-verified keyboard synthesis: see
[editor_actions.md](editor_actions.md) (`build_verse()`, plus `save_all()` and
`push_changes()`, and the compile-error self-check loop that reads
`VerseBuild: SUCCESS` / `VerseBuild: Error:` lines from the editor log). Compiled
verse classes only exist at `/<Project>/_Verse.<name>` after a successful build —
build before harvesting slot names or loading tag classes.

## Field-tested refinements (battle scars)

Lessons from linking a full 6-plot game (hundreds of slots) that this doc's first
version didn't know:

- **The proxies live on the INNER script instance, not the actor.**
  `actor.get_editor_property("__verse_0x...")` fails with "Failed to find
  property". Resolve the instance first:
  ```python
  inner = unreal.find_object(None, actor.get_path_name() + ".<class_name>_0")
  proxy = inner.get_editor_property("__verse_0xHASH_SlotName")
  proxy.set_editor_property("SavedActor", target_actor)
  ```
- **`dir()` never exposes `__verse_0x` properties** — harvesting from the T3D
  clipboard text is the only discovery path. And read the clipboard from an
  OUTSIDE process (`Get-Clipboard` in PowerShell): in-process `ctypes`
  `GetClipboardData` returns a null handle in the editor.
- **Verse-device-to-verse-device refs are DIRECT pointers** (no SavedActor proxy):
  `inner.set_editor_property(mangled, other_inner)`; for arrays, get the array,
  assign the element, set it back.
- **Asset editables (`creative_prop_asset`) are the protected exception.** Their
  proxy subobjects are named `Devices_creative_prop_asset_N`; the payload property
  `ActorClass` is protected from python for both read AND write. Channel: T3D
  paste-replace — inject `ActorClass="/Pkg/Path/BP_X.BP_X_C"` (**class form, WITH
  `_C`**) into the proxy's instance block (`Begin Object Name="..." ExportPath=...`),
  then paste-replace the device.
- **Paste-replace safely**: EDIT COPY → rename the original (e.g. `_OLD`) → inject
  lines into the clipboard text → EDIT PASTE (the clone keeps every link) → verify
  the clone's links by readback → destroy the original → **re-link any INBOUND
  refs** (other verse devices pointing at the replaced one hold direct pointers to
  the old instance — reassign them to the clone's inner instance).
- **World Partition persistence (One-File-Per-Actor)**: scripted edits change RAM
  but may never flag the actor's external package dirty — a normal save then
  silently skips them and the edits vanish on editor restart. Rules:
  - Always call `actor.modify(True)` after scripted edits.
  - Check pending state via `EditorLoadingAndSavingUtils.get_dirty_map_packages()`
    / `get_dirty_content_packages()`.
  - `Package.is_dirty` / `set_dirty_flag` / `mark_package_dirty` do NOT exist in
    this build's python — don't prescribe them.
  - Verify persistence ON DISK: `actor.get_package().get_name()` →
    `Content/__ExternalActors__/...uasset`, check the mtime, then binary-scan for
    the LINK TARGET's internal FName (`target.get_name()`, the `BP_X_C_UAID_...`
    form). Scanning for labels or proxy names gives false positives — proxy names
    exist in the file before any link does.
  - Force-writer when dirty registration fails (spawn/attach/data-layer edits):
    `EditorLoadingAndSavingUtils.save_packages(pkgs, only_dirty=False)`.
