"""Remote (streamable HTTP) transport for the UEFN MCP server, with bearer auth.

WHY THIS EXISTS
    mcp_server.py speaks stdio only, which a hosted agent (Grok, a cloud LLM, anything not
    running on this machine) cannot use. This wraps the SAME FastMCP instance -- same tools,
    same code path -- in a Starlette app served over HTTP, and puts a bearer token in front
    of it.

SECURITY -- READ THIS
    These tools include `execute_python`, which runs ARBITRARY PYTHON inside the UEFN editor
    on this machine. Anyone who can reach this endpoint with the token has code execution
    here. Consequences:
      * The bearer token is equivalent to an SSH key. Rotate it if it leaks.
      * This binds to 127.0.0.1 ONLY. It is never exposed directly; a tunnel fronts it.
      * The UEFN listener itself (127.0.0.1:8765-8770) is NEVER tunnelled. Only this port.
      * Stop the tunnel when you are not using it.

USAGE
    Token is read from the UEFN_MCP_TOKEN env var, else from `.mcp_token` beside this file.

        python mcp_http_server.py                  # 127.0.0.1:8799
        python mcp_http_server.py --port 9100
        python mcp_http_server.py --new-token      # rotate, print, exit

    Endpoints:
        POST/GET  /mcp      streamable HTTP MCP endpoint (requires Authorization header)
        GET       /healthz  liveness only, no auth, reveals nothing
"""

import argparse
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcp_server  # noqa: E402  -- importing registers every @mcp.tool on mcp_server.mcp

from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
import uvicorn  # noqa: E402

HOST = "127.0.0.1"          # never 0.0.0.0 -- the tunnel is the only way in
DEFAULT_HTTP_PORT = 8799
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mcp_token")


def load_or_create_token(force_new: bool = False) -> str:
    env = os.environ.get("UEFN_MCP_TOKEN", "").strip()
    if env and not force_new:
        return env
    if os.path.exists(TOKEN_FILE) and not force_new:
        with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
            tok = fh.read().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(32)
    with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
        fh.write(tok)
    try:                      # best-effort: restrict to the current user
        os.system(f'icacls "{TOKEN_FILE}" /inheritance:r /grant:r "%USERNAME%":F >nul 2>&1')
    except Exception:
        pass
    return tok


class BearerAuth(BaseHTTPMiddleware):
    """Reject anything without the exact bearer token, in constant time."""

    def __init__(self, app, token: str):
        super().__init__(app)
        self._expected = f"Bearer {token}"

    async def dispatch(self, request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied, self._expected):
            # Deliberately terse: do not confirm whether the path or the token was wrong.
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def _healthz(_request):
    return PlainTextResponse("ok")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("UEFN_MCP_HTTP_PORT",
                                                                  DEFAULT_HTTP_PORT)))
    ap.add_argument("--new-token", action="store_true", help="rotate the token and exit")
    ap.add_argument("--public-host", default=os.environ.get("UEFN_MCP_PUBLIC_HOST", ""),
                    help="tunnel hostname(s), comma separated, e.g. foo.trycloudflare.com. "
                         "Use '*' to accept any Host header.")
    args = ap.parse_args()

    if args.new_token:
        print(load_or_create_token(force_new=True))
        return 0

    token = load_or_create_token()

    # Stateless: no Mcp-Session-Id affinity required, which is far more forgiving for
    # third-party/hosted MCP clients and for a tunnel that may re-dial.
    try:
        mcp_server.mcp.settings.stateless_http = True
    except Exception:
        pass

    # DNS-rebinding protection: the SDK validates the Host header and defaults to
    # localhost only, so a tunnelled request arrives as 421 "Invalid Host header".
    # Add the tunnel hostname. The bearer token remains the actual access control --
    # this check only exists to stop a browser on this machine being tricked into
    # posting to 127.0.0.1, and a browser cannot supply the token.
    hosts = [h.strip() for h in args.public_host.split(",") if h.strip()]
    if hosts:
        from mcp.server.transport_security import TransportSecuritySettings
        if "*" in hosts:
            sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
            sys.stderr.write("[uefn-mcp-http] WARNING: Host validation disabled (--public-host '*').\n")
        else:
            allow_h = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
            allow_o = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
            for h in hosts:
                allow_h += [h, f"{h}:*"]
                allow_o += [f"https://{h}", f"http://{h}"]
            sec = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                            allowed_hosts=allow_h, allowed_origins=allow_o)
        mcp_server.mcp.settings.transport_security = sec

    app = mcp_server.mcp.streamable_http_app()
    app.routes.append(Route("/healthz", _healthz, methods=["GET"]))
    app.add_middleware(BearerAuth, token=token)

    sys.stderr.write(
        f"[uefn-mcp-http] listening on http://{HOST}:{args.port}/mcp (bearer auth ON)\n"
        f"[uefn-mcp-http] token file: {TOKEN_FILE}\n"
        f"[uefn-mcp-http] UEFN listener stays on 127.0.0.1 and is NOT exposed.\n"
    )
    uvicorn.run(app, host=HOST, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
