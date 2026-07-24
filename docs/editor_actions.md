# Editor Actions — scripted Save, Verse Build, and Push Changes

`uefn_editor_actions.py` gives Claude (or any script) the three editor-level actions
that keep an automation loop closed: **Save All**, **Build Verse Code**, and
**Push Changes**. With these, an agent can edit `.verse` files, compile them, read
its own compile errors, fix them, save, and push — without a human touching the
keyboard.

## Install

Copy `uefn_editor_actions.py` into your project's `Content/Python/` folder (next to
`uefn_listener.py`). That folder is on `sys.path`, so it imports immediately — no
editor restart:

```python
# via the MCP execute_python tool
import importlib, uefn_editor_actions as act
importlib.reload(act)          # pick up file edits without restarting
act.save_all()
act.build_verse()
act.push_changes()
```

## The three actions

| Action | Mechanism | Needs focus? |
|---|---|---|
| `save_all()` | `EditorLoadingAndSavingUtils.save_dirty_packages(True, True)` — the real python API behind Ctrl+S | No |
| `build_verse()` | Synthesized **Ctrl+Shift+B** into the editor's own window | Yes (handled) |
| `push_changes()` | Synthesized **Alt+P** into the editor's own window | Yes (handled) |

Why keyboard synthesis for two of them: the Verse build and Push Changes have **no
python or console-command entry point**. This was verified by probing the entire
python API surface for build/compile/push methods and sweeping candidate console
commands (`Verse.Build`, `BuildVerseCode`, etc. — all no-ops). The keyboard
accelerator is the only channel.

## Safety design (the part that matters)

Synthesized keys go to whatever window has focus, so the helpers enforce:

1. **Find the editor's real main window** by enumerating windows owned by this
   process. Gotchas: the listener's own status window ("UEFN MCP Listener") must be
   excluded, and the editor's title is `Unreal Editor for Fortnite` — it does *not*
   contain your project name.
2. **Claim foreground legitimately**: tap ALT (an input event from the owning
   process unlocks `SetForegroundWindow`), then bring the editor forward.
3. **Verify foreground actually changed.** If the editor is not confirmed as the
   foreground window, **nothing is sent** — keys are never leaked into another app.
4. Side effect to warn users about: the editor jumps to the foreground when a
   build/push fires.

## Verifying a build (compile-error self-check loop)

`build_verse()` returns as soon as the chord is sent; the build runs async. Never
sleep-wait inside editor python (it blocks the game thread the build needs).
Instead, poll the editor log from outside:

```
%LOCALAPPDATA%\UnrealEditorFortnite\Saved\Logs\UnrealEditorFortnite.log
```

Record the file size before triggering, then read from that offset until one of:

```
VerseBuild: SUCCESS -- Build complete.
VerseBuild: Error: <file>(<line>,<col>): Script error <code>: <message>
```

The error lines carry full file/line/column — everything needed to fix and rebuild
automatically. Loop until SUCCESS.

**Retry rule:** if neither marker appears within ~60 seconds, re-send the chord
once. After a *failed* build the editor pops the Message Log window, which can
steal focus at exactly the wrong moment and eat the next chord. (A no-change build
still logs `SUCCESS` with "No packages found requiring compilation", so a silent
outcome always means the chord didn't land.)

Push verification: a real push floods the log with `LogValkyrie: Project upload
starting` and the upload flow (~tens of KB). ~1 KB of growth means the chord
didn't land.

## Verifying a save

`save_all()` returns `{"saved": true, "dirty_before": [maps, content],
"dirty_after": [0, 0]}`. For scripted edits to actors in World Partition projects,
call `actor.modify(True)` after editing so the external actor package registers as
dirty — see the persistence notes in
[verse_device_linking.md](verse_device_linking.md).
