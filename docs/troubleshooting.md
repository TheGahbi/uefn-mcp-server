# Troubleshooting

## Connection Issues

### "UEFN listener is not running"

**Cause:** The MCP server cannot reach the HTTP listener inside UEFN.

**Fix:**
1. Make sure UEFN editor is open
2. Go to **Tools > Execute Python Script** and select `uefn_listener.py`
3. Check the Output Log (Window > Output Log) for `[MCP] Listener started on http://127.0.0.1:8765`
4. Verify with curl: `curl http://127.0.0.1:8765/`

### "Connection refused" after listener was running

**Cause:** The listener crashed or the editor was restarted.

**Fix:** Re-run the listener via **Tools > Execute Python Script**. If you want auto-start, set up `init_unreal.py` (see [Setup Guide](setup.md)).

### Port conflict

**Cause:** Port 8765 is already in use by another process.

**Fix:** The listener auto-detects free ports in range 8765-8770. Check which port it bound to in the Output Log. Then configure the MCP server to use the same port:

```bash
python mcp_server.py --port 8766
```

Or update `.mcp.json` accordingly.

To find what's using the port:
```bash
netstat -ano | findstr :8765
```

## "Works for one person but not another" / tools never appear

Almost always an **interpreter mismatch**: Claude Code launches the `command` from
your config, but `mcp` was installed into a *different* Python, so the server exits
immediately and the tools never register.

**Diagnose in one command:**

```bash
python mcp_server.py --check
```

It reports the interpreter path, whether `mcp` is importable there, whether the
listener is reachable, and prints the exact `.mcp.json` line to use.

**Fix:** install `mcp` into the *same* interpreter you name in the config, and name
it by full path:

```bash
"C:/full/path/to/python.exe" -m pip install mcp
```

```json
{ "command": "C:/full/path/to/python.exe", "args": ["C:/path/to/mcp_server.py"] }
```

Other fresh-machine gotchas:
- On Windows, a bare `python` may open the Microsoft Store instead of running.
  Install from python.org with "Add to PATH", or use the full `.exe` path.
- On macOS/Linux it's usually `python3`, not `python`.
- `uefn_editor_actions.py` (scripted build/save/push) is **Windows-only** — it
  uses `ctypes.windll`. The core server and all tools are cross-platform; only
  those keyboard-driven helpers require Windows.

## Command Errors

### "Command timed out after 30s"

**Cause:** The command took too long to execute on the main thread, or the editor is frozen/busy.

**Possible reasons:**
- Editor is compiling shaders
- Editor is loading a large level
- The Python code in `execute_python` has an infinite loop
- A very large operation (e.g., listing millions of assets)

**Fix:**
- Wait for the editor to finish its current operation
- For long operations, break them into smaller batches
- Check the UEFN Output Log for errors

### "Unknown command: xyz"

**Cause:** The command name doesn't match any registered handler.

**Fix:** Use `ping` to see the list of available commands. Make sure listener and MCP server versions match.

### "Actor not found" / "Asset not found"

**Cause:** The path or label doesn't match any existing object.

**Fix:**
- Use `get_all_actors` to list actors and find the correct path/label
- Use `list_assets` to browse the content directory
- Actor labels are case-sensitive
- Asset paths must start with `/Game/` (or `/Engine/` for engine assets)

## Python Execution Issues

### `execute_python` returns empty result

**Cause:** The code didn't assign to the `result` variable.

**Fix:** Assign your return value to `result`:
```python
# Wrong — no output
x = 1 + 1

# Correct
result = 1 + 1
```

### `execute_python` shows error in stderr

**Cause:** The Python code raised an exception.

**Fix:** Check the `stderr` field for the full traceback. Common issues:
- `AttributeError`: The API method doesn't exist in UEFN (check `docs/uefn_api_availability.md`)
- `TypeError`: Wrong argument types (use `unreal.Vector`, `unreal.Rotator`, etc.)
- `RuntimeError`: Editor state doesn't allow the operation (e.g., saving during PIE)

### `print()` output not visible

**Cause:** By default, `print()` output goes to `stdout` which is captured and returned in the response.

**Fix:** Check the `stdout` field in the response. If you want it in the UE Output Log too, use:
```python
unreal.log("My message")
```

## MCP Server Issues

### Claude Code doesn't show UEFN tools

**Cause:** `.mcp.json` not found or MCP server failed to start.

**Fix:**
1. Verify `.mcp.json` exists in the project root
2. Check the path to `mcp_server.py` is correct and absolute
3. Verify `mcp` SDK is installed: `pip install mcp`
4. Test the server manually: `python mcp_server.py` (should hang waiting for stdio)
5. Restart Claude Code

### "ModuleNotFoundError: No module named 'mcp'"

**Cause:** MCP SDK not installed in the Python used by Claude Code.

**Fix:**
```bash
pip install mcp
```

Make sure you're installing for the same Python that `.mcp.json` references. If you have multiple Python versions:
```bash
python3 -m pip install mcp
```

## Editor Issues

### Editor freezes briefly when executing commands

**Expected behavior.** Commands execute on the main thread, which blocks the editor for the duration of the operation. Keep operations fast. For batch operations, use `ScopedSlowTask` to show a progress bar:

```python
with unreal.ScopedSlowTask(100, 'Processing...') as task:
    task.make_dialog(True)
    for i in range(100):
        if task.should_cancel():
            break
        task.enter_progress_frame(1)
        # ... work
```

### Listener survives editor restart?

**No.** The listener runs inside the editor process. When the editor closes, the listener dies. You need to restart it (or use `init_unreal.py` for auto-start).

### Multiple editor instances

Each editor instance needs its own listener on a different port. The auto-detect range (8765-8770) supports up to 6 simultaneous instances. Configure each MCP server connection with the correct port.

## Editor Crashes (caused by scripts)

The listener runs inside the editor process — buggy python can take the whole editor down. Field-confirmed rules:

### Never pass `None` as a WorldContextObject

```python
# CRASHES THE EDITOR — hard access violation, no python traceback:
unreal.SystemLibrary.execute_console_command(None, "EDIT COPY")

# Always:
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
unreal.SystemLibrary.execute_console_command(world, "EDIT COPY")
```

Any UObject-context parameter typed as a world context must be a live object. `None` compiles fine and dies at native level (`WorldContext requested with invalid context object` in the log, then `EXCEPTION_ACCESS_VIOLATION`). This class of bug produces **"connection closed mid-receive" followed by "connection refused"** on the MCP side.

### Diagnosing a crash

1. Check whether the editor process is actually gone (`UnrealEditorFortnite*` in the process list) — distinguishes an editor crash from a listener-only failure.
2. Read the evidence, don't guess: `%LOCALAPPDATA%\UnrealEditorFortnite\Saved\Crashes\UECC-*\` contains `CrashContext.runtime-xml` (error + callstack — python-triggered crashes show engine frames stacked on `python311` frames) and a copy of the session log whose tail shows the last commands executed.

### Don't run long python through the HTTP window

The HTTP round-trip times out at 30s; the queued work still completes on the main thread but **its result is lost**. Full-object sweeps (`unreal.ObjectIterator()` over everything) take minutes — use targeted `unreal.find_object()` with a known path instead.

### Scripted saves vs. user saves

Don't bundle a level save into the same call as risky edits — if the call dies you cannot tell what persisted. Make edits + `actor.modify(True)` + verify by readback in one call; save separately (`save_dirty_packages`, or let the user Ctrl+S). See the persistence rules in [verse_device_linking.md](verse_device_linking.md).
