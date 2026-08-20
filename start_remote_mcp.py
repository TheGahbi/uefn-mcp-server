"""One command: HTTPS-tunnelled, token-protected UEFN MCP for a remote agent.

Starts a Cloudflare quick tunnel, discovers the public hostname, then starts the MCP
streamable-HTTP server bound to 127.0.0.1 with that hostname allowlisted. Prints the URL
and bearer token, and shuts both down together on Ctrl-C.

    python start_remote_mcp.py
    python start_remote_mcp.py --port 8799

Also writes .tunnel_status.json (no token) so a remote agent can find the live URL.
Prefer the desktop "UEFN Tunnel" switch.

WHAT IS AND IS NOT EXPOSED
    exposed  : 127.0.0.1:<port>/mcp   -- the MCP server, behind a bearer token
    NOT      : 127.0.0.1:8765-8770    -- the UEFN editor listener. Never tunnelled.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tunnel_status  # noqa: E402

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def find_cloudflared() -> str:
    p = shutil.which("cloudflared")
    if p:
        return p
    for c in (r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
              r"C:\Program Files\cloudflared\cloudflared.exe"):
        if os.path.exists(c):
            return c
    sys.exit("cloudflared not found. Install:  winget install --id Cloudflare.cloudflared -e")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()

    def log(msg: str) -> None:
        print(msg, flush=True)

    tunnel_status.write(enabled=True, url=None, port=args.port, pid=os.getpid(), error="starting")

    cfd = find_cloudflared()
    log(f"[1/2] starting Cloudflare tunnel -> http://127.0.0.1:{args.port}")
    hide = 0x08000000  # CREATE_NO_WINDOW
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    tun = subprocess.Popen(
        [cfd, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{args.port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        startupinfo=si, creationflags=hide)

    public = {}

    def pump():
        for line in tun.stdout:
            if "url" not in public:
                m = URL_RE.search(line)
                if m:
                    public["url"] = m.group(0)

    threading.Thread(target=pump, daemon=True).start()

    for _ in range(60):
        if "url" in public:
            break
        if tun.poll() is not None:
            tunnel_status.mark_off(error="cloudflared exited before publishing a URL")
            sys.exit("cloudflared exited before publishing a URL.")
        time.sleep(0.5)
    if "url" not in public:
        tun.terminate()
        tunnel_status.mark_off(error="timed out waiting for the tunnel URL")
        sys.exit("timed out waiting for the tunnel URL.")

    url = public["url"]
    host = url.split("://", 1)[1]
    mcp_url = url + "/mcp"

    log(f"[2/2] starting MCP server (127.0.0.1:{args.port}, host allowlist: {host})")
    srv = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "mcp_http_server.py"),
         "--port", str(args.port), "--public-host", host],
        startupinfo=si, creationflags=hide)

    time.sleep(3)
    token_file = os.path.join(HERE, ".mcp_token")
    token = open(token_file, encoding="utf-8").read().strip() if os.path.exists(token_file) else "(none)"

    tunnel_status.write(
        enabled=True, url=mcp_url, public_host=host,
        port=args.port, pid=os.getpid(), error=None)

    log("")
    log("=" * 68)
    log("  MCP URL      : " + mcp_url)
    log("  Transport    : streamable HTTP")
    log("  Auth header  : Authorization: Bearer " + token)
    log("=" * 68)
    log("  UEFN listener stays on 127.0.0.1 and is NOT tunnelled.")
    log("  This URL changes every restart. Ctrl-C or the desktop switch stops both.")
    log("")

    try:
        while True:
            if srv.poll() is not None:
                log("MCP server exited.")
                break
            if tun.poll() is not None:
                log("Tunnel exited.")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in (srv, tun):
            try:
                p.terminate()
            except Exception:
                pass
        tunnel_status.mark_off()
    return 0


if __name__ == "__main__":
    sys.exit(main())
