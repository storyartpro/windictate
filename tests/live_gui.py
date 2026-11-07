"""Live GUI/hotkey smoke test for WinDictate (Lead integration).

Starts the real app (hidden, no tray), presses the global hotkey through SendInput,
checks that the «Диктовка» window appears/disappears on Enter/Esc, and that the
process stays healthy. Does not need a microphone (silence is expected).

Run: python tests/live_gui.py
Exit 0 when all checks pass.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

user32 = ctypes.WinDLL("user32", use_last_error=True)
TITLE = "Диктовка"

# --- SendInput -----------------------------------------------------------------


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def _send(vk: int, up: bool = False) -> None:
    flags = 0x0002 if up else 0  # KEYEVENTF_KEYUP
    inp = INPUT(type=1)
    inp.u.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if not sent:
        raise OSError(f"SendInput failed for vk={vk}: {ctypes.get_last_error()}")


VK_CONTROL = 0x11
VK_MENU = 0x12
VK_SPACE = 0x20
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B

DEFAULT_HOTKEY = "Ctrl+Alt+Space"
DEFAULT_SETTINGS_HOTKEY = "Ctrl+Alt+S"
VK_SHIFT = 0x10
VK_WIN = 0x5B

_MODIFIERS = {"ctrl": VK_CONTROL, "control": VK_CONTROL, "alt": VK_MENU,
              "shift": VK_SHIFT, "win": VK_WIN}
_KEYS = {
    "space": VK_SPACE, "enter": VK_RETURN, "esc": VK_ESCAPE, "escape": VK_ESCAPE,
    "tab": 0x09, "backspace": 0x08, "insert": 0x2D, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    ",": 0xBC, ".": 0xBE, "/": 0xBF, ";": 0xBA, "'": 0xDE,
    "[": 0xDB, "]": 0xDD, "-": 0xBD, "=": 0xBB, "`": 0xC0,
}


def parse_hotkey(spec: str) -> tuple[list[int], int]:
    """'Ctrl+Alt+Shift+D' -> ([VK_CONTROL, VK_MENU, VK_SHIFT], VK_D)."""
    modifiers: list[int] = []
    key = 0
    for raw in str(spec or "").split("+"):
        part = raw.strip().lower()
        if not part:
            continue
        if part in _MODIFIERS:
            modifiers.append(_MODIFIERS[part])
        elif part in _KEYS:
            key = _KEYS[part]
        elif len(part) == 1 and (part.isalpha() or part.isdigit()):
            key = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            key = 0x70 + int(part[1:]) - 1
    if not key:
        return [VK_CONTROL, VK_MENU], VK_SPACE
    return modifiers, key


SETTINGS_TITLE = "Настройки — Диктовка"


def settings_file() -> Path:
    return Path(
        os.environ.get("WINDICTATE_SETTINGS")
        or Path(os.environ.get("LOCALAPPDATA", "")) / "WinDictate" / "settings.json"
    )


def prepare_settings() -> Path | None:
    """Temporarily force a deterministic state (not pinned, normal size); restore afterwards."""
    path = settings_file()
    if not path.is_file():
        return None
    backup = path.with_suffix(".json.live-backup")
    shutil.copy2(path, backup)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["pinned"] = False
    data["sizeMode"] = "normal"
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return None
    return backup


def restore_settings(backup: Path | None) -> None:
    if backup is not None and backup.is_file():
        try:
            shutil.move(str(backup), str(settings_file()))
        except OSError:
            pass


def read_settings_hotkeys() -> tuple[str, str]:
    """Read the user's configured hotkeys so the test presses what the app actually listens to."""
    settings_path = settings_file()
    hotkey, settings_hotkey = DEFAULT_HOTKEY, DEFAULT_SETTINGS_HOTKEY
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            hotkey = str(data.get("hotkey") or hotkey)
            settings_hotkey = str(data.get("settingsHotkey") or settings_hotkey)
    except (OSError, ValueError):
        pass
    return hotkey, settings_hotkey


def press_combo(modifiers: list[int], key: int) -> None:
    for mod in modifiers:
        _send(mod)
    time.sleep(0.03)
    _send(key)
    time.sleep(0.03)
    _send(key, up=True)
    for mod in reversed(modifiers):
        _send(mod, up=True)


def find_window(title: str) -> int:
    return int(user32.FindWindowW(None, title))


def window_visible(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsWindowVisible(hwnd))


def wait_for(predicate, timeout_s: float, step: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def main() -> int:
    python = os.environ.get("WINDICTATE_PYTHON", sys.executable)
    app_path = Path(os.environ.get("WINDICTATE_APP", str(ROOT / "app.py")))
    log_path = Path(os.environ.get("TEMP", ".")) / "windictate-live-gui.log"
    log = open(log_path, "w", encoding="utf-8", errors="replace")
    settings_backup = prepare_settings()
    proc = subprocess.Popen(
        [python, str(app_path), "--autostart", "--no-tray"],
        cwd=str(app_path.parent),
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=0x08000000 if os.name == "nt" else 0,  # CREATE_NO_WINDOW
    )
    checks: list[tuple[str, bool, str]] = []
    hotkey, settings_hotkey = read_settings_hotkeys()
    dictate_modifiers, dictate_vk = parse_hotkey(hotkey)
    settings_modifiers, settings_vk = parse_hotkey(settings_hotkey)
    print(f"hotkeys: dictate={hotkey!r} settings={settings_hotkey!r}")
    try:
        time.sleep(4.0)
        checks.append(("app process alive", proc.poll() is None, f"pid={proc.pid} code={proc.poll()}"))

        press_combo(dictate_modifiers, dictate_vk)
        timeline: list[bool] = []
        for _ in range(30):
            hwnd = find_window(TITLE)
            timeline.append(window_visible(hwnd))
            time.sleep(0.2)
        appeared = any(timeline)
        checks.append(("hotkey opens window", appeared, f"timeline={timeline}"))

        checks.append(("window stays visible while recording", appeared and timeline[-1], ""))

        # Enter during recording = stop; finalize of silence is quick.
        press_combo([], VK_RETURN)
        time.sleep(2.5)
        checks.append(("app alive after stop", proc.poll() is None, f"code={proc.poll()}"))

        # Enter again (idle) copies and closes.
        press_combo([], VK_RETURN)
        closed = wait_for(lambda: not window_visible(find_window(TITLE)), 4.0)
        checks.append(("Enter closes window after stop", closed, ""))

        # Esc path: open again and cancel.
        press_combo(dictate_modifiers, dictate_vk)
        reopened = wait_for(lambda: window_visible(find_window(TITLE)), 6.0)
        checks.append(("hotkey reopens window", reopened, ""))
        time.sleep(0.8)  # let the Tk window take focus before Esc
        press_combo([], VK_ESCAPE)
        cancelled = wait_for(lambda: not window_visible(find_window(TITLE)), 4.0)
        checks.append(("Esc closes and cancels", cancelled, ""))

        # Settings hotkey.
        press_combo(settings_modifiers, settings_vk)
        settings_hwnd = wait_for(
            lambda: window_visible(find_window(SETTINGS_TITLE)), 5.0
        )
        checks.append(("settings hotkey opens settings", settings_hwnd, ""))
        press_combo([], VK_ESCAPE)
        time.sleep(0.5)
    finally:
        # The app owns a separate STT sidecar process: kill the whole tree so
        # force-terminating the test never leaves CUDA processes behind.
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=20,
            )
        else:
            try:
                proc.terminate()
            except Exception:
                proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        restore_settings(settings_backup)
        log.close()

    failed = 0
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")
        if not ok:
            failed += 1
    print(f"\napp log: {log_path}")
    if failed:
        print(f"FAIL {failed}/{len(checks)}")
        return 1
    print(f"PASS {len(checks)}/{len(checks)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
