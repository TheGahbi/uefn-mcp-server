# Transferable Lessons — UEFN editor scripting

Condensed, project-agnostic findings. Each is stated as the general mechanic first,
so it applies to any project. Read [`START_HERE.md`](START_HERE.md) first for the
mental model and safety rules.

---

## Blueprint component editing

- **CDO writes silently don't stick.** Edit components through
  `SubobjectDataSubsystem`, not the class-default object:
  `k2_gather_subobject_data_for_blueprint(bp)` → `k2_find_subobject_data_from_handle(h)`
  → `SubobjectDataBlueprintFunctionLibrary.get_object(data)` → `set_editor_property`
  → `BlueprintEditorLibrary.compile_blueprint(bp)` → `save_loaded_asset(bp)`.
- For an **actor instance** in a level (not the asset), use the instance variant:
  `k2_gather_subobject_data_for_instance(actor)`.
- **Know which component actually renders.** A single Blueprint often has several
  StaticMesh components and only one shows in-game:
  - `StaticMeshComponent0` — the native root; frequently empty.
  - `EditorOnlyStaticMeshComponent` — renders in the editor viewport but **never in
    game**. A classic trap: your mesh "appears" in the editor but not in play.
  - `<MeshName>_GEN_VARIABLE` — the real, game-visible SCS part. This is usually
    your target.
  Enumerate ALL components and identify by name/behavior before assuming
  "first component = the visible one".

## Meshes, pivots, and placement math

- **To recenter a mesh's pivot without editing the asset**, offset the mesh
  component's `relative_location` by the negated bounds-center (scaled by the
  component's scale): `-bounds.origin ⊙ relative_scale3d`. The actor origin then sits
  at the visual center.
- **Re-pivoting changes every placement assumption.** After centering a pivot, code
  that assumed a base pivot must add half-extent (e.g. stack layer `Z += thickness/2`),
  and hand-placed markers now seat the mesh centered rather than hanging. Do the
  pivot change and the dependent math in the same pass.
- **Never hardcode one orientation constant across an asset family.** Mesh local-axis
  conventions vary per asset (some model "flat" on Z, others on Y or X). Measure each
  mesh's bounds (`mesh.get_bounds().box_extent × scale`), find the min-extent axis,
  and bake a per-asset rotation/thickness table into data — don't assume they share
  a "lay flat" rotation.

## Materials

- **`creative_prop.SetMaterial(...)` affects the ROOT MESH ONLY.** On a multi-component
  Blueprint prop it no-ops silently on the sub-parts, so the visible change never
  appears in game. Workaround: pre-build a material-swapped **twin** Blueprint and
  show/hide the twins instead of recoloring at runtime.
- **Palette / gradient textures need mipmaps disabled.** Auto-imported models often
  use a small "gradient" atlas where each strip is one material's color. With mipmaps
  on, distant/low-LOD sampling bleeds neighboring strips together and the model looks
  wrong ("weird"/washed) only at distance. Fix on the texture:
  `mip_gen_settings = TMGS_NO_MIPMAPS`, `never_stream = True`.
- **Reimport-in-place hot-swaps a texture under a live material** (same
  `AssetImportTask` path/name + `replace_existing`) — no rewiring. Keep ≥ ~1.5
  uu/texel or fine detail goes soft.
- Translucent "ghost/hologram" materials: dense translucency with a subtle fresnel
  edge reads far better than dithered opacity (dither shows visible stipple).

## Runtime validity of script-created actors

- **Paste-born actors are valid; spawn-born ones can be invalid.** Devices in
  particular need `PlaysetPackagePathName` and a paste "rebirth" to behave. When a
  scripted actor misbehaves at runtime, prefer materializing it via EDIT COPY/EDIT
  PASTE of a healthy donor over `spawn_actor_from_class`.
- **A `FortStaticMeshActor` is never a valid runtime `creative_prop`.** Its
  `GetTransform()` errors ("disposed") at runtime and it can't be driven. For
  runtime-manipulated props, place/paste actual prop Blueprint instances.

## `creative_prop` / `creative_prop_asset` (Verse)

- `SetMaterial`/`SetMesh` act on the root mesh only (see Materials above).
  `Show()`/`Hide()`, `MoveTo`/`TeleportTo`, `GetTransform()`,
  `GetYawPitchRollDegrees()` work on any prop.
- `creative_prop_asset` `@editable`s store the BP as a protected `ActorClass` string
  in **class form, WITH `_C`**: `"/Pkg/Path/BP_X.BP_X_C"`. It's protected from Python
  read/write — set it via T3D paste (see
  [`../docs/verse_device_linking.md`](../docs/verse_device_linking.md)).

## Verse asset module depth

- Assets one folder deep are reachable from Verse (e.g. `Materials.M_Foo{}`). Deeper
  paths hit "internal module" errors (3593) — duplicate the asset into a shallow
  folder to reference it.

## Mangled Verse proxy names

- `@editable` slots serialize as `__verse_0xHASH_FieldName`. **Hashes regenerate
  across remove/re-add cycles**, and stale orphan hashes linger in an actor's T3D —
  always harvest the LIVE hash (from a fresh EDIT COPY of the current device) rather
  than reusing one you saw earlier.

## When you fix something the hard way

Write the fix down as the **general mechanic**, not just the specific steps, so the
next agent (or you, later) doesn't pay for the same discovery twice. That's why this
pack exists.
