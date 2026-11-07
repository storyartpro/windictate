"""Иконка в системном трее приложения «Диктовка».

pystray и Pillow — мягкая зависимость: если их нет, приложение работает без
трея. Колбэки трея вызываются в его собственном потоке, поэтому app.py
маршалит их в Tk-поток через root.after.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from dictate import autostart, theme
from dictate.hotkeys import format_hotkey

LOG = logging.getLogger("win-dictate.tray")

_IMAGE: Any = None


def _create_image() -> Any:
    """Иконка приложения (Pillow RGBA); кэшируется."""
    global _IMAGE
    if _IMAGE is not None:
        return _IMAGE
    from PIL import Image

    try:
        if theme.TRAY_ICON_PATH.is_file():
            _IMAGE = Image.open(theme.TRAY_ICON_PATH).convert("RGBA")
            return _IMAGE
    except Exception as exc:  # noqa: BLE001 - иконка косметика
        LOG.info("Иконка трея недоступна: %s", exc)
    # Запасной вариант: тёмный модуль с акцентом из токенов.
    from PIL import ImageDraw

    palette = theme.colors("scene")
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (2, 2, size - 2, size - 2),
        fill=palette["surface_alt"],
        outline=palette["border"],
        width=2,
    )
    draw.rectangle((24, 14, 40, 36), fill=palette["text"])
    draw.arc((18, 24, 46, 46), start=0, end=180, fill=palette["text"], width=3)
    draw.line((32, 46, 32, 52), fill=palette["accent"], width=3)
    draw.line((24, 52, 40, 52), fill=palette["accent"], width=3)
    _IMAGE = image
    return image


class TrayIcon:
    """Меню трея: диктовка, настройки, автозапуск, выход."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._icon: Any = None
        self._thread: threading.Thread | None = None
        self.available = False
        self.last_error: str | None = None

    # ------------------------------------------------------------------ public

    def start(self) -> bool:
        try:
            import pystray  # noqa: F401 - проверка наличия
        except Exception as exc:  # noqa: BLE001 - без pystray живём дальше
            self.last_error = "трей недоступен: нет pystray"
            LOG.info("Трей недоступен (%s), продолжаем без него", exc)
            return False
        try:
            import pystray

            icon = pystray.Icon(
                "WinDictate",
                _create_image(),
                theme.APP_TITLE,
                self._build_menu(pystray),
            )
            self._icon = icon
            thread = threading.Thread(
                target=self._run_icon, args=(icon,), name="WinDictateTray", daemon=True
            )
            self._thread = thread
            thread.start()
            self.available = True
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"трей недоступен: {exc}"
            LOG.warning("Не удалось запустить трей: %s", exc)
            self._icon = None
            return False

    def stop(self) -> None:
        icon = self._icon
        self._icon = None
        if icon is not None:
            try:
                icon.stop()
            except Exception:  # noqa: BLE001
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self.available = False

    # ------------------------------------------------------------------ internals

    def _run_icon(self, icon: Any) -> None:
        try:
            icon.run()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"трей остановлен: {exc}"
            LOG.warning("Цикл трея завершился ошибкой: %s", exc)

    def _build_menu(self, pystray: Any) -> Any:
        hotkey = "Ctrl+Alt+Space"
        settings = getattr(self.app, "settings", None)
        if isinstance(settings, dict) and settings.get("hotkey"):
            hotkey = format_hotkey(str(settings["hotkey"]))
        menu = pystray.Menu(
            pystray.MenuItem(
                f"Диктовка ({hotkey})", self._on_dictate, default=True
            ),
            pystray.MenuItem("Показать окно", self._on_show_window),
            pystray.MenuItem("История диктовок", self._on_history),
            pystray.MenuItem("Настройки", self._on_settings),
            pystray.MenuItem(
                "Запускать с Windows",
                self._on_toggle_autostart,
                checked=lambda _item: autostart.is_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self._on_quit),
        )
        return menu

    def _call(self, name: str, *args: Any) -> None:
        callback = getattr(self.app, name, None)
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # noqa: BLE001 - трей не должен падать
            LOG.exception("Ошибка обработчика трея: %s", name)

    def _on_dictate(self, *_args: Any) -> None:
        self._call("open_last")

    def _on_show_window(self, *_args: Any) -> None:
        self._call("show_window")

    def _on_history(self, *_args: Any) -> None:
        self._call("open_history")

    def _on_settings(self, *_args: Any) -> None:
        self._call("open_settings")

    def _on_toggle_autostart(self, *_args: Any) -> None:
        enabled = not autostart.is_enabled()
        self._call("toggle_autostart", enabled)

    def _on_quit(self, *_args: Any) -> None:
        self._call("quit")
