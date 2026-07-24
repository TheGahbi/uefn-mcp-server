# UEFN MCP — AI Context Pack

**You are an AI coding agent.** You are reading this because someone handed you this
folder to give you working knowledge of controlling **Unreal Editor for Fortnite
(UEFN)** through the `uefn-mcp-server` tool. This pack is written *for you*, not for
a human — it front-loads the hard-won operating rules so you don't rediscover them
by crashing the editor or shipping broken Verse.

Read this file fully before you touch the editor. Then pull specific recipes from
the linked docs as you need them.

---

## What you're controlling

- A **listener** runs *inside* the UEFN editor process (an HTTP server on
  `127.0.0.1:8765`, range 8765–8770). It executes Python against the live editor via
  the `unreal` module and runs editor console commands.
- An **MCP server** (`mcp_server.py`) bridges your MCP tools to that listener. If
  MCP tools named for UEFN are available to you, use them. If not, you can POST
  directly: `{"command": "execute_python", "params": {"code": "...; result = ..."}}`.
  In `execute_python`, **assign your output to a variable named `result`** — that's
  what comes back.
- You edit `.verse` files on disk, then trigger a build; you edit assets/actors
  through `unreal` Python.

Full tool list: [`../docs/tools_reference.md`](../docs/tools_reference.md).
API surface map: [`../docs/uefn_python_capabilities.md`](../docs/uefn_python_capabilities.md).

---

## The one mental model that prevents most failures

**Every editor system has a display layer and an authoritative store. Scripted
writes to the display layer silently no-op or revert.** Before any edit, ask: *where
does the editor's own UI write this value?* That store is your target. Seen
repeatedly:

| You want to change | The decoy (writes look OK, don't stick) | The authoritative store |
|---|---|---|
| A device option | `PlayerOptionData` row / user-option map | native actor/component property, or the device's `Fort*Component` setter |
| A Blueprint component | the CDO component | SCS node template via `SubobjectDataSubsystem` |
| A Verse `@editable` link | Details-panel value / actor property | `__verse_0xHASH_Slot` proxy on the **inner** script instance |

**Discovery method when you don't know the store:** have the human hand-edit ONE
example in the UI, then diff it against an untouched twin (EDIT COPY the T3D, or
export the asset) — every real property reveals itself in the diff.

---

## Operating rules (follow these every time)

1. **Verify by readback, never by "the call returned OK."** Re-load the object and
   read the value back. Compare **full object paths**, not names (different folders
   reuse names). If you can't read it in-process, verify through another channel:
   the T3D clipboard, a binary scan of the `.uasset`, or the editor log.

2. **Never pass `None` as a `WorldContextObject`.** It compiles fine and then
   **hard-crashes the editor** with an access violation (e.g.
   `execute_console_command(None, ...)`). Always pass a live world:
   `unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()`.
   See [`../docs/troubleshooting.md`](../docs/troubleshooting.md) → "Editor Crashes".

3. **Close the Verse compile loop yourself.** After editing `.verse`, trigger the
   build and read your own errors from the editor log — fix and rebuild until
   `VerseBuild: SUCCESS -- Build complete.` Never hand a human broken Verse to
   compile for you. Full loop + the scripted build/save/push:
   [`../docs/editor_actions.md`](../docs/editor_actions.md).

4. **Persistence is not automatic in World Partition.** Scripted edits change RAM
   but may not flag the actor's One-File-Per-Actor package dirty, so a normal save
   skips them and they vanish on restart. Always `actor.modify(True)` after edits,
   and verify on disk by scanning the external `.uasset` for the target's internal
   FName. Details: [`../docs/verse_device_linking.md`](../docs/verse_device_linking.md)
   → "World Partition persistence".

5. **Don't sleep-wait inside editor Python** — it blocks the game thread the build
   needs. Fire the action, return, then poll from outside (the log, a follow-up
   query).

6. **The 30s HTTP window is real but not fatal.** A long command (shader compile,
   big build) can time out on the wire while still completing on the main thread.
   Don't blindly re-fire — verify with a follow-up query first.

7. **Prefer the editor's own channels over reconstructed ones.** Paste-clone beats
   spawn-from-class; a human-made donor beats hand-authored serialization; a native
   property set beats a derived-view edit.

---

## Where the deep recipes live

| Topic | Doc |
|---|---|
| Wiring Verse `@editable` slots, placing verse devices, protected `ActorClass`, paste-replace, persistence | [`../docs/verse_device_linking.md`](../docs/verse_device_linking.md) |
| Scripted Save / Verse build / Push Changes + compile-error self-check loop | [`../docs/editor_actions.md`](../docs/editor_actions.md) |
| Script-caused editor crashes, interpreter mismatch, timeouts | [`../docs/troubleshooting.md`](../docs/troubleshooting.md) |
| All tools with params and examples | [`../docs/tools_reference.md`](../docs/tools_reference.md) |
| Full `unreal` Python capability map | [`../docs/uefn_python_capabilities.md`](../docs/uefn_python_capabilities.md) |
| Two-process architecture | [`../docs/architecture.md`](../docs/architecture.md) |

More condensed, transferable findings (Blueprint component editing, materials,
runtime-validity of spawned actors, Verse effect gotchas):
[`LESSONS.md`](LESSONS.md).

---

## Verse language gotchas you WILL hit

- No `return` — a function's last expression is its value.
- `GetTransform()` and similar device calls carry `no_rollback`: never call them
  inside a failable `if (...)` condition — hoist into `X := F()` first. For-loop
  **sources** are failure contexts too; annotate pure-read helpers `<transacts>`.
- `set Map[K] = v` must be wrapped in an `if (...) {}`.
- `FindCreativeObjectsWithTag`: pass the **type** (`my_tag`), not an instance.
- `event(agent)` has `Signal`/`Await` only — no `Subscribe`.
- Compiled Verse classes only exist at `/<Project>/_Verse.<Folder>-<name>` **after a
  successful build** — build before harvesting slot names or loading tag classes.
