"""Реальные скриншоты UI WinDictate 0.2 по канону дизайн-системы.

Снимает PNG в ``win-dictate/screenshots/``:
dictation-normal-paper/scene, dictation-compact-scene, settings-paper/scene,
settings-llm-paper (секции 07–08) и history-paper (окно истории).

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
SETTINGS_TITLE = "Настройки — Диктовка"


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


def capture_settings(
    root,
    user32,
    theme_name: str,
    *,
    scroll_bottom: bool = False,
    llm: bool = False,
) -> dict:
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
        on_history=lambda: None,
        on_check_key=lambda _key, _model: (True, "Ключ работает."),
    )
    window.show()
    pump(root, 0.8)
    if scroll_bottom:
        try:
            window.canvas.yview_moveto(1.0)
        except Exception:  # noqa: BLE001
            pass
        pump(root, 0.6)
    try:
        hwnd = find_own_window(user32, TITLE)
        if not hwnd:
            return {"ok": False, "error": "окно «Настройки — Диктовка» не найдено"}
        suffix = "llm" if llm else theme_name
        path = SHOTS / f"settings-{suffix}-{theme_name}.png" if llm else SHOTS / f"settings-{theme_name}.png"
        bbox = grab_window(user32, hwnd, path)
        return {"ok": True, "path": str(path), "bbox": bbox}
    finally:
        window.hide()
        window.destroy()
        pump(root, 0.2)


class _DemoHistoryStore:
    """Маленькое in-memory хранилище для скриншота окна истории."""

    def __init__(self) -> None:
        self._sessions = [
            {
                "id": "demo00000001",
                "created_at": "2026-11-07T20:14:00",
                "ts": 1.0,
                "duration_ms": 12_400,
                "chars": 58,
                "text": "Привет, это локальная диктовка. Текст можно поправить и скопировать.",
                "model": "small",
                "source": "dictation",
                "edited": False,
            },
            {
                "id": "demo00000002",
                "created_at": "2026-11-07T19:02:00",
                "ts": 2.0,
                "duration_ms": 7_800,
                "chars": 42,
                "text": "Короткая заметка для проверки истории диктовок.",
                "model": "small",
                "source": "dictation",
                "edited": True,
            },
            {
                "id": "demo00000003",
                "created_at": "2026-11-06T11:45:00",
                "ts": 3.0,
                "duration_ms": 3_200,
                "chars": 28,
                "text": "Идея: добавить пресеты для улучшения текста.",
                "model": "small",
                "source": "dictation",
                "edited": False,
            },
        ]

    def list(self, query: str = "") -> list[dict]:
        query = (query or "").lower()
        return [dict(item) for item in self._sessions if query in item["text"].lower()]

    def update_text(self, session_id: str, text: str) -> bool:
        for item in self._sessions:
            if item["id"] == session_id:
                item["text"] = text
                item["edited"] = True
                item["chars"] = len(text)
                return True
        return False

    def delete(self, session_id: str) -> bool:
        before = len(self._sessions)
        self._sessions = [item for item in self._sessions if item["id"] != session_id]
        return len(self._sessions) != before

    def clear(self) -> int:
        count = len(self._sessions)
        self._sessions = []
        return count


def capture_history(root, user32, theme_name: str) -> dict:
    from dictate import config
    from dictate.ui import HistoryWindow

    settings = dict(config.DEFAULTS)
    settings["theme"] = theme_name
    palette = __import__("dictate.theme", fromlist=["theme"]).colors(theme_name)
    window = HistoryWindow(
        root,
        _DemoHistoryStore(),
        palette=palette,
        ui_scale=float(settings.get("uiScale", 1.0)),
    )
    window.show()
    try:
        window.attributes("-topmost", True)
        window.lift()
        window.focus_force()
    except Exception:  # noqa: BLE001
        pass
    pump(root, 1.0)
    try:
        hwnd = find_own_window(user32, window.TITLE)
        if not hwnd:
            return {"ok": False, "error": "окно «История — Диктовка» не найдено"}
        path = SHOTS / f"history-{theme_name}.png"
        bbox = grab_window(user32, hwnd, path)
        return {"ok": True, "path": str(path), "bbox": bbox}
    finally:
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass
        pump(root, 0.2)


def main() -> int:
    enable_dpi_awareness()
    # Нейтральные пути для публичных скриншотов: без домашних dev-каталогов.
    os.environ["WIN_DICTATE_DATA"] = r"C:\Apps\WinDictate\data"
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
        results.append(
            (
                "settings-llm-paper",
                capture_settings(root, user32, "paper", scroll_bottom=True, llm=True),
            )
        )
        results.append(
            ("history-paper", capture_history(root, user32, "paper"))
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
