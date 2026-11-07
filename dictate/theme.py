"""Тема WinDictate — дизайн-система СТОРИАРТ (канон 27.10.2026).

Базовые токены взяты из ``СТОРИАРТ-Design-System/DESIGN.md``: белая бумага и тёплый
графит, терракотовый акцент, прямые углы, волосяные линии 1px, без теней. Тёмная тема
(``scene``) — из тёмных полос системы (#14120F / #191919).

Типографика: Onest (400/500/600). В каноне тело набрано DM Sans, но у DM Sans нет
кириллицы — для русского интерфейса всё набрано Onest (кириллица есть, это display-шрифт
системы). Микролейблы — ALL CAPS в квадратных скобках, как в каноне.

Модуль содержит только токены и helpers: окна и формы строит ui.py.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from pathlib import Path
from typing import Any

LOG = logging.getLogger("win-dictate.theme")

APP_DIR = Path(__file__).resolve().parent.parent
ASSETS = APP_DIR / "assets"
FONTS_DIR = ASSETS / "fonts"
ICONS_DIR = ASSETS / "icons"
ICON_PATH = ICONS_DIR / "windictate.ico"
TRAY_ICON_PATH = ICONS_DIR / "windictate-tray-64.png"

APP_NAME = "WinDictate"
APP_TITLE = "Диктовка"
APP_KICKER = "ЛОКАЛЬНОЕ РАСПОЗНАВАНИЕ"
APP_VERSION = "0.2.0"

# --- палитра (DESIGN.md §Color Palette) --------------------------------------
PAPER = "#FFFFFF"
SURFACE_SOFT = "#F2F2F2"
SURFACE_CARD = "#F7F7F6"
WARM_INK = "#24211C"
MUTED_WARM = "#625E58"
MUTED_COOL = "#646A71"
HAIRLINE = "#E5E5E5"
SIGNAL_TERRACOTTA = "#C0603C"
WORK_TERRACOTTA = "#B5532F"
ACTION_TERRACOTTA = "#A45536"
BAND_RED = "#C5501E"
DARK_BG = "#14120F"
DARK_SURFACE = "#191919"

# Тёмная тема: тёмные полосы канона + терракота.
DARK_THEME = {
    "name": "scene",
    "label": "Тёмная",
    "bg": DARK_BG,
    "bg_alt": DARK_SURFACE,
    "surface": DARK_SURFACE,
    "surface_alt": "#1F1D1A",
    "surface_hover": "#26231F",
    "border": "#33302B",
    "text": "#FFFFFF",
    "text_secondary": "#C9C4BC",
    "text_muted": "#9A948C",
    "accent": SIGNAL_TERRACOTTA,
    "accent_strong": SIGNAL_TERRACOTTA,
    "accent_soft": "#7A4433",
    "accent_deep": "#D98D6F",
    "on_accent": "#FFFFFF",
    "danger": "#E0806A",
    "ok": "#7FBF9A",
    "track": "#33302B",
    "is_dark": True,
}

# Светлая тема — основная поверхность канона.
PAPER_THEME = {
    "name": "paper",
    "label": "Бумага",
    "bg": PAPER,
    "bg_alt": SURFACE_SOFT,
    "surface": PAPER,
    "surface_alt": SURFACE_CARD,
    "surface_hover": "#EFEDE7",
    "border": HAIRLINE,
    "text": WARM_INK,
    "text_secondary": MUTED_WARM,
    "text_muted": MUTED_COOL,
    "accent": SIGNAL_TERRACOTTA,
    "accent_strong": ACTION_TERRACOTTA,
    "accent_soft": "#E3C0B2",
    "accent_deep": "#8A3F26",
    "on_accent": "#FFFFFF",
    "danger": "#8D2D2D",
    "ok": "#2F6B4F",
    "track": HAIRLINE,
    "is_dark": False,
}

THEMES = {"paper": PAPER_THEME, "scene": DARK_THEME}
SCENE = DARK_THEME  # обратная совместимость имени
THEME_NAMES = {"paper": "Бумага (светлая)", "scene": "Тёмная"}
DEFAULT_THEME = "paper"

DEFAULT_ACCENT = SIGNAL_TERRACOTTA
# Допустимые акценты — только терракотовая шкала канона (один акцент, без второго цвета).
ACCENT_PRESETS = (
    (SIGNAL_TERRACOTTA, "Сигнальная терракота"),
    (ACTION_TERRACOTTA, "Действие"),
    (WORK_TERRACOTTA, "Работа"),
    (BAND_RED, "Плашка"),
)

# --- геометрия и ритм (DESIGN.md §Layout) ------------------------------------
RADIUS = {
    "window": 0,   # прямой угол — норма
    "card": 0,
    "button": 0,
    "field": 2,    # 2–3 px только у чипов и полей
    "chip": 2,
    "pill": 999,   # только иконочные кнопки
}
SPACE = {"1": 4, "2": 8, "3": 12, "4": 16, "5": 24, "6": 32, "7": 48}
BORDER = 1  # толщина волосяной линии

# --- шрифты ------------------------------------------------------------------
FONT_DISPLAY = "Onest"
FONT_TEXT = "Onest"
FONT_MONO = "Onest"  # микролейблы и цифры — тоже Onest (без второй гарнитуры)
FALLBACK_TEXT = "Segoe UI"
FONT_ICONS = "Segoe Fluent Icons"
FONT_ICONS_FALLBACK = "Segoe MDL2 Assets"

FONT_FILES = ("Onest-400.ttf", "Onest-500.ttf", "Onest-600.ttf")

# Глифы Segoe Fluent Icons / Segoe MDL2 Assets (стабильные коды).
ICONS = {
    "mic": "\uE720",
    "stop": "\uE71A",
    "copy": "\uE8C8",
    "paste": "\uE77F",
    "settings": "\uE713",
    "close": "\uE711",
    "pin": "\uE718",
    "unpin": "\uE77A",
    "new": "\uE710",
    "reset": "\uE777",
    "refresh": "\uE72C",
    "save": "\uE74E",
    "edit": "\uE70F",
    "delete": "\uE74D",
    "check": "\uE73E",
    "chevron_down": "\uE70D",
    "model": "\uE9D9",
    "appearance": "\uE790",
    "keyboard": "\uE765",
    "volume": "\uE767",
    "info": "\uE946",
    "warning": "\uE7BA",
    "expand": "\uE740",
    "collapse": "\uE73F",
    "arrow_right": "\u2192",
    "arrow_up_right": "\u2197",
}

_fonts_registered: bool | None = None


def _add_fonts() -> int:
    if sys.platform != "win32":
        return 0
    gdi32 = ctypes.windll.gdi32
    added = 0
    for name in FONT_FILES:
        path = FONTS_DIR / name
        if not path.is_file():
            continue
        try:
            added += int(gdi32.AddFontResourceExW(str(path), 0x10, None) or 0)
        except OSError as exc:  # pragma: no cover - platform nicety
            LOG.warning("Не удалось подключить шрифт %s: %s", name, exc)
    return added


def register_fonts() -> bool:
    """Подключает шрифты Onest к процессу; True, если семейство доступно."""
    global _fonts_registered
    if _fonts_registered is not None:
        return _fonts_registered
    added = _add_fonts()
    try:
        import tkinter.font as tkfont

        families = set(tkfont.families())
        ok = FONT_DISPLAY in families or FONT_TEXT in families
    except Exception:  # noqa: BLE001 - окно ещё не создано
        ok = added > 0
    _fonts_registered = bool(ok)
    LOG.info("Шрифты подключены: %s", added)
    return _fonts_registered


def _family(preferred: str) -> str:
    try:
        import tkinter.font as tkfont

        if preferred in set(tkfont.families()):
            return preferred
    except Exception:  # noqa: BLE001
        pass
    return FALLBACK_TEXT


def font_display(size: int = 14, weight: str = "bold") -> tuple[str, int, str]:
    return (_family(FONT_DISPLAY), size, weight)


def font_text(size: int = 11, weight: str = "normal") -> tuple[str, int, str]:
    return (_family(FONT_TEXT), size, weight)


def font_mono(size: int = 10, weight: str = "normal") -> tuple[str, int, str]:
    return (_family(FONT_MONO), size, weight)


def icon_font(size: int = 12, weight: str = "normal") -> tuple[str, int, str]:
    """Шрифт иконок Windows; если его нет — обычный текстовый."""
    try:
        import tkinter.font as tkfont

        families = set(tkfont.families())
        for candidate in (FONT_ICONS, FONT_ICONS_FALLBACK):
            if candidate in families:
                return (candidate, size, weight)
    except Exception:  # noqa: BLE001
        pass
    return (FONT_TEXT, size, weight)


def icon(name: str) -> str:
    return ICONS.get(name, "")


# Микролейблы канона: ALL CAPS в квадратных скобках, трекинг 1.5 px.
THIN_SPACE = "\u2009"


def micro(text: str) -> str:
    """«Распознавание» → «[ Р А С П О З Н А В А Н И Е ]» (регистр и трекинг канона)."""
    body = THIN_SPACE.join(text.upper())
    return f"[ {body} ]"


def tracked(text: str) -> str:
    """Регистр + разреженные буквы без скобок (для подписей и KPI)."""
    return THIN_SPACE.join(text.upper())


# --- цвета -------------------------------------------------------------------
def normalize_hex(value: Any) -> str | None:
    """'#abc' / 'aabbcc' / '#AABBCC' -> '#AABBCC'; None для мусора."""
    if not isinstance(value, str):
        return None
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return "#" + text.upper()


def _rgb(color: str) -> tuple[int, int, int]:
    text = color.lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, int(round(value)))):02X}" for value in rgb)


def mix(color: str, other: str, amount: float) -> str:
    """Смешивает два цвета: amount=0 → color, amount=1 → other."""
    a = _rgb(color)
    b = _rgb(other)
    return _hex(tuple(a[i] + (b[i] - a[i]) * amount for i in range(3)))


def luminance(color: str) -> float:
    """Относительная яркость 0..1 (для выбора цвета текста на заливке)."""
    r, g, b = (value / 255.0 for value in _rgb(color))

    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def accent_palette(base: str) -> dict[str, str]:
    """Производные акцента: заливка, hover/мелкий текст, мягкий фон, контрастный текст."""
    base = normalize_hex(base) or DEFAULT_ACCENT
    return {
        "accent": base,
        "accent_strong": mix(base, "#000000", 0.12),
        "accent_soft": mix(base, "#FFFFFF", 0.62),
        "accent_deep": mix(base, "#000000", 0.30),
        "on_accent": "#FFFFFF" if luminance(base) < 0.62 else "#14120F",
    }


def colors(theme: str = DEFAULT_THEME, accent: str | None = None) -> dict[str, Any]:
    """Семантическая палитра темы с опциональным пользовательским акцентом."""
    palette = dict(THEMES.get(str(theme), PAPER_THEME))
    chosen = normalize_hex(accent)
    if chosen:
        palette.update(accent_palette(chosen))
    return palette


# --- Tk helpers --------------------------------------------------------------
def icon_photo(master: Any, theme: str = DEFAULT_THEME, size: int = 26) -> Any:
    """Иконка приложения для шапки (PNG); None, если нет Pillow/ассетов."""
    name = "mic-scene.png" if str(theme) == "scene" else "mic-paper.png"
    path = ICONS_DIR / name
    try:
        from PIL import Image, ImageTk

        image = Image.open(path).convert("RGBA")
        ratio = size / max(image.size)
        image = image.resize(
            (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
            Image.LANCZOS,
        )
        return ImageTk.PhotoImage(image, master=master)
    except Exception as exc:  # noqa: BLE001 - интерфейс деградирует до глифа
        LOG.info("Иконка приложения недоступна: %s", exc)
        return None


def tooltip_icon(size: int = 64) -> Any:
    """Pillow-изображение иконки (для трея)."""
    from PIL import Image

    image = Image.open(ICONS_DIR / "mic-scene.png").convert("RGBA")
    return image.resize((size, size), Image.LANCZOS)


def init_ttk(root: Any, theme: str = DEFAULT_THEME, accent: str | None = None) -> Any:
    """ttk-стили под канон: прямые углы, волосяные рамки, терракотовый фокус."""
    from tkinter import ttk

    palette = colors(theme, accent)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:  # noqa: BLE001
        pass
    style.configure(
        ".",
        background=palette["bg"],
        foreground=palette["text"],
        fieldbackground=palette["surface_alt"],
        bordercolor=palette["border"],
        lightcolor=palette["surface_alt"],
        darkcolor=palette["surface_alt"],
        troughcolor=palette["track"],
        focuscolor=palette["accent"],
        font=font_text(11),
        borderwidth=1,
        relief="flat",
    )
    style.configure(
        "TCombobox",
        fieldbackground=palette["surface_alt"],
        background=palette["surface_alt"],
        foreground=palette["text"],
        arrowcolor=palette["text_secondary"],
        bordercolor=palette["border"],
        lightcolor=palette["surface_alt"],
        darkcolor=palette["surface_alt"],
        selectbackground=palette["surface_alt"],
        selectforeground=palette["text"],
        padding=(8, 5),
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", palette["surface_alt"]), ("disabled", palette["bg"])],
        foreground=[("disabled", palette["text_muted"])],
        bordercolor=[("focus", palette["accent"])],
        arrowcolor=[("active", palette["accent"])],
    )
    style.configure(
        "TEntry",
        fieldbackground=palette["surface_alt"],
        foreground=palette["text"],
        insertcolor=palette["accent"],
        bordercolor=palette["border"],
        lightcolor=palette["surface_alt"],
        darkcolor=palette["surface_alt"],
        padding=(8, 5),
    )
    style.map("TEntry", bordercolor=[("focus", palette["accent"])])
    style.configure(
        "Sa.TCheckbutton",
        background=palette["bg"],
        foreground=palette["text_secondary"],
        indicatorbackground=palette["surface_alt"],
        indicatorforeground=palette["accent"],
        focuscolor=palette["accent"],
    )
    style.map(
        "Sa.TCheckbutton",
        indicatorbackground=[("selected", palette["accent"]), ("active", palette["surface_hover"])],
        foreground=[("active", palette["text"])],
    )
    style.configure(
        "Sa.Horizontal.TScale",
        background=palette["bg"],
        troughcolor=palette["track"],
        sliderthickness=14,
        bordercolor=palette["border"],
    )
    try:
        root.option_add("*TCombobox*Listbox.background", palette["surface_alt"])
        root.option_add("*TCombobox*Listbox.foreground", palette["text"])
        root.option_add("*TCombobox*Listbox.selectBackground", palette["accent_strong"])
        root.option_add("*TCombobox*Listbox.selectForeground", palette["on_accent"])
        root.option_add("*TCombobox*Listbox.font", font_text(10))
    except Exception:  # noqa: BLE001
        pass
    return style


def apply_window_icon(window: Any) -> None:
    """Ставит иконку приложения окну."""
    try:
        window.iconbitmap(default=str(ICON_PATH))
    except Exception:  # noqa: BLE001 - иконка не критична
        pass


__all__ = [
    "ACCENT_PRESETS",
    "APP_KICKER",
    "APP_NAME",
    "APP_DIR",
    "APP_TITLE",
    "APP_VERSION",
    "BORDER",
    "DEFAULT_ACCENT",
    "DEFAULT_THEME",
    "FONT_DISPLAY",
    "FONT_ICONS",
    "FONT_ICONS_FALLBACK",
    "FONT_MONO",
    "FONT_TEXT",
    "ICON_PATH",
    "ICONS",
    "PAPER_THEME",
    "RADIUS",
    "SCENE",
    "SPACE",
    "THEMES",
    "THEME_NAMES",
    "TRAY_ICON_PATH",
    "accent_palette",
    "apply_window_icon",
    "colors",
    "font_display",
    "font_mono",
    "font_text",
    "icon",
    "icon_font",
    "icon_photo",
    "init_ttk",
    "luminance",
    "micro",
    "mix",
    "normalize_hex",
    "register_fonts",
    "tooltip_icon",
    "tracked",
]
