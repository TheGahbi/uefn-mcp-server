"""Auto-start the MCP listener when the UEFN editor opens a project.

Place this file AND uefn_listener.py in your UEFN project's Content/Python/
directory. UEFN executes init_unreal.py automatically on project open (when
Python scripting is enabled), and importing uefn_listener runs its module-level
auto-start block, which starts the HTTP listener on 127.0.0.1:8765-8770.

Tip: add `Content/Python` and `**/*.py` to your project's .loreignore so this
local tooling never syncs to Epic's servers or affects validation.
"""

import unreal

try:
    import uefn_listener  # noqa: F401 — the import side effect starts the listener
    unreal.log(f"[MCP] Listener auto-started on port {unreal._mcp_bound_port}")
except Exception as e:
    unreal.log_error(f"[MCP] Auto-start failed: {e}")
