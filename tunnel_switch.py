"""Desktop on/off switch for the UEFN remote MCP tunnel.

ON  starts start_remote_mcp.py (Cloudflare quick tunnel + HTTP MCP on 8799).
OFF stops those processes. Writes .tunnel_status.json so a remote agent can
read the live URL from this folder. Never tunnels the UEFN listener.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from tkinter import font as tkfont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tunnel_status  # noqa: E402

START_SCRIPT = os.path.join(HERE, "start_remote_mcp.py")
PORT = 8799


def _wmic_cmdlines() -> list[tuple[int, str]]:
    try:
        out = subprocess.check_output(
            ["wmic", "process", "get", "ProcessId,CommandLine", "/FORMAT:LIST"],
            text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    rows: list[tuple[int, str]] = []
    pid = None
    cmd = ""
    for line in out.splitlines():
        if line.startswith("CommandLine="):
            cmd = line.split("=", 1)[1].strip()
        elif line.startswith("ProcessId="):
            try:
                pid = int(line.split("=", 1)[1].strip())
            except ValueError:
                pid = None
        elif not line.strip() and pid and cmd:
            rows.append((pid, cmd))
            pid, cmd = None, ""
    if pid and cmd:
        rows.append((pid, cmd))
    return rows


def running_pids() -> list[int]:
    pids = []
    for pid, cmd in _wmic_cmdlines():
        low = cmd.lower()
        if "start_remote_mcp.py" in low:
            pids.append(pid)
        elif "mcp_http_server.py" in low:
            pids.append(pid)
        elif "cloudflared" in low and f"127.0.0.1:{PORT}" in low:
            pids.append(pid)
    return pids


def tunnel_on() -> bool:
    return bool(running_pids())


def start_tunnel() -> None:
    if tunnel_on():
        return
    tunnel_status.write(enabled=True, url=None, port=PORT, pid=None, error="starting")
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [sys.executable, START_SCRIPT, "--port", str(PORT)],
        cwd=HERE,
        creationflags=flags,
    )


def stop_tunnel() -> None:
    for pid in running_pids():
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, check=False)
        except OSError:
            pass
    tunnel_status.mark_off()


class SwitchApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("UEFN Tunnel")
        self.root.geometry("360x220")
        self.root.resizable(False, False)
        self.root.configure(bg="#1b1b1b")

        title_f = tkfont.Font(family="Segoe UI", size=14, weight="bold")
        body_f = tkfont.Font(family="Segoe UI", size=10)
        small_f = tkfont.Font(family="Segoe UI", size=8)

        tk.Label(self.root, text="UEFN remote tunnel", font=title_f,
                 fg="#f2f2f2", bg="#1b1b1b").pack(pady=(16, 4))
        tk.Label(self.root, text="Listener stays on this PC. Only 8799 is exposed.",
                 font=small_f, fg="#9a9a9a", bg="#1b1b1b").pack()

        self.btn = tk.Button(self.root, text="OFF", font=title_f, width=14,
                             command=self.toggle, relief="flat",
                             fg="#111", bg="#666", activebackground="#888")
        self.btn.pack(pady=16)

        self.status = tk.Label(self.root, text="", font=body_f, fg="#d0d0d0",
                               bg="#1b1b1b", wraplength=320, justify="center")
        self.status.pack(padx=16)

        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.refresh()
        self.root.after(1500, self.tick)

    def toggle(self) -> None:
        if tunnel_on():
            stop_tunnel()
        else:
            start_tunnel()
        self.refresh()

    def refresh(self) -> None:
        on = tunnel_on()
        st = tunnel_status.read()
        if on:
            self.btn.configure(text="ON", bg="#3dd68c", activebackground="#2fb873")
            url = st.get("url") or "waiting for Cloudflare URL…"
            self.status.configure(text=url)
        else:
            self.btn.configure(text="OFF", bg="#666", activebackground="#888")
            err = st.get("error")
            self.status.configure(text=err if err and err != "starting" else "Off. I cannot reach the editor.")

    def tick(self) -> None:
        self.refresh()
        self.root.after(1500, self.tick)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    SwitchApp().run()
