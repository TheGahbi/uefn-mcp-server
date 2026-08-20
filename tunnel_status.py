"""Shared on-disk tunnel status so a remote agent can find the live MCP URL.

Never stores the bearer token. Path: .tunnel_status.json beside this file.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(HERE, ".tunnel_status.json")


def read() -> dict[str, Any]:
    if not os.path.exists(STATUS_FILE):
        return {"enabled": False, "url": None}
    try:
        with open(STATUS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"enabled": False, "url": None}


def write(*, enabled: bool, url: str | None = None, public_host: str | None = None,
          port: int = 8799, pid: int | None = None, error: str | None = None) -> None:
    payload = {
        "enabled": bool(enabled),
        "url": url,
        "public_host": public_host,
        "port": port,
        "pid": pid,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "listener_not_tunneled": True,
        "error": error,
    }
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, STATUS_FILE)


def mark_off(error: str | None = None) -> None:
    write(enabled=False, url=None, public_host=None, error=error)
