# Setup Guide

## Prerequisites

- UEFN editor with Python scripting enabled via **Project Settings**
- Python 3.10+ installed on your system (for the MCP server process)
- Claude Code CLI installed

## Step 0: Let Claude do the setup

Open Claude Code and ask: *"Help me set up UEFN MCP server"* — it will install dependencies, create config files, and walk you through the rest.

If you prefer to do it manually, follow the steps below.

## Step 1: Enable Python in UEFN

1. Open your project in UEFN
2. Go to **Project > Project Settings**
3. Search for **Python** and check the box for **Python Editor Script Plugin**

After this, you should see **Tools > Execute Python Script** in the menu bar.

## Step 2: Start the Listener

### Manual start (recommended for first use)

1. In UEFN, go to **Tools > Execute Python Script**
2. Navigate to and select `uefn_listener.py`
3. A **status window** will appear:

```
UEFN MCP Listener  v0.2.0
● Listener: Running
● MCP Server: Connecting...

Port      8765
Uptime    0m 05s
Requests  0
...
```

The window shows real-time status — you don't need to check the Output Log.
You can safely close the window; the listener continues running in the background.

### Auto-start on editor launch

Copy both files to your UEFN project's `Content/Python/` directory:

```bash
cp uefn_listener.py  <YourUEFNProject>/Content/Python/uefn_listener.py
cp init_unreal.py     <YourUEFNProject>/Content/Python/init_unreal.py
```

The listener will start automatically every time you open the project in UEFN.

## Step 3: Install MCP SDK

> ⚠️ **The #1 reason this "works for one person but not another."** Claude Code
> launches whatever the `command` in your config resolves to (e.g. `python`). If
> `mcp` is installed into a *different* interpreter than that one, the server dies
> at startup with a cryptic error. Install into the **exact** interpreter you will
> point the config at, and use its full path.

Find the interpreter you'll use and install `mcp` into that same one:

```bash
# 1. Find the interpreter's full path (use whichever of these runs on your system):
python  -c "import sys; print(sys.executable)"      # Windows
python3 -c "import sys; print(sys.executable)"      # macOS / Linux

# 2. Install mcp into THAT interpreter (note the -m pip form):
python  -m pip install mcp
```

On a fresh Windows install, `python` may open the Microsoft Store instead of
running — install Python from [python.org](https://www.python.org/downloads/) and
tick **"Add python.exe to PATH"**, or just use the full `.exe` path everywhere.

Verify — and get your ready-to-paste config line — with the built-in check:

```bash
python mcp_server.py --check
```

It prints the interpreter, whether `mcp` is importable, whether the UEFN listener
is reachable, and the exact `.mcp.json` snippet to use.

## Step 4: Configure Claude Code

### Option A: Project-level config (recommended)

Create `.mcp.json` in your project root. **Use the full interpreter path from Step 3**
(the one that has `mcp` installed) — not a bare `"python"`, which is the usual cause
of "server failed to start" on someone else's machine:

```json
{
  "mcpServers": {
    "uefn": {
      "command": "C:/Users/you/AppData/Local/Programs/Python/Python312/python.exe",
      "args": ["C:/path/to/uefn-mcp-server/mcp_server.py"]
    }
  }
}
```

`python mcp_server.py --check` prints this exact line filled in for your machine.
(A bare `"python"` works too *if* the same `python` on your PATH is the one with
`mcp` installed — the full path just removes all doubt.)

### Option B: Global config

Add to `~/.claude/settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "uefn": {
      "command": "python",
      "args": ["/path/to/uefn-mcp-server/mcp_server.py"]
    }
  }
}
```

### Custom port

If the default port 8765 is in use, you can specify a different port:

```json
{
  "mcpServers": {
    "uefn": {
      "command": "python",
      "args": ["/path/to/uefn-mcp-server/mcp_server.py", "--port", "8766"]
    }
  }
}
```

Or via environment variable:

```json
{
  "mcpServers": {
    "uefn": {
      "command": "python",
      "args": ["/path/to/uefn-mcp-server/mcp_server.py"],
      "env": { "UEFN_MCP_PORT": "8766" }
    }
  }
}
```

## Step 5: Restart Claude Code

Claude Code reads `.mcp.json` on startup. Start a new session:

```bash
claude
```

The UEFN MCP tools should now be available. Test with: "ping the UEFN editor".

## Listener Management

### Using the status window

The status window provides **Stop**, **Start**, and **Restart** buttons. When stopped, you can change the port number before starting again.

Status indicators:
- **Listener: Running** (green) — HTTP server is active
- **Listener: Stopped** (red) — HTTP server is not running
- **MCP Server: Connected** (green) — Claude Code is actively connected (heartbeat received)
- **MCP Server: Connecting...** (yellow) — listener just started, waiting for first heartbeat
- **MCP Server: Lost Xs ago** (gray) — Claude Code disconnected or was restarted

### Re-running the script

Running `uefn_listener.py` again via **Tools > Execute Python Script** is safe — it will cleanly replace the previous listener and open a new status window.

### Check status from Claude Code

Use the `ping` tool, or ask: *"Is the UEFN listener running?"*

### Shutdown from Claude Code

Use the `shutdown` tool to stop the listener remotely. The port is freed immediately.
