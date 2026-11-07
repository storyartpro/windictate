"""Реальные скриншоты фирменного UI WinDictate (STORYARTPRO).

Снимает четыре PNG в ``win-dictate/screenshots/``:
dictation-scene.png, dictation-paper.png, settings-scene.png, settings-paper.png.

Окно находится через Win32 ``FindWindowW`` (и проверяется, что оно наше —
по PID процесса), прямоугольник — ``GetWindowRect``, снимок — ``PIL.ImageGrab``.
Если окно не найдено или снимок не сохранился — честный FAIL, без подделок.

Запуск: python tests/capture_ui.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SHOTS = ROOT / "screenshots"
DICTATION_TITLE = "Диктовка"
SETTINGS_TITLE = "Настройки — WinDictate"


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wt.LONG),
        ("top", wt.LONG),
        ("right", wt.LONG),
        ("bottom", wt.LONG),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


def _user32() -> ctypes.WinDLL:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    user32.FindWindowW.restype = wt.HWND
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindowVisible.restype = wt.BOOL
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wt.BOOL
    user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
    user32.GetClientRect.restype = wt.BOOL
    user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(POINT)]
    user32.ClientToScreen.restype = wt.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    return user32


def find_own_window(user32: ctypes.WinDLL, title: str, timeout_s: float = 6.0) -> int:
    """HWND видимого окна с нашим заголовком, принадлежащего этому процессу."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        hwnd = int(user32.FindWindowW(None, title) or 0)
        if hwnd and user32.IsWindowVisible(hwnd):
            pid = wt.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) == os.getpid():
                return hwnd
        time.sleep(0.08)
    return 0


def grab_window(user32: ctypes.WinDLL, hwnd: int, path: Path) -> tuple[int, int, int, int]:
    """Снимает ровно клиентскую область окна (без системной рамки и тени)."""
    from PIL import ImageGrab

    client = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(client)):
        raise RuntimeError("GetClientRect не сработал")
    origin = POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        raise RuntimeError("ClientToScreen не сработал")
    width = int(client.right - client.left)
    height = int(client.bottom - client.top)
    if width < 50 or height < 50:
        raise RuntimeError(f"подозрительно маленькая клиентская область: {width}x{height}")
    bbox = (int(origin.x), int(origin.y), int(origin.x) + width, int(origin.y) + height)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")
    image.save(path)
    return bbox


def pump(root, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            root.update()
        except Exception:  # noqa: BLE001
            break
        time.sleep(0.03)


def capture_dictation(
    root,
    user32,
    theme_name: str,
    size_mode: str,
    state: str = "idle",
    pinned: bool = False,
    name: str | None = None,
) -> dict:
    from dictate import config
    from dictate.ui import DictationWindow, WINDOW_TITLE

    settings = dict(config.DEFAULTS)
    settings.update({"theme": theme_name, "sizeMode": size_mode, "pinned": pinned})
    window = DictationWindow(
        root,
        {"copy": lambda: None, "paste": lambda: None, "escape": lambda: None},
        settings,
    )
    window.set_text(
        "Привет, это локальная диктовка. Текст растёт во время речи, "
        "его можно поправить и скопировать уже исправленную версию."
    )
    if state == "recording":
        window.set_state("recording")
        window.set_level(0.12)
        window.set_elapsed(12_000)
    else:
        window.set_state("idle")
        window.set_notice("Скопировано в буфер")
    window.set_pinned(pinned)
    window.show()
    pump(root, 1.2)
    try:
        hwnd = find_own_window(user32, WINDOW_TITLE)
        if not hwnd:
            return {"ok": False, "error": "окно «Диктовка» не найдено"}
        path = SHOTS / f"{name or ('dictation-' + size_mode + '-' + theme_name)}.png"
        bbox = grab_window(user32, hwnd, path)
        return {"ok": True, "path": str(path), "bbox": bbox}
    finally:
        window.hide()
        window.win.destroy()
        pump(root, 0.2)


def capture_settings(root, user32, theme_name: str) -> dict:
    from dictate import config
    from dictate.ui import SETTINGS_TITLE as TITLE, SettingsWindow

    settings = dict(config.DEFAULTS)
    settings["theme"] = theme_name
    window = SettingsWindow(
        root,
        settings,
        on_save=lambda _data: None,
        on_test_mic=lambda _data: None,
        on_model_status=lambda: ("Загружено: small · cuda · float16 · порт 3211", False),
    )
    window.show()
    pump(root, 1.2)
    try:
        hwnd = find_own_window(user32, TITLE)
        if not hwnd:
            return {"ok": False, "error": "окно «Настройки — Диктовка» не найдено"}
        path = SHOTS / f"settings-{theme_name}.png"
        bbox = grab_window(user32, hwnd, path)
        return {"ok": True, "path": str(path), "bbox": bbox}
    finally:
        window.hide()
        window.destroy()
        pump(root, 0.2)


def main() -> int:
    enable_dpi_awareness()
    try:
        from dictate import theme

        theme.register_fonts()
    except Exception as exc:  # noqa: BLE001 - шрифты не критичны для снимков
        print(f"WARN: шрифты не подключились: {exc}")
    import tkinter as tk

    user32 = _user32()
    root = tk.Tk()
    root.withdraw()
    results: list[tuple[str, dict]] = []
    try:
        results.append(
            (
                "dictation-normal-paper",
                capture_dictation(
                    root, user32, "paper", "normal", state="idle",
                    name="dictation-normal-paper",
                ),
            )
        )
        results.append(
            (
                "dictation-normal-scene",
                capture_dictation(
                    root, user32, "scene", "normal", state="recording",
                    name="dictation-normal-scene",
                ),
            )
        )
        results.append(
            (
                "dictation-compact-scene",
                capture_dictation(
                    root, user32, "scene", "compact", state="idle",
                    name="dictation-compact-scene",
                ),
            )
        )
        for theme_name in ("paper", "scene"):
            results.append(
                (f"settings-{theme_name}", capture_settings(root, user32, theme_name))
            )
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass

    failed = 0
    for name, result in results:
        if result.get("ok"):
            print(f"PASS  {name}: {result['path']} bbox={result['bbox']}")
        else:
            failed += 1
            print(f"FAIL  {name}: {result.get('error')}")
    total = len(results)
    if failed:
        print(f"FAIL {failed}/{total}")
        return 1
    print(f"PASS {total}/{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
