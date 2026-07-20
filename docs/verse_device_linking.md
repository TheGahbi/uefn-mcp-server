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

## The one thing that can't be scripted

**Verse > Build Verse Code** (Ctrl+Shift+B) has no scripting hook. Compiled verse
classes only exist at `/<Project>/_Verse.<name>` after a successful build — do the
build before harvesting slot names or loading tag classes.
