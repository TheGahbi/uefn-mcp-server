# uefn_editor_actions.py — editor-level actions with no direct python API.
#
# PLATFORM: Windows only. build_verse()/push_changes() synthesize keystrokes via
# ctypes.windll (Win32). save_all() is pure `unreal` API and works on any OS.
#
# Drop this file into your project's Content/Python/ folder (next to the listener).
# It is importable immediately — no editor restart needed:
#
#   import uefn_editor_actions as act
#   act.save_all()        # Ctrl+S equivalent (real python API, no keyboard)
#   act.build_verse()     # Ctrl+Shift+B (synthesized hotkey, foreground-verified)
#   act.push_changes()    # Alt+P (synthesized hotkey, foreground-verified)
#
# WHY HOTKEYS: the Verse build and Push Changes have NO python or console-command
# entry points (verified against the full python API surface and a console-command
# sweep). The only channel is the keyboard accelerator. These helpers synthesize
# the chord INTO THE EDITOR'S OWN MAIN WINDOW with a hard safety rule: if the
# editor cannot be verified as the foreground window, NOTHING is sent — keys are
# never leaked into whatever app the user is using.
#
# VERIFYING A BUILD: the chord returns immediately; the build runs async. Poll the
# editor log (Saved/Logs/UnrealEditorFortnite.log) from your pre-build offset for:
#   VerseBuild: SUCCESS -- Build complete.
#   VerseBuild: Error: <file>(<line>,<col>): Script error <code>: <message>
# If neither appears within ~60s, re-send the chord once — a focus steal (e.g. the
# Message Log window popping after a failed build) can eat the first attempt.
# NEVER sleep-wait inside editor python: it blocks the game thread the build needs.

import unreal
import ctypes
import time
from ctypes import wintypes

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12   # Alt
VK_B = 0x42
VK_P = 0x50
KEYEVENTF_KEYUP = 0x0002


def save_all():
    """Save every dirty map + content package (external actors included).

    This IS Ctrl+S — proper python API, no keyboard or focus needed. Returns the
    dirty-package counts before/after so success is verifiable.
    """
    before = [len(unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages()),
              len(unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages())]
    ok = unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
    after = [len(unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages()),
             len(unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages())]
    return {"saved": bool(ok), "dirty_before": before, "dirty_after": after}


def _main_hwnd():
    """Find this process's visible main editor window.

    Title-matching gotchas learned the hard way: the listener's own status window
    ("UEFN MCP Listener") contains "UEFN" and must be excluded, and the editor's
    real title is "Unreal Editor for Fortnite" (it does NOT contain the project
    name). Fallback: the biggest visible window owned by this process.
    """
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    pid = k32.GetCurrentProcessId()
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _l):
        p = wintypes.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(h):
            n = u32.GetWindowTextLengthW(h)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(h, buf, n + 1)
                found.append((h, buf.value))
        return True

    u32.EnumWindows(cb, 0)
    cands = [(h, t) for h, t in found if "MCP Listener" not in t]
    for h, t in cands:
        if "Unreal Editor" in t or "UEFN" in t:
            return h
    best = None
    best_area = 0
    r = wintypes.RECT()
    for h, t in cands:
        if u32.GetWindowRect(h, ctypes.byref(r)):
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
            if area > best_area:
                best_area = area
                best = h
    return best


def _fg_title():
    u32 = ctypes.windll.user32
    h = u32.GetForegroundWindow()
    n = u32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(h, buf, n + 1)
    return buf.value


def _tap(vk):
    u32 = ctypes.windll.user32
    scan = u32.MapVirtualKeyW(vk, 0)
    u32.keybd_event(vk, scan, 0, 0)
    u32.keybd_event(vk, scan, KEYEVENTF_KEYUP, 0)


def _send_chord(vks):
    """Foreground-verified chord send. Sends NOTHING unless the editor holds focus."""
    u32 = ctypes.windll.user32
    h = _main_hwnd()
    if h is None:
        return {"err": "main window not found"}
    if u32.GetForegroundWindow() != h:
        _tap(VK_MENU)  # an input event from our own process unlocks SetForegroundWindow
        u32.SetForegroundWindow(h)
        time.sleep(0.25)
    if u32.GetForegroundWindow() != h:
        return {"err": "editor not foreground — keys NOT sent", "foreground": _fg_title()}
    for vk in vks:
        u32.keybd_event(vk, u32.MapVirtualKeyW(vk, 0), 0, 0)
        time.sleep(0.03)
    for vk in reversed(vks):
        u32.keybd_event(vk, u32.MapVirtualKeyW(vk, 0), KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)
    return {"sent": True, "foreground": _fg_title()}


def build_verse():
    """Ctrl+Shift+B — Verse build. Poll the editor log afterwards (see module docs)."""
    return _send_chord((VK_CONTROL, VK_SHIFT, VK_B))


def push_changes():
    """Alt+P — Push Changes to the running session (chord no-ops if no session)."""
    return _send_chord((VK_MENU, VK_P))
