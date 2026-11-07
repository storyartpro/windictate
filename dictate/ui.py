"""Интерфейс WinDictate 0.1 — канон дизайн-системы студии (27.10.2026).

Бумага по умолчанию, терракотовый акцент, прямые углы, волосяные линии 1px,
без теней. Микролейблы — ALL CAPS в квадратных скобках (``theme.micro``).
Публичный API окон сохранён для app.py и live_gui.py.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any, Callable

from dictate import theme

LOG = logging.getLogger("win-dictate.ui")

WINDOW_TITLE = "Диктовка"
SETTINGS_TITLE = "Настройки — Диктовка"
THEME_NAMES = ("paper", "scene")

SIZE_MODES = ("compact", "normal", "wide")
SIZE_SIZES = {
    "compact": (560, 184),
    "normal": (860, 400),
    "wide": (1120, 480),
}
SIZE_LABELS = {
    "compact": "Компактный",
    "normal": "Обычный",
    "wide": "Широкий",
}
SIZE_VALUES = {label: value for value, label in SIZE_LABELS.items()}

STT_DEVICE_LABELS = {"Авто (auto)": "auto", "GPU (CUDA)": "cuda", "CPU": "cpu"}
STT_DEVICE_VALUES = {value: label for label, value in STT_DEVICE_LABELS.items()}

STATUS_RECORDING = theme.micro("Запись")
STATUS_FINALIZING = theme.micro("Распознаю")
STATUS_IDLE = theme.micro("Готово")

HINT_IDLE = "   ".join(
    (
        theme.micro("Enter — копировать"),
        theme.micro("Ctrl+Enter — вставить"),
        theme.micro("Esc — закрыть"),
    )
)
HINT_RECORDING = "   ".join(
    (theme.micro("Esc — отмена"), theme.micro("Ctrl+Alt+Space — остановить"))
)
HINT_EDIT = theme.micro("Текст можно править — копируется текущая версия")

THEME_LABELS = theme.THEME_NAMES
THEME_VALUES = {label: value for value, label in THEME_LABELS.items()}

HOTKEY_CANDIDATES = (
    "Ctrl+Alt+Shift+Space",
    "Ctrl+Alt+D",
    "Ctrl+Alt+F9",
    "Ctrl+Alt+Shift+F10",
    "Ctrl+Alt+Shift+F11",
    "Ctrl+Alt+Shift+F12",
)

TOP_BAR_HEIGHT = 54
BOTTOM_BAR_HEIGHT = 56


# --------------------------------------------------------------------------- helpers


def normalize_theme(name: Any) -> str:
    return str(name) if str(name) in THEME_NAMES else theme.DEFAULT_THEME


def normalize_size_mode(name: Any) -> str:
    return str(name) if str(name) in SIZE_MODES else "normal"


def clamp_ui_scale(value: Any) -> float:
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.8, min(1.6, scale))


def _px(value: float, scale: float) -> int:
    return max(1, int(round(value * scale)))


def format_elapsed(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    return f"{total // 60}:{total % 60:02d}"


def _blend(color_a: str, color_b: str, ratio: float) -> str:
    ratio = max(0.0, min(1.0, ratio))

    def rgb(value: str) -> tuple[int, int, int]:
        text = value.lstrip("#")
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)

    a = rgb(color_a)
    b = rgb(color_b)
    mixed = tuple(int(round(a[i] + (b[i] - a[i]) * ratio)) for i in range(3))
    return "#" + "".join(f"{max(0, min(255, value)):02X}" for value in mixed)


def _rounded_polygon(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
    **kwargs: Any,
) -> int:
    r = max(0.0, min(float(radius), (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    points = [
        x1 + r, y1,
        x2 - r, y1,
        x2, y1,
        x2, y1 + r,
        x2, y2 - r,
        x2, y2,
        x2 - r, y2,
        x1 + r, y2,
        x1, y2,
        x1, y2 - r,
        x1, y1 + r,
        x1, y1,
    ]
    return int(canvas.create_polygon(points, smooth=True, **kwargs))


_ICONS_AVAILABLE: bool | None = None


def icons_available() -> bool:
    """True, если в системе есть иконочный шрифт Windows."""
    global _ICONS_AVAILABLE
    if _ICONS_AVAILABLE is None:
        try:
            _ICONS_AVAILABLE = theme.icon_font(12)[0] != theme.FONT_TEXT
        except Exception:  # noqa: BLE001
            _ICONS_AVAILABLE = False
    return bool(_ICONS_AVAILABLE)


def icon_glyph(name: str) -> str:
    return theme.icon(name) if icons_available() else ""


class Tooltip:
    """Простой всплывающий хинт для иконочных кнопок."""

    def __init__(self, widget: tk.Widget, text: str, palette: dict[str, Any]) -> None:
        self.widget = widget
        self.text = text
        self.palette = palette
        self._job: str | None = None
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        try:
            self._job = self.widget.after(450, self._show)
        except tk.TclError:
            self._job = None

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _show(self) -> None:
        self._job = None
        if self._window is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 8
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            window = tk.Toplevel(self.widget)
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            window.configure(bg=self.palette["border"])
            label = tk.Label(
                window,
                text=self.text,
                bg=self.palette["surface_alt"],
                fg=self.palette["text"],
                font=theme.font_text(9),
                padx=8,
                pady=4,
            )
            label.pack(padx=1, pady=1)
            window.geometry(f"+{x}+{y}")
            self._window = window
        except Exception:  # noqa: BLE001 - подсказка не критична
            self._window = None

    def _hide(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        window = self._window
        self._window = None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass


class BrandButton(tk.Canvas):
    """Кнопка канона: прямые углы, 1 px линии, CTA/secondary/ghost/icon."""

    KINDS = ("cta", "secondary", "ghost", "icon")

    def __init__(
        self,
        master: tk.Misc,
        text: str = "",
        command: Callable[[], Any] | None = None,
        *,
        palette: dict[str, Any],
        kind: str = "secondary",
        font: tuple[str, int, str] | None = None,
        padding: int = 12,
        height: int = 32,
        radius: int = 0,
        icon: str | None = None,
        icon_only: bool = False,
        tooltip: str | None = None,
    ) -> None:
        self.palette = palette
        self.kind = kind if kind in self.KINDS else "secondary"
        self._text = str(text)
        self._icon_name = icon
        self._icon_only = bool(icon_only)
        self._command = command
        self._enabled = True
        self._hover = False
        self._font = font or theme.font_text(
            11, "bold" if self.kind == "cta" else "normal"
        )
        self._padding = int(padding)
        self._height = int(height)
        self._radius = int(radius)
        self._glyph = icon_glyph(icon) if icon else ""
        self._label = self._compose_label()
        parent_bg = self._parent_background(master)
        self._parent_bg = parent_bg
        self.fill_color = self._fill()
        self.text_color = self._text_color()
        width = self._label_width()
        super().__init__(
            master,
            width=width,
            height=self._height,
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
            takefocus=0,
            cursor="hand2",
        )
        self.tooltip: Tooltip | None = None
        if tooltip:
            self.tooltip = Tooltip(self, tooltip, palette)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._draw()

    # -- compat ------------------------------------------------------------

    @staticmethod
    def _parent_background(master: tk.Misc) -> str:
        try:
            return str(master.cget("bg"))
        except Exception:  # noqa: BLE001
            return "#000000"

    def _compose_label(self) -> str:
        if not self._glyph:
            return self._text
        if self._icon_only:
            return self._glyph
        if not self._text:
            return self._glyph
        return f"{self._glyph}  {self._text}"

    def _label_width(self) -> int:
        measured = self._measure_text()
        if self._glyph:
            # Иконочный глиф может измеряться в 0 в некоторых шрифтах —
            # резервируем минимум по кеглю, иначе кнопка обрезает иконку.
            measured = max(measured, int(self._font[1]))
        return measured + 2 * self._padding

    def cget(self, key: str) -> Any:  # noqa: D401 - совместимость
        if key == "text":
            return self._text
        if key in ("bg", "background", "fill"):
            return self.fill_color
        if key == "state":
            return "normal" if self._enabled else "disabled"
        if key == "font":
            return self._font
        return super().cget(key)

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:  # noqa: N802
        state = kwargs.pop("state", None)
        text = kwargs.pop("text", None)
        if state is not None:
            self.set_enabled(str(state) != "disabled")
        if text is not None:
            self.set_text(str(text))
        if kwargs or cnf is not None:
            return super().configure(cnf, **kwargs)
        return None

    def invoke(self) -> None:
        if self._enabled and self._command is not None:
            try:
                self._command()
            except Exception:  # noqa: BLE001
                LOG.exception("Ошибка кнопки")

    # -- state -------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        try:
            tk.Canvas.configure(self, cursor="hand2" if self._enabled else "arrow")
        except tk.TclError:
            pass
        self._draw()

    def set_text(self, text: str) -> None:
        self._text = str(text)
        self._label = self._compose_label()
        width = self._label_width()
        try:
            tk.Canvas.configure(self, width=width)
        except tk.TclError:
            pass
        self._draw()

    def _measure_text(self) -> int:
        try:
            return int(tkfont.Font(font=self._font).measure(self._label))
        except Exception:  # noqa: BLE001
            return 8 * len(self._label)

    # -- colors ------------------------------------------------------------

    def _fill(self) -> str:
        palette = self.palette
        if not self._enabled:
            return palette["bg_alt"]
        if self.kind == "cta":
            return palette["accent_strong"] if self._hover else palette["accent"]
        if self.kind == "secondary":
            return palette["surface_alt"] if self._hover else self._parent_bg
        if self.kind == "icon":
            return palette["surface_alt"] if self._hover else self._parent_bg
        return self._parent_bg

    def _text_color(self) -> str:
        palette = self.palette
        if not self._enabled:
            return palette["text_muted"]
        if self.kind == "cta":
            return palette["on_accent"]
        if self.kind in ("secondary", "icon"):
            return palette["text"] if self._hover else palette["text_secondary"]
        return palette["text"] if self._hover else palette["text_secondary"]

    def _border_color(self) -> str:
        if not self._enabled:
            return self.palette["border"]
        if self.kind == "secondary":
            return self.palette["border"]
        if self.kind == "icon" and self._hover:
            return self.palette["border"]
        return ""

    def _draw(self) -> None:
        self.delete("all")
        self.fill_color = self._fill()
        self.text_color = self._text_color()
        width = max(2, int(self.winfo_reqwidth()))
        height = max(2, int(self.winfo_reqheight()))
        border = self._border_color()
        if border:
            self.create_rectangle(
                0, 0, width - 1, height - 1,
                fill=self.fill_color, outline=border, width=1,
            )
        else:
            self.create_rectangle(
                0, 0, width, height, fill=self.fill_color, outline=""
            )
        self.create_text(
            width / 2,
            height / 2,
            text=self._label,
            fill=self.text_color,
            font=self._font,
        )

    # -- events ------------------------------------------------------------

    def _on_click(self, _event: tk.Event | None = None) -> None:
        self.invoke()

    def _on_enter(self, _event: tk.Event | None = None) -> None:
        if not self._enabled:
            return
        self._hover = True
        self._draw()

    def _on_leave(self, _event: tk.Event | None = None) -> None:
        if not self._hover:
            return
        self._hover = False
        self._draw()


class SuggestionsWindow(tk.Toplevel):
    """Модальное окно «спорных слов»: вариант на каждое слово или «оставить как есть»."""

    TITLE = "Спорные слова — Диктовка"

    def __init__(
        self,
        master: tk.Misc,
        suggestions: list[Any],
        palette: dict[str, Any],
        *,
        on_apply: Callable[[dict[int, str | None]], Any] | None = None,
        ui_scale: float = 1.0,
    ) -> None:
        super().__init__(master)
        self.palette = palette
        self.suggestions = list(suggestions)
        self.choices: dict[int, str | None] = {index: None for index in range(len(self.suggestions))}
        self.on_apply = on_apply
        self._scale = clamp_ui_scale(ui_scale)
        self._buttons: dict[tuple[int, int | None], BrandButton] = {}
        self.title(self.TITLE)
        self.configure(bg=palette["bg"])
        width = _px(660, self._scale)
        height = _px(520, self._scale)
        self.geometry(f"{width}x{height}")
        try:
            self.transient(master.winfo_toplevel())
            self.grab_set()
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._build()
        self.bind("<Escape>", lambda _event: self.destroy())

    def _build(self) -> None:
        palette = self.palette
        scale = self._scale
        outer = tk.Frame(self, bg=palette["bg"])
        outer.pack(fill="both", expand=True)

        header = tk.Frame(
            outer, bg=palette["bg"], height=_px(54, scale),
            padx=_px(16, scale), pady=_px(8, scale),
        )
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header, text=theme.micro("Спорные слова"), bg=palette["bg"],
            fg=palette["accent"], font=theme.font_text(10, "bold"),
        ).pack(side="left")
        tk.Label(
            header,
            text=theme.micro(f"{len(self.suggestions)} слов"),
            bg=palette["bg"],
            fg=palette["text_muted"],
            font=theme.font_text(9),
        ).pack(side="right")
        tk.Frame(outer, bg=palette["border"], height=1).pack(fill="x")

        holder = tk.Frame(outer, bg=palette["bg"])
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, bg=palette["bg"], highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=palette["bg"])
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.columnconfigure(0, weight=1)
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=int(event.width)))
        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        self.canvas = canvas
        self.scrollbar = scrollbar

        for index, suggestion in enumerate(self.suggestions):
            self._build_row(body, index, suggestion)

        tk.Frame(outer, bg=palette["border"], height=1).pack(fill="x")
        bottom = tk.Frame(
            outer, bg=palette["bg"], padx=_px(16, scale), pady=_px(8, scale)
        )
        bottom.pack(fill="x")
        tk.Label(
            bottom,
            text=theme.micro("Выберите вариант или оставьте как есть"),
            bg=palette["bg"],
            fg=palette["text_muted"],
            font=theme.font_text(9),
        ).pack(side="left")
        self.apply_button = BrandButton(
            bottom, "ПРИМЕНИТЬ ВЫБРАННОЕ", self.apply, palette=palette,
            kind="cta", height=_px(40, scale), font=theme.font_display(11, "bold"),
        )
        self.apply_button.pack(side="right", padx=(8, 0))
        self.cancel_button = BrandButton(
            bottom, "ОТМЕНА", self.destroy, palette=palette,
            kind="secondary", height=_px(40, scale), font=theme.font_text(11),
        )
        self.cancel_button.pack(side="right")

    def _build_row(self, body: tk.Frame, index: int, suggestion: Any) -> None:
        palette = self.palette
        scale = self._scale
        row = tk.Frame(body, bg=palette["bg"], padx=_px(16, scale), pady=_px(10, scale))
        row.grid(row=index, column=0, sticky="ew")
        row.columnconfigure(1, weight=1)
        tk.Frame(body, bg=palette["border"], height=1).grid(
            row=index, column=0, sticky="sew"
        )
        word = str(getattr(suggestion, "word", "") or "")
        reason = str(getattr(suggestion, "reason", "") or "")
        tk.Label(
            row, text=word, bg=palette["bg"], fg=palette["text"],
            font=theme.font_display(14, "bold"), anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=(0, _px(12, scale)))
        tk.Label(
            row, text=reason, bg=palette["bg"], fg=palette["text_muted"],
            font=theme.font_text(10), anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=(0, _px(12, scale)))
        options = tk.Frame(row, bg=palette["bg"])
        options.grid(row=0, column=1, rowspan=2, sticky="w")
        variants = list(getattr(suggestion, "variants", []) or [])
        for option_index, variant in enumerate(variants):
            button = BrandButton(
                options, str(variant), palette=palette, kind="secondary",
                font=theme.font_text(11), height=_px(30, scale),
                command=lambda idx=index, value=str(variant): self._choose(idx, value),
            )
            button.pack(side="left", padx=(0, _px(6, scale)))
            self._buttons[(index, variant)] = button
        keep = BrandButton(
            options, "ОСТАВИТЬ КАК ЕСТЬ", palette=palette, kind="ghost",
            font=theme.font_text(10), height=_px(30, scale),
            command=lambda idx=index: self._choose(idx, None),
        )
        keep.pack(side="left")
        self._buttons[(index, None)] = keep

    def _choose(self, index: int, value: str | None) -> None:
        self.choices[index] = value
        for (row_index, option), button in self._buttons.items():
            if row_index != index:
                continue
            selected = option == value
            button.kind = "cta" if selected else (
                "ghost" if option is None else "secondary"
            )
            button._draw()

    def apply(self) -> None:
        callback = self.on_apply
        choices = dict(self.choices)
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        if callback is not None:
            try:
                callback(choices)
            except Exception:  # noqa: BLE001
                LOG.exception("Ошибка применения спорных слов")


class HistoryWindow(tk.Toplevel):
    """Окно истории диктовок: список сессий, поиск, редактирование текста."""

    TITLE = "История — Диктовка"
    WIDTH = 860
    HEIGHT = 520

    def __init__(
        self,
        master: tk.Misc,
        store: Any,
        *,
        palette: dict[str, Any] | None = None,
        on_close: Callable[[], Any] | None = None,
        ui_scale: float = 1.0,
    ) -> None:
        super().__init__(master)
        self.win = self  # удобный алиас, как у DictationWindow/SettingsWindow
        self.store = store
        self.palette = palette or theme.colors(theme.DEFAULT_THEME)
        self.on_close = on_close
        self._scale = clamp_ui_scale(ui_scale)
        self._sessions: list[dict[str, Any]] = []
        self._current_id: str | None = None
        self._dirty = False
        self.title(self.TITLE)
        self.configure(bg=self.palette["bg"])
        width = _px(self.WIDTH, self._scale)
        height = _px(self.HEIGHT, self._scale)
        self.geometry(f"{width}x{height}")
        self.minsize(_px(620, self._scale), _px(380, self._scale))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self.refresh()

    def _build(self) -> None:
        palette = self.palette
        scale = self._scale
        outer = tk.Frame(self, bg=palette["bg"])
        outer.pack(fill="both", expand=True)

        header = tk.Frame(
            outer, bg=palette["bg"], height=_px(54, scale),
            padx=_px(16, scale), pady=_px(8, scale),
        )
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header, text=theme.micro("История"), bg=palette["bg"],
            fg=palette["accent"], font=theme.font_text(10, "bold"),
        ).pack(side="left")
        self.count_label = tk.Label(
            header, text=theme.micro("0 записей"), bg=palette["bg"],
            fg=palette["text_muted"], font=theme.font_text(9),
        )
        self.count_label.pack(side="right")
        tk.Frame(outer, bg=palette["border"], height=1).pack(fill="x")

        body = tk.Frame(outer, bg=palette["bg"])
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0, minsize=_px(310, scale))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = tk.Frame(body, bg=palette["bg"], padx=_px(16, scale), pady=_px(10, scale))
        left.grid(row=0, column=0, sticky="nsew")
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        search_row = tk.Frame(left, bg=palette["bg"])
        search_row.grid(row=0, column=0, sticky="ew", pady=(0, _px(6, scale)))
        search_row.columnconfigure(1, weight=1)
        tk.Label(
            search_row, text=theme.micro("Поиск"), bg=palette["bg"],
            fg=palette["text_muted"], font=theme.font_text(9),
        ).grid(row=0, column=0, padx=(0, _px(8, scale)))
        self.search_entry = tk.Entry(
            search_row, bg=palette["surface"], fg=palette["text"],
            insertbackground=palette["accent"], relief="flat", bd=0,
            font=theme.font_text(11), highlightthickness=1, width=1,
            highlightbackground=palette["border"], highlightcolor=palette["accent"],
        )
        self.search_entry.grid(row=0, column=1, sticky="ew", ipady=_px(5, scale))
        self.search_entry.bind("<KeyRelease>", lambda _event: self.refresh())
        self.listbox = tk.Listbox(
            left, bg=palette["surface"], fg=palette["text"],
            selectbackground=palette["accent_soft"], selectforeground=palette["text"],
            relief="flat", bd=0, highlightthickness=1, width=1,
            highlightbackground=palette["border"], highlightcolor=palette["border"],
            font=theme.font_text(10), activestyle="none",
        )
        self.listbox.grid(row=1, column=0, sticky="nsew")
        self.listbox.bind("<<ListboxSelect>>", lambda _event: self._on_select())

        right = tk.Frame(body, bg=palette["bg"], padx=_px(16, scale), pady=_px(10, scale))
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        self.session_label = tk.Label(
            right, text=theme.micro("Выберите запись"), bg=palette["bg"],
            fg=palette["text_muted"], font=theme.font_text(9), anchor="w",
        )
        self.session_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, _px(4, scale)))
        self.text = tk.Text(
            right, bg=palette["surface"], fg=palette["text"],
            insertbackground=palette["accent"], selectbackground=palette["accent_soft"],
            selectforeground=palette["text"], relief="flat", bd=0, width=1,
            font=theme.font_text(12), undo=True, wrap="word",
            padx=_px(10, scale), pady=_px(8, scale),
            highlightthickness=1, highlightbackground=palette["border"],
            highlightcolor=palette["border"],
        )
        self.text.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.text.bind("<<Modified>>", self._on_modified)

        buttons = tk.Frame(right, bg=palette["bg"])
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(_px(8, scale), 0))
        self.save_button = BrandButton(
            buttons, "СОХРАНИТЬ", self.save_current, palette=palette, kind="cta",
            height=_px(36, scale), padding=_px(8, scale), font=theme.font_display(10, "bold"),
        )
        self.save_button.pack(side="left")
        self.copy_button = BrandButton(
            buttons, "КОПИРОВАТЬ", self.copy_current, palette=palette, kind="secondary",
            height=_px(36, scale), padding=_px(8, scale), font=theme.font_text(10),
        )
        self.copy_button.pack(side="left", padx=(_px(4, scale), 0))
        self.delete_button = BrandButton(
            buttons, "УДАЛИТЬ", self.delete_current, palette=palette, kind="ghost",
            height=_px(36, scale), padding=_px(8, scale), font=theme.font_text(10),
        )
        self.delete_button.pack(side="left", padx=(_px(8, scale), 0))
        self.clear_button = BrandButton(
            buttons, "ОЧИСТИТЬ ВСЁ", self.clear_all, palette=palette, kind="ghost",
            height=_px(36, scale), padding=_px(8, scale), font=theme.font_text(10),
        )
        self.clear_button.pack(side="left", padx=(_px(4, scale), 0))
        self.close_button = BrandButton(
            buttons, "ЗАКРЫТЬ", self.close, palette=palette, kind="ghost",
            height=_px(36, scale), padding=_px(8, scale), font=theme.font_text(10),
        )
        self.close_button.pack(side="left", padx=(_px(4, scale), 0))

        self.status_label = tk.Label(
            outer, text="", bg=palette["bg"], fg=palette["text_muted"],
            font=theme.font_text(9), anchor="w",
        )
        self.status_label.pack(fill="x", padx=_px(16, scale), pady=(0, _px(6, scale)))

    # -- data -------------------------------------------------------------

    def refresh(self) -> None:
        query = ""
        try:
            query = str(self.search_entry.get()).strip()
        except tk.TclError:
            pass
        try:
            sessions = list(self.store.list(query) or [])
        except Exception:  # noqa: BLE001
            sessions = []
        self._sessions = sessions
        self.listbox.delete(0, "end")
        for session in sessions:
            self.listbox.insert("end", self._format_row(session))
        self.count_label.configure(text=theme.micro(f"{len(sessions)} записей"))
        if sessions:
            self.listbox.selection_set(0)
            self._load_session(sessions[0])
        else:
            self._current_id = None
            self.session_label.configure(text=theme.micro("Записей нет"))
            self._set_text("")
            self._dirty = False

    @staticmethod
    def _format_row(session: dict[str, Any]) -> str:
        created = str(session.get("created_at") or "")
        stamp = created[8:10] + "." + created[5:7] + " " + created[11:16] if len(created) >= 16 else created
        duration = int(session.get("duration_ms") or 0) // 1000
        text = str(session.get("text") or "").replace("\n", " ")
        return f"{stamp}  {duration // 60}:{duration % 60:02d}  {text[:60]}"

    def _on_select(self) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        index = int(selection[0])
        if index >= len(self._sessions):
            return
        session = self._sessions[index]
        session_id = str(session.get("id") or "")
        if self._dirty and session_id != self._current_id:
            if not self._confirm_discard():
                self._reselect_current()
                return
        self._load_session(session)

    def _reselect_current(self) -> None:
        for index, session in enumerate(self._sessions):
            if str(session.get("id") or "") == self._current_id:
                self.listbox.selection_clear(0, "end")
                self.listbox.selection_set(index)
                return

    def _load_session(self, session: dict[str, Any]) -> None:
        self._current_id = str(session.get("id") or "")
        created = str(session.get("created_at") or "")
        duration = int(session.get("duration_ms") or 0) // 1000
        stamp = (
            created[8:10] + "." + created[5:7] + " " + created[11:16]
            if len(created) >= 16
            else created
        )
        self.session_label.configure(
            text=f"{theme.micro('Запись')}  {stamp}  ·  {duration // 60}:{duration % 60:02d}"
        )
        self._set_text(str(session.get("text") or ""))
        self._dirty = False
        try:
            self.save_button.set_enabled(False)
        except tk.TclError:
            pass
        self.set_status("")

    def _set_text(self, value: str) -> None:
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", value)
            self.text.edit_modified(False)
        except tk.TclError:
            pass

    def _on_modified(self, _event: tk.Event | None = None) -> None:
        try:
            modified = bool(self.text.edit_modified())
        except tk.TclError:
            return
        if not modified:
            return
        self._dirty = True
        self.save_button.set_enabled(True)

    def _confirm_discard(self) -> bool:
        from tkinter import messagebox

        try:
            return bool(
                messagebox.askyesno(
                    "История — Диктовка",
                    "Сохранить изменения текущей записи?",
                    parent=self,
                )
            )
        except tk.TclError:
            return False

    def current_text(self) -> str:
        try:
            return self.text.get("1.0", "end-1c")
        except tk.TclError:
            return ""

    def set_status(self, text: str, error: bool = False) -> None:
        try:
            self.status_label.configure(
                text=text, fg=self.palette["danger"] if error else self.palette["text_muted"]
            )
        except tk.TclError:
            pass

    # -- operations -------------------------------------------------------

    def save_current(self) -> bool:
        if self._current_id is None:
            return False
        text = self.current_text()
        try:
            ok = bool(self.store.update_text(self._current_id, text))
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"Не удалось сохранить: {exc}", error=True)
            return False
        if not ok:
            self.set_status("Не удалось сохранить запись", error=True)
            return False
        self._dirty = False
        try:
            self.save_button.set_enabled(False)
        except tk.TclError:
            pass
        self.set_status("Сохранено")
        self.refresh()
        return True

    def copy_current(self) -> bool:
        text = self.current_text()
        from dictate import winapi

        try:
            ok = bool(winapi.set_clipboard_text(text))
        except Exception:  # noqa: BLE001
            ok = False
        self.set_status("Скопировано в буфер" if ok else "Не удалось скопировать", error=not ok)
        return ok

    def delete_current(self) -> bool:
        if self._current_id is None:
            return False
        try:
            ok = bool(self.store.delete(self._current_id))
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"Не удалось удалить: {exc}", error=True)
            return False
        self._current_id = None
        self._dirty = False
        self.set_status("Запись удалена" if ok else "Не удалось удалить")
        self.refresh()
        return ok

    def clear_all(self) -> int:
        from tkinter import messagebox

        try:
            confirmed = bool(
                messagebox.askyesno(
                    "История — Диктовка",
                    "Удалить все записи истории? Это действие нельзя отменить.",
                    parent=self,
                )
            )
        except tk.TclError:
            confirmed = False
        if not confirmed:
            return 0
        try:
            removed = int(self.store.clear() or 0)
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"Не удалось очистить: {exc}", error=True)
            return 0
        self._current_id = None
        self._dirty = False
        self.set_status(f"Удалено записей: {removed}")
        self.refresh()
        return removed

    def close(self) -> None:
        if self._dirty:
            try:
                from tkinter import messagebox

                answer = messagebox.askyesnocancel(
                    "История — Диктовка",
                    "Сохранить изменения перед закрытием?",
                    parent=self,
                )
            except tk.TclError:
                answer = None
            if answer is True:
                self.save_current()
            elif answer is None:
                return
        callback = self.on_close
        try:
            self.destroy()
        except tk.TclError:
            pass
        if callback is not None:
            try:
                callback()
            except Exception:  # noqa: BLE001
                LOG.exception("Ошибка закрытия истории")

    def show(self) -> None:
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass


class BrandCheck(tk.Frame):
    """Чекбокс канона: квадрат 12×12, рамка 1 px, отмечен — заливка accent."""

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        variable: tk.BooleanVar,
        *,
        palette: dict[str, Any],
        bg: str,
    ) -> None:
        super().__init__(master, bg=bg, cursor="hand2")
        self.palette = palette
        self.variable = variable
        self.canvas = tk.Canvas(
            self, width=12, height=12, bg=bg, highlightthickness=0, bd=0,
            cursor="hand2",
        )
        self.canvas.pack(side="left", padx=(0, 8))
        self.label = tk.Label(
            self, text=text, bg=bg, fg=palette["text"],
            font=theme.font_text(11), anchor="w", cursor="hand2",
        )
        self.label.pack(side="left")
        self.canvas.bind("<Button-1>", self.toggle)
        self.label.bind("<Button-1>", self.toggle)
        self.variable.trace_add("write", lambda *_args: self._draw())
        self._draw()

    def toggle(self, _event: tk.Event | None = None) -> None:
        try:
            self.variable.set(not bool(self.variable.get()))
        except tk.TclError:
            pass

    def _draw(self) -> None:
        try:
            self.canvas.delete("all")
            self.canvas.create_rectangle(
                0, 0, 11, 11, outline=self.palette["border"], width=1
            )
            if self.variable.get():
                self.canvas.create_rectangle(
                    1, 1, 10, 10, fill=self.palette["accent"], outline=""
                )
                self.canvas.create_line(
                    3, 6, 5, 8, 9, 3, fill=self.palette["on_accent"], width=1
                )
        except tk.TclError:
            pass


class BrandSlider(tk.Canvas):
    """Слайдер: track, заливка accent, pill-ползунок accent_strong."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        from_: float,
        to: float,
        value: float,
        command: Callable[[Any], Any] | None = None,
        palette: dict[str, Any],
        step: float = 1,
        height: int = 20,
        track_height: int = 2,
        thumb_size: int = 10,
    ) -> None:
        self.from_ = float(from_)
        self.to = float(to)
        self.step = max(1e-9, float(step))
        self._value = self._snap(float(value))
        self._command = command
        self.palette = palette
        self.track_color = palette["border"]
        self.fill_color = palette["accent"]
        self.base_thumb_color = palette["accent"]
        self.thumb_color = self.base_thumb_color
        self.hover_thumb_color = palette["accent_strong"]
        self._height = int(height)
        self._track_height = int(track_height)
        self._thumb_size = int(thumb_size)
        self._hover = False
        self._focused = False
        self._dragging = False
        self._parent_bg = BrandButton._parent_background(master)
        super().__init__(
            master, height=self._height, bg=self._parent_bg,
            highlightthickness=0, bd=0, takefocus=1, cursor="hand2",
        )
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Left>", lambda event: self._step_by(-1, event))
        self.bind("<Right>", lambda event: self._step_by(1, event))
        self.bind("<Home>", lambda event: self._set_and_notify(self.from_, event))
        self.bind("<End>", lambda event: self._set_and_notify(self.to, event))
        self._draw()

    def get(self) -> float:
        return self._value

    def set(self, value: Any, notify: bool = False) -> None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        self._value = self._snap(number)
        self._draw()
        if notify:
            self._notify()

    def _width(self) -> int:
        try:
            width = int(self.winfo_width())
        except tk.TclError:
            width = 0
        if width <= 1:
            try:
                width = int(float(self.cget("width")))
            except (tk.TclError, TypeError, ValueError):
                width = 300
        return max(60, width)

    def _thumb_x(self) -> float:
        width = self._width()
        left = self._thumb_size / 2.0
        right = max(left + 1.0, width - self._thumb_size / 2.0)
        ratio = (self._value - self.from_) / max(1e-9, self.to - self.from_)
        return left + ratio * (right - left)

    def _value_from_x(self, x: float) -> float:
        width = self._width()
        left = self._thumb_size / 2.0
        right = max(left + 1.0, width - self._thumb_size / 2.0)
        ratio = (float(x) - left) / (right - left)
        return self.from_ + max(0.0, min(1.0, ratio)) * (self.to - self.from_)

    def _snap(self, value: float) -> float:
        clamped = max(self.from_, min(self.to, value))
        steps = round((clamped - self.from_) / self.step)
        snapped = self.from_ + steps * self.step
        snapped = max(self.from_, min(self.to, snapped))
        return int(round(snapped)) if float(snapped).is_integer() else round(snapped, 3)

    def _draw(self) -> None:
        self.delete("all")
        try:
            width = self._width()
            height = int(self.winfo_height())
        except tk.TclError:
            return
        if height <= 1:
            height = self._height
        middle = height / 2.0
        track_top = middle - self._track_height / 2.0
        track_bottom = middle + self._track_height / 2.0
        self.create_rectangle(
            0, track_top, width, track_bottom,
            fill=self.track_color, outline="",
        )
        thumb_x = self._thumb_x()
        if thumb_x > 1:
            self.create_rectangle(
                0, track_top, thumb_x, track_bottom,
                fill=self.fill_color, outline="",
            )
        self.thumb_color = (
            self.hover_thumb_color if (self._hover or self._focused)
            else self.base_thumb_color
        )
        half = self._thumb_size / 2.0
        if self._focused:
            self.create_rectangle(
                thumb_x - half - 3, middle - half - 3,
                thumb_x + half + 3, middle + half + 3,
                outline=self.palette["accent"], width=1,
            )
        self.create_rectangle(
            thumb_x - half, middle - half,
            thumb_x + half, middle + half,
            fill=self.thumb_color, outline="",
        )

    def _on_press(self, event: tk.Event) -> str:
        try:
            self.focus_set()
        except tk.TclError:
            pass
        self._dragging = True
        self._set_and_notify(self._value_from_x(event.x), event)
        return "break"

    def _on_drag(self, event: tk.Event) -> str:
        if self._dragging:
            self._set_and_notify(self._value_from_x(event.x), event)
        return "break"

    def _on_release(self, _event: tk.Event | None = None) -> str:
        self._dragging = False
        return "break"

    def _on_enter(self, _event: tk.Event | None = None) -> None:
        self._hover = True
        self._draw()

    def _on_leave(self, _event: tk.Event | None = None) -> None:
        self._hover = False
        self._draw()

    def _on_focus_in(self, _event: tk.Event | None = None) -> None:
        self._focused = True
        self._draw()

    def _on_focus_out(self, _event: tk.Event | None = None) -> None:
        self._focused = False
        self._draw()

    def _step_by(self, direction: int, _event: tk.Event | None = None) -> str:
        self._set_and_notify(self._value + direction * self.step, _event)
        return "break"

    def _set_and_notify(self, value: float, _event: tk.Event | None = None) -> str:
        self.set(value, notify=True)
        return "break"

    def _notify(self) -> None:
        if self._command is None:
            return
        try:
            self._command(self._value)
        except Exception:  # noqa: BLE001
            LOG.exception("Ошибка команды слайдера")


def _apply_dark_titlebar(window: tk.Toplevel, dark: bool = True) -> None:
    if sys.platform != "win32":
        return
    try:
        window.update_idletasks()
        user32 = ctypes.windll.user32
        hwnd = int(window.winfo_id())
        root = int(user32.GetAncestor(hwnd, 2) or hwnd)
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            root, 20, ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- dictation


class DictationWindow:
    """Плавающее окно диктовки с иконками, pin, размерами и правкой текста."""

    def __init__(
        self,
        master: tk.Misc,
        callbacks: dict[str, Callable[..., Any]] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.callbacks: dict[str, Callable[..., Any]] = dict(callbacks or {})
        self.settings: dict[str, Any] = dict(settings or {})
        self.ui_scale = clamp_ui_scale(self.settings.get("uiScale", 1.0))
        self.theme_name = normalize_theme(self.settings.get("theme", "scene"))
        self.accent = self.settings.get("accent") or theme.DEFAULT_ACCENT
        self.palette = theme.colors(self.theme_name, self.accent)
        self.size_mode = normalize_size_mode(self.settings.get("sizeMode", "normal"))
        self.pinned = bool(self.settings.get("pinned", False))
        self.state = "idle"
        self.visible = False
        self._elapsed_ms = 0
        self._drag_offset: tuple[int, int] | None = None
        self._dragging = False
        self._pulse_job: str | None = None
        self._pulse_index = 0
        self._notice_text = ""
        self._notice_is_error = False
        self._icons = icons_available()
        self._suggestions_window: SuggestionsWindow | None = None
        self._improve_busy = False
        self._x = 0
        self._y = 0

        self.width, self.height = self._mode_size()
        self.win = tk.Toplevel(master)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=self.palette["bg"])
        self.win.title(WINDOW_TITLE)

        self._build()
        self._apply_geometry()
        self.set_state("idle")

    # ------------------------------------------------------------------ sizes/geometry

    def _mode_size(self) -> tuple[int, int]:
        base_w, base_h = SIZE_SIZES[self.size_mode]
        return (_px(base_w, self.ui_scale), _px(base_h, self.ui_scale))

    def _screen_bounds(self) -> tuple[int, int]:
        try:
            return int(self.win.winfo_screenwidth() or 1920), int(
                self.win.winfo_screenheight() or 1080
            )
        except tk.TclError:
            return 1920, 1080

    def _apply_geometry(self) -> None:
        self.width, self.height = self._mode_size()
        screen_w, screen_h = self._screen_bounds()
        saved_x = self.settings.get("windowX")
        saved_y = self.settings.get("windowY")
        if isinstance(saved_x, int) and isinstance(saved_y, int):
            x, y = saved_x, saved_y
        else:
            x = max(0, (screen_w - self.width) // 2)
            y = max(0, screen_h - self.height - 80)
        x = max(8, min(int(x), max(8, screen_w - self.width - 8)))
        y = max(8, min(int(y), max(8, screen_h - self.height - 60)))
        self._x, self._y = x, y
        self.win.geometry(f"{self.width}x{self.height}+{x}+{y}")

    # ------------------------------------------------------------------ build

    def _build(self) -> None:
        palette = self.palette
        scale = self.ui_scale
        compact = self.size_mode == "compact"

        self.border_frame = tk.Frame(self.win, bg=palette["border"], padx=1, pady=1)
        self.border_frame.pack(fill="both", expand=True)
        self.inner = tk.Frame(self.border_frame, bg=palette["bg"])
        self.inner.pack(fill="both", expand=True)

        header_height = _px(44 if compact else TOP_BAR_HEIGHT, scale)
        self.top_bar = tk.Frame(
            self.inner, bg=palette["bg"], height=header_height,
            padx=_px(12 if compact else 16, scale),
            pady=_px(2 if compact else 3, scale),
        )
        self.top_bar.pack(fill="x")
        self.top_bar.pack_propagate(False)

        left = tk.Frame(self.top_bar, bg=palette["bg"])
        left.pack(side="left", fill="y")
        self._mic_image = theme.icon_photo(self.win, self.theme_name, _px(20 if compact else 22, scale))
        if self._mic_image is not None:
            self.mic_label = tk.Label(left, image=self._mic_image, bg=palette["bg"], bd=0)
        else:
            self.mic_label = tk.Label(
                left,
                text=icon_glyph("mic") or "●",
                bg=palette["bg"],
                fg=palette["accent"],
                font=theme.icon_font(_px(18, scale)),
                bd=0,
            )
        self.mic_label.pack(side="left", padx=(0, _px(8, scale)))

        brand = tk.Frame(left, bg=palette["bg"])
        brand.pack(side="left", fill="y")
        self.title_brand = tk.Label(
            brand,
            text=theme.APP_TITLE,
            bg=palette["bg"],
            fg=palette["text"],
            font=theme.font_display(15 if not compact else 14, "bold"),
            anchor="w",
        )
        self.title_brand.pack(anchor="w")
        self.kicker_label = tk.Label(
            brand,
            text=theme.micro(theme.APP_KICKER),
            bg=palette["bg"],
            fg=palette["text_muted"],
            font=theme.font_text(9),
            anchor="w",
        )
        if not compact:
            self.kicker_label.pack(anchor="w")

        right = tk.Frame(self.top_bar, bg=palette["bg"])
        right.pack(side="right", fill="y")

        self._level_width = _px(96 if not compact else 64, scale)
        self._level_height = _px(2, scale)
        self.level_canvas = tk.Canvas(
            right, width=self._level_width, height=self._level_height,
            bg=palette["bg"], highlightthickness=0, bd=0,
        )
        if not compact:
            self.level_canvas.pack(side="left", padx=(0, _px(10, scale)))
        self._level_track = self.level_canvas.create_rectangle(
            0, 0, self._level_width, self._level_height, fill=palette["border"], outline=""
        )
        self._level_rect = self.level_canvas.create_rectangle(
            0, 0, 0, self._level_height, fill=palette["accent"], outline=""
        )

        self.dot = tk.Canvas(
            right, width=10, height=10, bg=palette["bg"], highlightthickness=0, bd=0
        )
        self._dot_item = self.dot.create_oval(0, 0, 10, 10, fill=palette["text_muted"], outline="")

        self.title_label = tk.Label(
            right,
            text=STATUS_IDLE,
            bg=palette["bg"],
            fg=palette["text_muted"],
            font=theme.font_text(10 if compact else 10, "bold"),
        )
        self.title_label.pack(side="left", padx=(0, _px(8, scale)))

        self.timer_label = tk.Label(
            right,
            text="0:00",
            bg=palette["bg"],
            fg=palette["text_secondary"],
            font=theme.font_display(13 if not compact else 11, "normal"),
        )
        self.timer_label.pack(side="left", padx=(0, _px(10, scale)))

        icon_size = _px(14, scale)
        self.pin_button = BrandButton(
            right, palette=palette, kind="icon", icon="pin", icon_only=True,
            height=_px(30, scale), padding=_px(6, scale),
            font=theme.icon_font(icon_size), tooltip="Закрепить как виджет",
            command=lambda: self._toggle_pin(),
        )
        self.pin_button.pack(side="left")
        self.size_button = BrandButton(
            right, palette=palette, kind="icon", icon="expand", icon_only=True,
            height=_px(30, scale), padding=_px(6, scale),
            font=theme.icon_font(icon_size), tooltip="Следующий размер окна",
            command=lambda: self._cycle_size(),
        )
        self.size_button.pack(side="left")
        self.settings_button = BrandButton(
            right, palette=palette, kind="icon", icon="settings", icon_only=True,
            height=_px(30, scale), padding=_px(6, scale),
            font=theme.icon_font(icon_size), tooltip="Настройки",
            command=lambda: self._fire("settings"),
        )
        self.settings_button.pack(side="left")
        self.close_button = BrandButton(
            right, palette=palette, kind="icon", icon="close", icon_only=True,
            height=_px(30, scale), padding=_px(6, scale),
            font=theme.icon_font(icon_size), tooltip="Закрыть окно",
            command=lambda: self._fire("close"),
        )
        self.close_button.pack(side="left")

        for widget in (
            self.top_bar, left, brand, self.mic_label, self.title_brand,
        ):
            self._bind_drag(widget)
        if not compact:
            self._bind_drag(self.kicker_label)

        self.separator = tk.Frame(self.inner, bg=palette["border"], height=_px(1, scale))
        self.separator.pack(fill="x")

        # Акцентная полоса 2 px — сразу под шапкой/линией.
        self.accent_strip = tk.Frame(self.inner, bg=palette["bg"], height=_px(2, scale))
        self.accent_strip.pack(fill="x")

        # Нижняя панель — до текста, чтобы прижималась к низу.
        bottom_height = _px(46 if compact else BOTTOM_BAR_HEIGHT, scale)
        self.bottom_bar = tk.Frame(
            self.inner, bg=palette["bg"], height=bottom_height,
            padx=_px(12 if compact else 16, scale),
            pady=_px(4 if compact else 6, scale),
        )
        self.bottom_bar.pack(side="bottom", fill="x")
        self.bottom_bar.pack_propagate(False)
        self.bottom_bar.columnconfigure(0, weight=1)
        self.bottom_bar.rowconfigure(0, weight=1)

        self.notice_label = tk.Label(
            self.bottom_bar, text="", bg=palette["bg"],
            fg=palette["text_muted"], font=theme.font_text(10), anchor="w",
        )
        self.notice_label.grid(row=0, column=0, sticky="w")
        self.hint_label = tk.Label(
            self.bottom_bar,
            text=HINT_IDLE,
            bg=palette["bg"],
            fg=palette["text_muted"],
            font=theme.font_text(9),
            anchor="w",
            justify="left",
        )
        if not compact:
            self.hint_label.grid(row=1, column=0, columnspan=2, sticky="w")

        self.buttons = tk.Frame(self.bottom_bar, bg=palette["bg"])
        self.buttons.grid(row=0, column=1, sticky="e")

        icon_btn_font = theme.icon_font(_px(13, scale))
        self.new_button = BrandButton(
            self.buttons, "НОВАЯ", lambda: self._fire("new"), palette=palette,
            kind="ghost", icon="new", height=_px(40, scale),
            font=theme.font_text(11),
        )
        self.stop_button = BrandButton(
            self.buttons, "ОСТАНОВИТЬ", lambda: self._fire("stop"), palette=palette,
            kind="cta", icon="stop", height=_px(40, scale),
            font=theme.font_display(11, "bold"),
        )
        self.cancel_button = BrandButton(
            self.buttons, "ОТМЕНА", lambda: self._fire("cancel"), palette=palette,
            kind="ghost", icon="close", height=_px(40, scale),
            font=theme.font_text(11),
        )
        self.copy_button = BrandButton(
            self.buttons, "КОПИРОВАТЬ", lambda: self._on_enter(), palette=palette,
            kind="cta", icon="copy", height=_px(40, scale),
            font=theme.font_display(11, "bold"),
        )
        self.paste_button = BrandButton(
            self.buttons, "ВСТАВИТЬ", palette=palette, kind="secondary", icon="paste",
            height=_px(40, scale), font=theme.font_text(11),
            command=lambda: self._on_ctrl_enter(),
        )
        self.bottom_settings_button = BrandButton(
            self.buttons, "НАСТРОЙКИ →", palette=palette, kind="ghost",
            icon="settings", height=_px(40, scale), font=theme.font_text(11),
            command=lambda: self._fire("settings"),
        )
        self.bottom_close_button = BrandButton(
            self.buttons, "ЗАКРЫТЬ", palette=palette, kind="ghost",
            icon="close", height=_px(40, scale), font=theme.font_text(11),
            command=lambda: self._fire("close"),
        )
        self.column = 0
        for widget in (
            self.new_button, self.stop_button, self.cancel_button,
            self.copy_button, self.paste_button, self.bottom_settings_button,
            self.bottom_close_button,
        ):
            widget.grid(row=0, column=self.column, padx=(6, 0))
            self.column += 1

        # Компактные иконочные действия постобработки — в том же ряду.
        self.compact_action_buttons: list[BrandButton] = []
        for icon, tooltip, event in (
            ("edit", "Пунктуация и грамматика", "punctuation"),
            ("refresh", "Улучшить текст (DeepSeek)", "improve"),
            ("info", "История диктовок", "history"),
            ("warning", "Спорные слова", "contested"),
        ):
            button = BrandButton(
                self.buttons, palette=palette, kind="icon", icon=icon, icon_only=True,
                height=_px(40, scale), font=icon_btn_font,
                tooltip=tooltip, command=lambda name=event: self._fire(name),
            )
            button.grid(row=0, column=self.column, padx=(6, 0))
            button.grid_remove()
            self.column += 1
            self.compact_action_buttons.append(button)
        (
            self.compact_punctuation_button,
            self.compact_improve_button,
            self.compact_history_button,
            self.compact_contested_button,
        ) = self.compact_action_buttons

        # Линия над нижней панелью.
        self.bottom_separator = tk.Frame(
            self.inner, bg=palette["border"], height=_px(1, scale)
        )
        self.bottom_separator.pack(side="bottom", fill="x")

        # Строка действий постобработки: пунктуация / улучшение / история / спорные.
        self._suggested_count = 0
        self.actions_bar = tk.Frame(
            self.inner, bg=palette["bg"],
            padx=_px(12 if compact else 16, scale),
            pady=_px(2, scale),
        )
        self.actions_bar.pack(side="bottom", fill="x")
        action_font = theme.font_text(10)
        self.punctuation_button = BrandButton(
            self.actions_bar, "ПУНКТУАЦИЯ →", lambda: self._fire("punctuation"),
            palette=palette, kind="ghost", icon="edit", height=_px(26, scale),
            font=action_font, tooltip="Пунктуация и грамматика",
        )
        self.punctuation_button.pack(side="left")
        self.improve_button = BrandButton(
            self.actions_bar, "УЛУЧШИТЬ →", lambda: self._fire("improve"),
            palette=palette, kind="ghost", icon="refresh", height=_px(26, scale),
            font=action_font, tooltip="Улучшить текст (DeepSeek)",
        )
        self.improve_button.pack(side="left", padx=(_px(10, scale), 0))
        self.history_button = BrandButton(
            self.actions_bar, "ИСТОРИЯ →", lambda: self._fire("history"),
            palette=palette, kind="ghost", icon="info", height=_px(26, scale),
            font=action_font, tooltip="История диктовок",
        )
        self.history_button.pack(side="left", padx=(_px(10, scale), 0))
        self.contested_button = BrandButton(
            self.actions_bar, "СПОРНЫЕ СЛОВА →", lambda: self._fire("contested"),
            palette=palette, kind="ghost", icon="warning", height=_px(26, scale),
            font=action_font, tooltip="Проверить спорные слова",
        )
        self.contested_button.pack(side="left", padx=(_px(10, scale), 0))
        self.contested_button.pack_forget()

        # Карточка текста.
        self.text_container = tk.Frame(self.inner, bg=palette["bg"])
        self.text_container.pack(
            fill="both", expand=True,
            padx=_px(10 if compact else 16, scale),
            pady=_px(5 if compact else 8, scale),
        )
        self.text_labels = tk.Frame(self.text_container, bg=palette["bg"])
        if not compact:
            self.text_labels.pack(fill="x", pady=(0, _px(4, scale)))
        self.text_label = tk.Label(
            self.text_labels,
            text=theme.micro("Текст"),
            bg=palette["bg"],
            fg=palette["text_muted"],
            font=theme.font_text(9),
            anchor="w",
        )
        self.text_label.pack(side="left")
        self.edit_label = tk.Label(
            self.text_labels,
            text=theme.micro("Правка разрешена"),
            bg=palette["bg"],
            fg=palette["text_muted"],
            font=theme.font_text(9),
            anchor="e",
        )
        self.edit_label.pack(side="right")
        self.text_card = tk.Frame(
            self.text_container, bg=palette["surface"],
            highlightthickness=_px(1, scale),
            highlightbackground=palette["border"],
            highlightcolor=palette["border"], bd=0,
        )
        self.text_card.pack(fill="both", expand=True)
        self.text = tk.Text(
            self.text_card,
            height=2 if compact else 6,
            wrap="word",
            bg=palette["surface"],
            fg=palette["text"],
            insertbackground=palette["accent"],
            selectbackground=palette["accent_soft"],
            selectforeground=palette["text"],
            relief="flat",
            bd=0,
            padx=_px(10 if compact else 16, scale),
            pady=_px(6 if compact else 10, scale),
            spacing1=2 if compact else 4,
            spacing3=2 if compact else 4,
            font=theme.font_text(13 if not compact else 11),
            undo=True,
            highlightthickness=0,
        )
        self.text.pack(fill="both", expand=True)
        for widget in (self.text_container, self.text_card):
            self._bind_drag(widget)

        # Остальной фон тоже перетаскиваемый.
        for widget in (self.border_frame, self.inner):
            self._bind_drag(widget)

        for widget in (self.win, self.text):
            widget.bind("<Return>", self._on_enter)
            widget.bind("<KP_Enter>", self._on_enter)
            widget.bind("<Control-Return>", self._on_ctrl_enter)
            widget.bind("<Escape>", self._on_escape)

        self._apply_size_layout()

    def _apply_size_layout(self) -> None:
        compact = self.size_mode == "compact"
        # В compact кнопки только с иконками (подписи уходят в tooltip).
        try:
            for button, text in (
                (self.new_button, "НОВАЯ"),
                (self.copy_button, "КОПИРОВАТЬ"),
                (self.stop_button, "ОСТАНОВИТЬ"),
                (self.cancel_button, "ОТМЕНА"),
                (self.paste_button, "ВСТАВИТЬ"),
                (self.bottom_settings_button, "НАСТРОЙКИ →"),
                (self.bottom_close_button, "ЗАКРЫТЬ"),
            ):
                button._icon_only = compact
                button.set_text(text)
            self.text.configure(height=2 if compact else 6)
        except tk.TclError:
            pass
        self._update_actions_visibility()

    def _update_actions_visibility(self) -> None:
        """Показывает строку постобработки в idle; в compact — иконки."""
        compact = self.size_mode == "compact"
        idle = self.state == "idle"
        try:
            if compact:
                if self.actions_bar.winfo_manager():
                    self.actions_bar.pack_forget()
                for index, button in enumerate(self.compact_action_buttons):
                    if not idle:
                        if button.winfo_manager():
                            button.grid_remove()
                        continue
                    if index == 3 and not self._suggested_count:
                        if button.winfo_manager():
                            button.grid_remove()
                        continue
                    if not button.winfo_manager():
                        button.grid()
            else:
                for button in self.compact_action_buttons:
                    if button.winfo_manager():
                        button.grid_remove()
                if idle:
                    if not self.actions_bar.winfo_manager():
                        self.actions_bar.pack(
                            side="bottom", fill="x", before=self.bottom_separator
                        )
                elif self.actions_bar.winfo_manager():
                    self.actions_bar.pack_forget()
            # «Спорные слова» — только когда есть suggestions.
            visible = idle and bool(self._suggested_count)
            if compact:
                if not visible and self.compact_contested_button.winfo_manager():
                    self.compact_contested_button.grid_remove()
            elif visible:
                if not self.contested_button.winfo_manager():
                    self.contested_button.pack(
                        side="left", padx=(_px(10, self.ui_scale), 0)
                    )
            elif self.contested_button.winfo_manager():
                self.contested_button.pack_forget()
        except tk.TclError:
            pass

    def set_contested(self, count: int) -> None:
        """Сколько «спорных» слов нашла постобработка (0 — скрыть кнопку)."""
        self._suggested_count = max(0, int(count))
        label = (
            f"СПОРНЫЕ СЛОВА: {self._suggested_count} →"
            if self._suggested_count
            else "СПОРНЫЕ СЛОВА →"
        )
        try:
            self.contested_button.set_text(label)
        except tk.TclError:
            pass
        self._update_actions_visibility()

    def set_improve_busy(self, busy: bool) -> None:
        """Блокирует «Улучшить», пока идёт запрос к DeepSeek."""
        self._improve_busy = bool(busy)
        label = "УЛУЧШАЮ…" if busy else "УЛУЧШИТЬ →"
        for button in (
            self.improve_button, self.compact_improve_button,
        ):
            try:
                button.set_enabled(not busy)
                button.set_text(label)
            except tk.TclError:
                pass

    def show_suggestions(self, suggestions: list[Any]) -> SuggestionsWindow | None:
        """Открывает модальное окно «спорных слов»; результат — callback suggestions."""
        if not suggestions:
            return None
        try:
            if self._suggestions_window is not None:
                self._suggestions_window.destroy()
        except tk.TclError:
            pass

        def _applied(choices: dict[int, str | None]) -> None:
            self._suggestions_window = None
            self._fire("suggestions", choices)

        try:
            self._suggestions_window = SuggestionsWindow(
                self.win,
                suggestions,
                self.palette,
                on_apply=_applied,
                ui_scale=self.ui_scale,
            )
        except Exception:  # noqa: BLE001
            LOG.exception("Не удалось открыть окно спорных слов")
            self._suggestions_window = None
        return self._suggestions_window

    def set_text_undo(self, text: str) -> None:
        """Заменяет текст, сохраняя undo-историю (Ctrl+Z отменяет правку)."""
        try:
            self.text.configure(state="normal")
            self.text.edit_separator()
            self.text.delete("1.0", "end")
            self.text.insert("1.0", text or "")
            self.text.edit_separator()
            self.text.see("end")
            if self.state in ("recording", "finalizing"):
                self.text.configure(state="disabled")
            self._refresh_buttons()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ drag

    def _bind_drag(self, widget: tk.Widget) -> None:
        widget.bind("<ButtonPress-1>", self._drag_start, add="+")
        widget.bind("<B1-Motion>", self._drag_move, add="+")
        widget.bind("<ButtonRelease-1>", self._drag_end, add="+")

    def _drag_start(self, event: tk.Event) -> None:
        self._drag_offset = (
            int(event.x_root) - self.win.winfo_x(),
            int(event.y_root) - self.win.winfo_y(),
        )
        self._dragging = True

    def _drag_move(self, event: tk.Event) -> None:
        if not self._drag_offset:
            return
        dx, dy = self._drag_offset
        self.win.geometry(f"+{int(event.x_root) - dx}+{int(event.y_root) - dy}")

    def _drag_end(self, _event: tk.Event | None = None) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self._drag_offset = None
        callback = self.callbacks.get("move")
        if callback is not None:
            try:
                callback(int(self.win.winfo_x()), int(self.win.winfo_y()))
            except Exception:  # noqa: BLE001
                LOG.exception("Ошибка обработчика перемещения окна")

    def current_position(self) -> tuple[int, int]:
        try:
            if self.win.winfo_ismapped():
                return int(self.win.winfo_x()), int(self.win.winfo_y())
        except tk.TclError:
            pass
        return int(self._x), int(self._y)

    # ------------------------------------------------------------------ pin/size

    def _toggle_pin(self) -> None:
        self.set_pinned(not self.pinned)
        callback = self.callbacks.get("pin")
        if callback is not None:
            try:
                callback(self.pinned)
            except Exception:  # noqa: BLE001
                LOG.exception("Ошибка обработчика закрепления")

    def set_pinned(self, pinned: bool) -> None:
        self.pinned = bool(pinned)
        self.settings["pinned"] = self.pinned
        try:
            self.win.attributes("-topmost", True)
            self.pin_button.set_text("")
            self.pin_button._icon_name = "unpin" if self.pinned else "pin"
            # Перерисовать глиф.
            self.pin_button._glyph = icon_glyph(self.pin_button._icon_name or "pin")
            self.pin_button._label = self.pin_button._compose_label()
            self.pin_button._draw()
            self.pin_button.tooltip.text = (
                "Открепить от экрана" if self.pinned else "Закрепить как виджет"
            )
        except (tk.TclError, AttributeError):
            pass

    def _cycle_size(self) -> None:
        index = SIZE_MODES.index(self.size_mode)
        self.set_size_mode(SIZE_MODES[(index + 1) % len(SIZE_MODES)])

    def set_size_mode(self, mode: str) -> None:
        self.size_mode = normalize_size_mode(mode)
        self.settings["sizeMode"] = self.size_mode
        self._apply_geometry()
        self._apply_size_layout()
        callback = self.callbacks.get("size")
        if callback is not None:
            try:
                callback(self.size_mode)
            except Exception:  # noqa: BLE001
                LOG.exception("Ошибка обработчика размера")

    # ------------------------------------------------------------------ appearance

    def apply_appearance(self, settings: dict[str, Any]) -> None:
        """Применяет тему/акцент/размер/pinned без перезапуска (с сохранением текста)."""
        text = self.get_text()
        state = self.state
        notice = self._notice_text
        notice_error = self._notice_is_error
        elapsed = self._elapsed_ms
        visible = self.visible
        self.settings = dict(settings)
        self.ui_scale = clamp_ui_scale(settings.get("uiScale", 1.0))
        self.theme_name = normalize_theme(settings.get("theme", "scene"))
        self.accent = settings.get("accent") or theme.DEFAULT_ACCENT
        self.palette = theme.colors(self.theme_name, self.accent)
        self.size_mode = normalize_size_mode(settings.get("sizeMode", "normal"))
        self.pinned = bool(settings.get("pinned", False))
        self.win.configure(bg=self.palette["bg"])
        self._stop_pulse()
        for child in self.win.winfo_children():
            child.destroy()
        self._build()
        self._apply_geometry()
        self.set_state(state)
        self.set_text(text)
        self.set_elapsed(elapsed)
        self.set_notice(notice, error=notice_error)
        self.set_pinned(self.pinned)
        if visible:
            self.win.deiconify()
        self._refresh_buttons()

    def set_settings(self, settings: dict[str, Any]) -> None:
        appearance_keys = ("theme", "accent", "sizeMode", "pinned", "uiScale")
        if any(settings.get(key) != self.settings.get(key) for key in appearance_keys):
            self.apply_appearance(settings)
            return
        self.settings = dict(settings)
        self.window_x = settings.get("windowX")
        self.window_y = settings.get("windowY")
        self._apply_geometry()
        self.set_state(self.state)
        self._refresh_buttons()

    # ------------------------------------------------------------------ state

    def show(self, focus: bool = True) -> None:
        self._apply_geometry()
        self.win.deiconify()
        self.win.lift()
        try:
            self.win.attributes("-topmost", True)
            if focus:
                self.win.focus_force()
        except tk.TclError:
            pass
        self.visible = True
        if focus:
            self.focus_editor()
        if self.state == "recording":
            self._start_pulse()
        try:
            if focus:
                self.win.after(40, self._force_text_focus)
        except tk.TclError:
            pass

    def _force_text_focus(self) -> None:
        try:
            self.win.focus_force()
            self.focus_editor()
        except tk.TclError:
            pass

    def hide(self) -> None:
        self._stop_pulse()
        try:
            self.win.withdraw()
        except tk.TclError:
            pass
        self.visible = False

    def set_state(self, state: str) -> None:
        self.state = state
        palette = self.palette
        compact = self.size_mode == "compact"
        if state == "recording":
            self.text.configure(state="disabled")
            self._set_visible(self.new_button, True)
            self._set_visible(self.stop_button, True)
            self._set_visible(self.cancel_button, True)
            self._set_visible(self.copy_button, False)
            self._set_visible(self.paste_button, False)
            self._set_visible(self.bottom_settings_button, False)
            self._set_visible(self.bottom_close_button, False)
            self.accent_strip.configure(bg=palette["accent"])
            self.title_label.configure(text=STATUS_RECORDING, fg=palette["accent"])
            self.hint_label.configure(text=HINT_RECORDING)
            self.stop_button.set_text("ОСТАНОВИТЬ")
            self.stop_button.set_enabled(True)
            self._set_edit_label_visible(False)
            self._start_pulse()
        elif state == "finalizing":
            self.text.configure(state="disabled")
            self._set_visible(self.new_button, True)
            self._set_visible(self.stop_button, True)
            self._set_visible(self.cancel_button, True)
            self._set_visible(self.copy_button, False)
            self._set_visible(self.paste_button, False)
            self._set_visible(self.bottom_settings_button, False)
            self._set_visible(self.bottom_close_button, False)
            self.stop_button.set_text(theme.micro("Распознаю…"))
            self.stop_button.set_enabled(False)
            self.accent_strip.configure(bg=palette["accent"])
            self.title_label.configure(
                text=STATUS_FINALIZING, fg=palette["accent_strong"]
            )
            self.hint_label.configure(text=HINT_RECORDING)
            self._set_edit_label_visible(False)
            self._stop_pulse()
            self._paint_dot(palette["accent_strong"])
        else:
            self.text.configure(state="normal")
            self._set_visible(self.new_button, True)
            self._set_visible(self.stop_button, False)
            self._set_visible(self.cancel_button, False)
            self._set_visible(self.copy_button, True)
            self._set_visible(
                self.paste_button, bool(self.settings.get("pasteOnCtrlEnter", True))
            )
            self._set_visible(self.bottom_settings_button, True)
            self._set_visible(self.bottom_close_button, True)
            self.stop_button.set_text("ОСТАНОВИТЬ")
            self.stop_button.set_enabled(True)
            self.accent_strip.configure(bg=palette["bg"])
            self.title_label.configure(text=STATUS_IDLE, fg=palette["text_muted"])
            self.hint_label.configure(text=HINT_IDLE)
            self._set_edit_label_visible(not compact)
            self._stop_pulse()
            has_text = bool(self.get_text().strip())
            self._paint_dot(palette["ok"] if has_text else palette["text_muted"])
        self._refresh_timer()
        self._refresh_buttons()
        self._apply_size_layout()

    def _set_edit_label_visible(self, visible: bool) -> None:
        try:
            if visible:
                if not self.edit_label.winfo_manager():
                    self.edit_label.pack(side="right")
            else:
                if self.edit_label.winfo_manager():
                    self.edit_label.pack_forget()
        except tk.TclError:
            pass

    def set_text(self, text: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text or "")
        if self.state in ("recording", "finalizing"):
            self.text.configure(state="disabled")
        self.text.see("end")
        if self.state == "idle":
            self._paint_dot(
                self.palette["ok"] if self.get_text().strip() else self.palette["text_muted"]
            )
        self._refresh_buttons()

    def set_level(self, rms: float) -> None:
        try:
            value = float(rms)
        except (TypeError, ValueError):
            value = 0.0
        level = max(0.0, min(1.0, value * 5.0))
        filled = int(round((self._level_width - 2) * level)) + 1
        filled = max(1, min(self._level_width, filled))
        try:
            self.level_canvas.coords(self._level_rect, 0, 0, filled, self._level_height)
        except tk.TclError:
            pass

    def set_elapsed(self, elapsed_ms: int) -> None:
        self._elapsed_ms = int(elapsed_ms)
        self._refresh_timer()

    def set_notice(self, text: str, error: bool = False) -> None:
        self._notice_text = str(text or "")
        self._notice_is_error = bool(error)
        try:
            self.notice_label.configure(
                text=self._notice_text,
                fg=self.palette["danger"] if error else self.palette["text_muted"],
            )
        except tk.TclError:
            pass

    def focus_editor(self) -> None:
        try:
            self.text.focus_set()
            self.text.mark_set("insert", "end-1c")
            self.text.see("end")
        except tk.TclError:
            pass

    def get_text(self) -> str:
        try:
            return self.text.get("1.0", "end-1c")
        except tk.TclError:
            return ""

    def hwnd(self) -> int:
        if sys.platform != "win32":
            return 0
        try:
            self.win.update_idletasks()
            hwnd = int(self.win.winfo_id())
            return int(ctypes.windll.user32.GetAncestor(hwnd, 2) or hwnd)
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------------ internals

    def _refresh_timer(self) -> None:
        show = bool(self.settings.get("showElapsed", True))
        active = self.state in ("recording", "finalizing")
        if show and active:
            self.timer_label.configure(text=format_elapsed(self._elapsed_ms))
            if not self.timer_label.winfo_manager():
                try:
                    self.timer_label.pack(
                        side="left", before=self.pin_button,
                        padx=(0, _px(10, self.ui_scale)),
                    )
                except tk.TclError:
                    self.timer_label.pack(
                        side="left", padx=(0, _px(10, self.ui_scale))
                    )
        else:
            if self.timer_label.winfo_manager():
                self.timer_label.pack_forget()

    def _refresh_buttons(self) -> None:
        if self.state != "idle":
            return
        has_text = bool(self.get_text().strip())
        self.copy_button.set_enabled(has_text)

    def _set_visible(self, widget: tk.Widget, visible: bool) -> None:
        if visible:
            if not widget.winfo_manager():
                widget.grid()
        else:
            if widget.winfo_manager():
                widget.grid_remove()

    def _paint_dot(self, color: str) -> None:
        try:
            self.dot.itemconfigure(self._dot_item, fill=color)
        except tk.TclError:
            pass

    def _start_pulse(self) -> None:
        self._stop_pulse()
        self._pulse_index = 0
        self._pulse_tick()

    def _stop_pulse(self) -> None:
        job = self._pulse_job
        self._pulse_job = None
        if job is not None:
            try:
                self.win.after_cancel(job)
            except tk.TclError:
                pass

    def _pulse_tick(self) -> None:
        self._pulse_job = None
        if self.state != "recording" or not self.visible:
            return
        step = self._pulse_index % 8
        ratio = step / 7.0
        if self._pulse_index % 16 >= 8:
            ratio = 1.0 - ratio
        color = _blend(self.palette["accent"], self.palette["accent_soft"], ratio)
        self._paint_dot(color)
        self._pulse_index += 1
        try:
            self._pulse_job = self.win.after(80, self._pulse_tick)
        except tk.TclError:
            self._pulse_job = None

    def _fire(self, name: str, *args: Any) -> None:
        callback = self.callbacks.get(name)
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # noqa: BLE001
            LOG.exception("Ошибка обработчика окна: %s", name)

    def _on_enter(self, _event: tk.Event | None = None) -> str:
        if self.state == "recording":
            self._fire("stop")
        elif self.state == "idle":
            self._fire("copy")
        return "break"

    def _on_ctrl_enter(self, _event: tk.Event | None = None) -> str:
        if self.state == "idle":
            self._fire("paste")
        return "break"

    def _on_escape(self, _event: tk.Event | None = None) -> str:
        self._fire("escape")
        return "break"


# --------------------------------------------------------------------------- data helpers


def list_input_devices() -> list[tuple[int, str]]:
    try:
        import sounddevice as sd
    except Exception:  # noqa: BLE001
        return []
    try:
        devices: list[tuple[int, str]] = []
        for index, info in enumerate(sd.query_devices()):
            try:
                channels = int(info.get("max_input_channels", 0))
            except (TypeError, ValueError, AttributeError):
                channels = 0
            if channels <= 0:
                continue
            devices.append((index, str(info.get("name", f"Устройство {index}"))))
        return devices
    except Exception:  # noqa: BLE001
        return []


_LANGUAGE_LABELS = {"ru": "Русский (ru)", "en": "English (en)", "auto": "Авто (auto)"}
_LANGUAGE_VALUES = {label: value for value, label in _LANGUAGE_LABELS.items()}

_KEYSYM_NAMES = {
    "space": "Space", "Return": "Enter", "KP_Enter": "Enter", "Escape": "Esc",
    "Tab": "Tab", "BackSpace": "Backspace", "Insert": "Insert", "Delete": "Delete",
    "Home": "Home", "End": "End", "Prior": "PageUp", "Next": "PageDown",
    "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right",
    "comma": ",", "period": ".", "slash": "/", "semicolon": ";",
    "apostrophe": "'", "bracketleft": "[", "bracketright": "]", "minus": "-",
    "equal": "=", "backslash": "\\", "grave": "`",
}


def spec_from_tk_event(event: tk.Event) -> str | None:
    keysym = str(getattr(event, "keysym", "") or "")
    if not keysym:
        return None
    if keysym in _KEYSYM_NAMES:
        key = _KEYSYM_NAMES[keysym]
    elif keysym in (
        "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
        "Win_L", "Win_R", "Caps_Lock", "Num_Lock",
    ):
        return None
    elif len(keysym) == 1 and keysym.isprintable():
        key = keysym.upper()
    elif keysym.startswith("F") and keysym[1:].isdigit():
        key = keysym.upper()
    else:
        return None
    state = int(getattr(event, "state", 0) or 0)
    modifiers: list[str] = []
    if state & 0x0004:
        modifiers.append("Ctrl")
    if state & 0x0008 or state & 0x20000:
        modifiers.append("Alt")
    if state & 0x0001:
        modifiers.append("Shift")
    if not modifiers and len(key) == 1:
        return None
    return "+".join(modifiers + [key])


# --------------------------------------------------------------------------- settings


class SettingsWindow:
    """Детальные настройки в скролле: секции, акцент, модель, ресайз."""

    def __init__(
        self,
        master: tk.Misc,
        settings: dict[str, Any],
        on_save: Callable[[dict[str, Any]], Any] | None = None,
        on_test_mic: Callable[[dict[str, Any]], Any] | None = None,
        on_probe_hotkey: Callable[[str], bool] | None = None,
        on_reload_model: Callable[[dict[str, Any]], Any] | None = None,
        on_model_status: Callable[[], tuple[str, bool]] | None = None,
        on_settings_size: Callable[[int, int], Any] | None = None,
        on_open_logs: Callable[[], Any] | None = None,
        on_history: Callable[[], Any] | None = None,
        on_check_key: Callable[[str, str], tuple[bool, str]] | None = None,
    ) -> None:
        self.settings = dict(settings)
        self.on_save = on_save
        self.on_test_mic = on_test_mic
        self.on_probe_hotkey = on_probe_hotkey
        self.on_reload_model = on_reload_model
        self.on_model_status = on_model_status
        self.on_settings_size = on_settings_size
        self.on_open_logs = on_open_logs
        self.on_history = on_history
        self.on_check_key = on_check_key

        self.visible = False
        self.ui_scale = clamp_ui_scale(self.settings.get("uiScale", 1.0))
        self.theme_name = normalize_theme(self.settings.get("theme", "scene"))
        self.accent = self.settings.get("accent") or theme.DEFAULT_ACCENT
        self.palette = theme.colors(self.theme_name, self.accent)
        self._devices = list_input_devices()
        self._capture_target: str | None = None
        self._capture_button: BrandButton | None = None
        self._hide_job: str | None = None
        self._status_text = ""
        self._model_job: str | None = None
        self._size_job: str | None = None
        self._last_size = (0, 0)
        self._accent = theme.normalize_hex(self.accent) or theme.DEFAULT_ACCENT
        self._current_hotkey = str(self.settings.get("hotkey", "Ctrl+Alt+Space"))
        self._current_settings_hotkey = str(self.settings.get("settingsHotkey", "Ctrl+Alt+S"))

        self.win = tk.Toplevel(master)
        self.win.withdraw()
        self.win.title(SETTINGS_TITLE)
        self.win.configure(bg=self.palette["bg"])
        self.win.resizable(True, True)
        self.win.minsize(_px(560, self.ui_scale), _px(520, self.ui_scale))
        self.win.protocol("WM_DELETE_WINDOW", self.hide)
        theme.apply_window_icon(self.win)
        self._center()
        self._configure_ttk()
        self._build()
        self.win.bind("<Escape>", lambda _event: self._cancel_capture())
        self.win.bind("<Configure>", self._on_configure, add="+")
        self.set_model_status(*self._current_model_status())
        self._schedule_model_status()

    # ------------------------------------------------------------------ style / geometry

    def _configure_ttk(self) -> None:
        theme.init_ttk(self.win, self.theme_name, self._accent)
        palette = self.palette
        try:
            style = ttk.Style(self.win)
            style.configure(
                "Sa.TCombobox",
                fieldbackground=palette["surface"],
                background=palette["surface"],
                foreground=palette["text"],
                arrowcolor=palette["text_secondary"],
                bordercolor=palette["border"],
                lightcolor=palette["surface"],
                darkcolor=palette["surface"],
                selectbackground=palette["surface"],
                selectforeground=palette["text"],
                padding=(8, 4),
            )
            style.map(
                "Sa.TCombobox",
                fieldbackground=[("readonly", palette["surface"]), ("disabled", palette["bg_alt"])],
                foreground=[("disabled", palette["text_muted"])],
                bordercolor=[("focus", palette["accent"])],
                arrowcolor=[("active", palette["accent"])],
            )
            style.configure(
                "SaCard.TCheckbutton",
                background=palette["bg"],
                foreground=palette["text"],
                indicatorbackground=palette["surface"],
                indicatorforeground=palette["on_accent"],
                focuscolor=palette["accent"],
                indicatorsize=12,
            )
            style.map(
                "SaCard.TCheckbutton",
                indicatorbackground=[("selected", palette["accent"]), ("active", palette["bg_alt"])],
                indicatorforeground=[("selected", palette["on_accent"])],
                foreground=[("active", palette["text"])],
            )
        except tk.TclError:
            pass

    def _center(self) -> None:
        width = self._initial_width()
        height = self._initial_height()
        try:
            screen_w = int(self.win.winfo_screenwidth() or 1920)
            screen_h = int(self.win.winfo_screenheight() or 1080)
        except tk.TclError:
            screen_w, screen_h = 1920, 1080
        width = min(width, screen_w - 40)
        height = min(height, screen_h - 80)
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        self.win.geometry(f"{width}x{height}+{x}+{y}")
        self._last_size = (width, height)

    def _initial_width(self) -> int:
        saved = self.settings.get("settingsW")
        if isinstance(saved, int):
            return max(_px(560, self.ui_scale), min(6000, saved))
        return _px(720, self.ui_scale)

    def _initial_height(self) -> int:
        saved = self.settings.get("settingsH")
        if isinstance(saved, int):
            return max(_px(520, self.ui_scale), min(6000, saved))
        return _px(860, self.ui_scale)

    def _on_configure(self, event: tk.Event) -> None:
        if event.widget is not self.win:
            return
        width = int(self.win.winfo_width())
        height = int(self.win.winfo_height())
        if (width, height) == self._last_size or width < 400 or height < 300:
            return
        self._last_size = (width, height)
        if self._size_job is not None:
            try:
                self.win.after_cancel(self._size_job)
            except tk.TclError:
                pass
        try:
            self._size_job = self.win.after(400, self._emit_size)
        except tk.TclError:
            self._size_job = None

    def _emit_size(self) -> None:
        self._size_job = None
        if self.on_settings_size is None:
            return
        try:
            self.on_settings_size(*self._last_size)
        except Exception:  # noqa: BLE001
            LOG.exception("Не удалось сохранить размер окна настроек")

    # ------------------------------------------------------------------ build

    def _build(self) -> None:
        palette = self.palette
        scale = self.ui_scale
        outer = tk.Frame(self.win, bg=palette["bg"])
        outer.pack(fill="both", expand=True)

        header = tk.Frame(
            outer, bg=palette["bg"], height=_px(54, scale),
            padx=_px(16, scale), pady=_px(8, scale),
        )
        header.pack(fill="x")
        header.pack_propagate(False)
        icon_label = tk.Label(
            header,
            text=icon_glyph("settings") or "≡",
            bg=palette["bg"],
            fg=palette["accent"],
            font=theme.icon_font(_px(18, scale)) if icons_available() else theme.font_display(16),
        )
        icon_label.pack(side="left", padx=(0, _px(8, scale)))
        self.header_label = tk.Label(
            header,
            text="Настройки",
            bg=palette["bg"],
            fg=palette["text"],
            font=theme.font_display(16, "bold"),
        )
        self.header_label.pack(side="left")
        self.header_kicker = tk.Label(
            header,
            text=theme.micro("Диктовка / настройки"),
            bg=palette["bg"],
            fg=palette["text_muted"],
            font=theme.font_text(9),
        )
        self.header_kicker.pack(side="right")

        separator = tk.Frame(outer, bg=palette["border"], height=_px(1, scale))
        separator.pack(fill="x")

        # Скролл-контейнер.
        scroll_holder = tk.Frame(outer, bg=palette["bg"])
        scroll_holder.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            scroll_holder, bg=palette["bg"], highlightthickness=0, bd=0
        )
        self.scrollbar = ttk.Scrollbar(
            scroll_holder, orient="vertical", command=self.canvas.yview
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=palette["bg"])
        self._body_window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.columnconfigure(0, weight=1)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

        self._build_recognition_section(0)
        self._build_microphone_section(1)
        self._build_appearance_section(2)
        self._build_control_section(3)
        self._build_behavior_section(4)
        self._build_about_section(5)
        self._build_postprocess_section(6)
        self._build_llm_section(7)

        bottom_line = tk.Frame(outer, bg=palette["border"], height=_px(1, scale))
        bottom_line.pack(fill="x")
        bottom = tk.Frame(outer, bg=palette["bg"], padx=_px(16, scale), pady=_px(8, scale))
        bottom.pack(fill="x")
        bottom.columnconfigure(0, weight=1)
        self.status_label = tk.Label(
            bottom,
            text="",
            bg=palette["bg"],
            fg=palette["text_muted"],
            font=theme.font_text(10),
            anchor="w",
            justify="left",
            wraplength=_px(700, scale),
        )
        self.status_label.grid(row=0, column=0, columnspan=2, sticky="w")
        self.version_label = tk.Label(
            bottom,
            text=theme.micro(f"WinDictate {theme.APP_VERSION}"),
            bg=palette["bg"],
            fg=palette["text_muted"],
            font=theme.font_text(9),
            anchor="w",
        )
        self.version_label.grid(row=1, column=0, sticky="w", pady=(_px(4, scale), 0))
        buttons = tk.Frame(bottom, bg=palette["bg"])
        buttons.grid(row=1, column=1, sticky="e", pady=(_px(4, scale), 0))
        self.save_button = BrandButton(
            buttons, "СОХРАНИТЬ", self._save, palette=palette, kind="cta",
            icon="save", height=_px(40, scale), font=theme.font_display(11, "bold"),
        )
        self.save_button.pack(side="right", padx=(8, 0))
        self.cancel_button = BrandButton(
            buttons, "ОТМЕНА", self.hide, palette=palette, kind="secondary",
            height=_px(40, scale), font=theme.font_text(11),
        )
        self.cancel_button.pack(side="right")
        self.reset_button = BrandButton(
            buttons, "СБРОСИТЬ", self._reset, palette=palette, kind="ghost",
            height=_px(40, scale), font=theme.font_text(11),
        )
        self.reset_button.pack(side="right", padx=(0, 8))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        try:
            self.canvas.itemconfigure(self._body_window, width=int(event.width))
        except tk.TclError:
            pass

    def _on_body_configure(self, _event: tk.Event | None = None) -> None:
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except tk.TclError:
            pass

    def _on_mousewheel(self, event: tk.Event) -> None:
        if not self.visible:
            return
        try:
            delta = int(-1 * (event.delta / 120)) if event.delta else 0
            if delta:
                self.canvas.yview_scroll(delta, "units")
        except (tk.TclError, TypeError, ValueError):
            pass

    def _card(self, row: int, icon: str, kicker: str, title: str) -> tk.Frame:
        """Секция-полоса канона: чередование bg/bg_alt, без рамок и теней."""
        palette = self.palette
        scale = self.ui_scale
        stripe_bg = palette["bg"] if row % 2 == 0 else palette["bg_alt"]
        self._section_bg = stripe_bg
        try:
            style = ttk.Style(self.win)
            style.configure(
                "SaCard.TCheckbutton",
                background=stripe_bg,
                foreground=palette["text"],
                indicatorbackground=palette["surface"],
                indicatorforeground=palette["on_accent"],
                focuscolor=palette["accent"],
                indicatorsize=12,
            )
        except tk.TclError:
            pass
        outer = tk.Frame(self.body, bg=stripe_bg)
        outer.grid(row=row, column=0, sticky="ew")
        inner = tk.Frame(
            outer, bg=stripe_bg, padx=_px(16, scale), pady=_px(12, scale)
        )
        inner.pack(fill="both", expand=True)
        inner.columnconfigure(1, weight=1)
        head = tk.Frame(inner, bg=stripe_bg)
        head.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, _px(8, scale)))
        tk.Label(
            head,
            text=theme.micro(kicker),
            bg=stripe_bg,
            fg=palette["text_muted"],
            font=theme.font_text(9),
        ).pack(side="left")
        tk.Label(
            head,
            text=title,
            bg=stripe_bg,
            fg=palette["text"],
            font=theme.font_display(18, "bold"),
            anchor="w",
        ).pack(side="left", padx=(_px(12, scale), 0))
        return inner

    def _field_label(self, parent: tk.Misc, row: int, column: int, text: str) -> tk.Label:
        try:
            bg = str(parent.cget("bg"))
        except tk.TclError:
            bg = self._section_bg
        label = tk.Label(
            parent,
            text=theme.micro(text),
            bg=bg,
            fg=self.palette["text_secondary"],
            font=theme.font_text(9),
            anchor="w",
        )
        label.grid(
            row=row, column=column, sticky="w",
            padx=(0, _px(10, self.ui_scale)),
            pady=(0, _px(4, self.ui_scale)),
        )
        return label

    # -- section 1: recognition --------------------------------------------

    def _build_recognition_section(self, row: int) -> None:
        scale = self.ui_scale
        card = self._card(row, "model", "01 / Распознавание", "Распознавание")
        self._field_label(card, 1, 0, "Модель")
        models = list(config_models())
        self.model_combo = ttk.Combobox(card, values=models, state="readonly", style="Sa.TCombobox")
        model = str(self.settings.get("model", "small"))
        self.model_combo.set(model if model in models else models[0])
        self.model_combo.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale)))

        self._field_label(card, 2, 0, "Устройство STT")
        self.stt_device_combo = ttk.Combobox(
            card,
            values=[STT_DEVICE_VALUES["auto"], STT_DEVICE_VALUES["cuda"], STT_DEVICE_VALUES["cpu"]],
            state="readonly", style="Sa.TCombobox",
        )
        self.stt_device_combo.set(
            STT_DEVICE_VALUES.get(str(self.settings.get("sttDevice", "auto")), STT_DEVICE_VALUES["auto"])
        )
        self.stt_device_combo.grid(row=2, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale)))

        self._field_label(card, 3, 0, "Beam size")
        beam_frame = tk.Frame(card, bg=self._section_bg)
        beam_frame.grid(row=3, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale)))
        beam_frame.columnconfigure(0, weight=1)
        self.beam_scale = BrandSlider(
            beam_frame, from_=1, to=8, step=1,
            value=int(self.settings.get("beamSize", 5)),
            palette=self.palette, command=self._update_beam_label,
        )
        self.beam_scale.grid(row=0, column=0, sticky="ew")
        self.beam_label = tk.Label(
            beam_frame, text="", bg=self._section_bg,
            fg=self.palette["text_secondary"], font=theme.font_text(9), width=4, anchor="e",
        )
        self.beam_label.grid(row=0, column=1, padx=(_px(8, scale), 0))
        self._update_beam_label(self.beam_scale.get())

        self._field_label(card, 4, 0, "Язык")
        self.language_combo = ttk.Combobox(
            card, values=list(_LANGUAGE_LABELS.values()), state="readonly", style="Sa.TCombobox"
        )
        language = str(self.settings.get("language", "ru"))
        self.language_combo.set(_LANGUAGE_LABELS.get(language, _LANGUAGE_LABELS["ru"]))
        self.language_combo.grid(row=4, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale)))

        self.vad_var = tk.BooleanVar(value=bool(self.settings.get("vad", True)))
        BrandCheck(
            card, "Silero VAD (фильтр тишины)", self.vad_var,
            palette=self.palette, bg=self._section_bg,
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(0, _px(4, scale)))

        status_frame = tk.Frame(card, bg=self._section_bg)
        status_frame.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(_px(2, scale), 0))
        status_frame.columnconfigure(0, weight=1)
        self.model_status_label = tk.Label(
            status_frame, text="", bg=self._section_bg,
            fg=self.palette["text_secondary"], font=theme.font_text(9), anchor="w",
            justify="left", wraplength=_px(520, scale),
        )
        self.model_status_label.grid(row=0, column=0, sticky="w")
        self.reload_button = BrandButton(
            status_frame, "ПЕРЕЗАГРУЗИТЬ МОДЕЛЬ →", self._reload_model,
            palette=self.palette, kind="ghost", height=_px(28, scale),
            font=theme.font_text(10), tooltip="Перезагрузить модель распознавания",
        )
        self.reload_button.grid(row=0, column=1, padx=(_px(8, scale), 0))

    # -- section 2: microphone ---------------------------------------------

    def _build_microphone_section(self, row: int) -> None:
        scale = self.ui_scale
        card = self._card(row, "volume", "02 / Микрофон", "Микрофон")
        self._field_label(card, 1, 0, "Устройство")
        device_values = ["Системный по умолчанию"] + [
            f"{name} (#{index})" for index, name in self._devices
        ]
        self.device_combo = ttk.Combobox(
            card, values=device_values, state="readonly", style="Sa.TCombobox"
        )
        device = self.settings.get("device")
        current = 0
        if isinstance(device, int):
            for position, (index, _name) in enumerate(self._devices, start=1):
                if index == device:
                    current = position
                    break
        self.device_combo.current(current)
        self.device_combo.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale)))

        self.test_mic_button = BrandButton(
            card, "ПРОВЕРИТЬ МИКРОФОН", self._test_mic, palette=self.palette,
            kind="secondary", icon="mic", height=_px(36, scale),
            font=theme.font_text(10),
        )
        self.test_mic_button.grid(row=2, column=0, columnspan=2, sticky="w")
        self.mic_result_label = tk.Label(
            card, text="", bg=self._section_bg,
            fg=self.palette["text_secondary"], font=theme.font_text(9),
            anchor="w", justify="left", wraplength=_px(480, scale),
        )
        self.mic_result_label.grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(_px(4, scale), 0)
        )

    # -- section 3: appearance ---------------------------------------------

    def _build_appearance_section(self, row: int) -> None:
        scale = self.ui_scale
        card = self._card(row, "appearance", "03 / Внешний вид", "Внешний вид")
        self._field_label(card, 1, 0, "Тема")
        self.theme_combo = ttk.Combobox(
            card, values=list(THEME_LABELS.values()), state="readonly", style="Sa.TCombobox"
        )
        self.theme_combo.set(THEME_LABELS.get(self.theme_name, THEME_LABELS["scene"]))
        self.theme_combo.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale)))

        self._field_label(card, 2, 0, "Акцент")
        accent_holder = tk.Frame(card, bg=self._section_bg)
        accent_holder.grid(row=2, column=1, columnspan=3, sticky="w", pady=(0, _px(4, scale)))
        self.accent_swatches: list[tk.Canvas] = []
        self._accent_items: list[int] = []
        for color, label in theme.ACCENT_PRESETS:
            swatch = tk.Canvas(
                accent_holder, width=28, height=28, bg=self._section_bg,
                highlightthickness=0, bd=0, cursor="hand2",
            )
            swatch.pack(side="left", padx=(0, _px(6, scale)))
            item = swatch.create_rectangle(
                2, 2, 26, 26, fill=color, outline=self.palette["border"], width=1
            )
            swatch.bind("<Button-1>", lambda _event, value=color: self._select_accent(value))
            self.accent_swatches.append(swatch)
            self._accent_items.append(item)
        self.accent_value_label = tk.Label(
            accent_holder, text="", bg=self._section_bg,
            fg=self.palette["text_muted"], font=theme.font_text(9),
        )
        self.accent_value_label.pack(side="left", padx=(_px(8, scale), 0))
        self._select_accent(self._accent)

        self._field_label(card, 3, 0, "Размер окна")
        self.size_combo = ttk.Combobox(
            card, values=list(SIZE_LABELS.values()), state="readonly", style="Sa.TCombobox"
        )
        self.size_combo.set(SIZE_LABELS[normalize_size_mode(self.settings.get("sizeMode"))])
        self.size_combo.grid(row=3, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale)))

        self._field_label(card, 4, 0, "Масштаб интерфейса")
        scale_frame = tk.Frame(card, bg=self._section_bg)
        scale_frame.grid(row=4, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale)))
        scale_frame.columnconfigure(0, weight=1)
        self.ui_scale_slider = BrandSlider(
            scale_frame, from_=80, to=160, step=5,
            value=int(round(self.ui_scale * 100)),
            palette=self.palette, command=self._update_ui_scale_label,
        )
        self.ui_scale_slider.grid(row=0, column=0, sticky="ew")
        self.ui_scale_label = tk.Label(
            scale_frame, text="", bg=self._section_bg,
            fg=self.palette["text_secondary"], font=theme.font_text(10), width=6, anchor="e",
        )
        self.ui_scale_label.grid(row=0, column=1, padx=(_px(8, scale), 0))
        self._update_ui_scale_label(self.ui_scale_slider.get())

        self.pinned_var = tk.BooleanVar(value=bool(self.settings.get("pinned", False)))
        BrandCheck(
            card, "Закрепить как виджет (поверх окон)", self.pinned_var,
            palette=self.palette, bg=self._section_bg,
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(_px(4, scale), 0))
        self.show_elapsed_var = tk.BooleanVar(value=bool(self.settings.get("showElapsed", True)))
        BrandCheck(
            card, "Показывать таймер записи", self.show_elapsed_var,
            palette=self.palette, bg=self._section_bg,
        ).grid(row=6, column=0, columnspan=4, sticky="w")

    # -- section 4: control ------------------------------------------------

    def _build_control_section(self, row: int) -> None:
        scale = self.ui_scale
        card = self._card(row, "keyboard", "04 / Управление", "Управление")
        self._hotkey_row(card, 1, "Горячая клавиша", "hotkey", self._current_hotkey)
        self._hotkey_row(
            card, 2, "Клавиша настроек", "settingsHotkey", self._current_settings_hotkey
        )
        tk.Label(
            card,
            text="Сочетание записывается нажатием. Win-клавишу впишите вручную.",
            bg=self._section_bg, fg=self.palette["text_muted"],
            font=theme.font_text(8), anchor="w",
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(0, _px(4, scale)))

    def _hotkey_row(self, parent: tk.Misc, row: int, label: str, target: str, value: str) -> None:
        scale = self.ui_scale
        self._field_label(parent, row, 0, label)
        holder = tk.Frame(parent, bg=self._section_bg)
        holder.grid(row=row, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale)))
        holder.columnconfigure(0, weight=1)
        entry = tk.Entry(
            holder, bg=self._section_bg, fg=self.palette["text"],
            readonlybackground=self._section_bg,
            insertbackground=self.palette["accent"], relief="flat", bd=0,
            font=theme.font_text(10), highlightthickness=1,
            highlightbackground=self.palette["border"], highlightcolor=self.palette["accent"],
        )
        entry.insert(0, value)
        entry.configure(state="readonly")
        entry.grid(row=0, column=0, sticky="ew", ipady=4, padx=(0, _px(8, scale)))
        button = BrandButton(
            holder, "ЗАПИСАТЬ СОЧЕТАНИЕ", lambda: self._start_capture(target),
            palette=self.palette, kind="secondary", icon="keyboard",
            font=theme.font_text(9),
        )
        button.grid(row=0, column=1)
        if target == "hotkey":
            self.hotkey_entry, self.hotkey_button = entry, button
        else:
            self.settings_hotkey_entry, self.settings_hotkey_button = entry, button

    # -- section 5: behavior -----------------------------------------------

    def _build_behavior_section(self, row: int) -> None:
        scale = self.ui_scale
        card = self._card(row, "settings", "05 / Поведение", "Поведение")
        self.auto_copy_var = tk.BooleanVar(value=bool(self.settings.get("autoCopy", True)))
        self.paste_ctrl_enter_var = tk.BooleanVar(
            value=bool(self.settings.get("pasteOnCtrlEnter", True))
        )
        self.single_line_var = tk.BooleanVar(value=bool(self.settings.get("singleLine", True)))
        try:
            from dictate import autostart

            autostart_enabled = autostart.is_enabled()
        except Exception:  # noqa: BLE001
            autostart_enabled = False
        self.autostart_var = tk.BooleanVar(value=autostart_enabled)
        checks = (
            ("Автокопировать при закрытии", self.auto_copy_var),
            ("Ctrl+Enter — вставить в предыдущее окно", self.paste_ctrl_enter_var),
            ("Одна строка", self.single_line_var),
            ("Запускать с Windows", self.autostart_var),
        )
        for index, (text, variable) in enumerate(checks, start=1):
            BrandCheck(
                card, text, variable, palette=self.palette, bg=self._section_bg
            ).grid(row=index, column=0, columnspan=4, sticky="w", pady=(0, _px(2, scale)))

        pause_row = len(checks) + 1
        self._field_label(card, pause_row, 0, "Пауза между фразами")
        pause_frame = tk.Frame(card, bg=self._section_bg)
        pause_frame.grid(
            row=pause_row, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale))
        )
        pause_frame.columnconfigure(0, weight=1)
        self.pause_scale = BrandSlider(
            pause_frame, from_=300, to=2000, step=50,
            value=int(self.settings.get("pauseMs", 800)),
            palette=self.palette, command=self._update_pause_label,
        )
        self.pause_scale.grid(row=0, column=0, sticky="ew")
        self.pause_label = tk.Label(
            pause_frame, text="", bg=self._section_bg,
            fg=self.palette["text_secondary"], font=theme.font_text(9), width=6, anchor="e",
        )
        self.pause_label.grid(row=0, column=1, padx=(_px(8, scale), 0))
        self._update_pause_label(self.pause_scale.get())

        limit_row = pause_row + 1
        self._field_label(card, limit_row, 0, "Максимальная запись")
        limit_frame = tk.Frame(card, bg=self._section_bg)
        limit_frame.grid(
            row=limit_row, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale))
        )
        limit_frame.columnconfigure(0, weight=1)
        self.limit_scale = BrandSlider(
            limit_frame, from_=10, to=600, step=10,
            value=max(10, min(600, int(self.settings.get("maxRecordMs", 600000)) // 1000)),
            palette=self.palette, command=self._update_limit_label,
        )
        self.limit_scale.grid(row=0, column=0, sticky="ew")
        self.max_record_scale = self.limit_scale
        self.limit_label = tk.Label(
            limit_frame, text="", bg=self._section_bg,
            fg=self.palette["text_secondary"], font=theme.font_text(9), width=6, anchor="e",
        )
        self.limit_label.grid(row=0, column=1, padx=(_px(8, scale), 0))
        self._update_limit_label(self.limit_scale.get())

        history_row = limit_row + 1
        self._field_label(card, history_row, 0, "История (записей)")
        history_frame = tk.Frame(card, bg=self._section_bg)
        history_frame.grid(
            row=history_row, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale))
        )
        history_frame.columnconfigure(0, weight=1)
        self.history_scale = BrandSlider(
            history_frame, from_=0, to=200, step=10,
            value=int(self.settings.get("historyLimit", 50)),
            palette=self.palette, command=self._update_history_label,
        )
        self.history_scale.grid(row=0, column=0, sticky="ew")
        self.history_label = tk.Label(
            history_frame, text="", bg=self._section_bg,
            fg=self.palette["text_secondary"], font=theme.font_text(9), width=4, anchor="e",
        )
        self.history_label.grid(row=0, column=1, padx=(_px(8, scale), 0))
        self._update_history_label(self.history_scale.get())

    # -- section 6: about --------------------------------------------------

    # -- section 7: postprocess -------------------------------------------

    def _build_postprocess_section(self, row: int) -> None:
        scale = self.ui_scale
        card = self._card(row, "settings", "07 / Постобработка", "Постобработка")
        self.auto_postprocess_var = tk.BooleanVar(
            value=bool(self.settings.get("autoPostprocess", True))
        )
        BrandCheck(
            card, "Пунктуация и грамматика после диктовки", self.auto_postprocess_var,
            palette=self.palette, bg=self._section_bg,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, _px(4, scale)))
        self.suggest_contested_var = tk.BooleanVar(
            value=bool(self.settings.get("suggestContested", True))
        )
        BrandCheck(
            card, "Показывать спорные слова", self.suggest_contested_var,
            palette=self.palette, bg=self._section_bg,
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, _px(4, scale)))

        self._field_label(card, 3, 0, "Хранить сессий")
        limit_frame = tk.Frame(card, bg=self._section_bg)
        limit_frame.grid(row=3, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale)))
        limit_frame.columnconfigure(0, weight=1)
        self.sessions_limit_scale = BrandSlider(
            limit_frame, from_=20, to=1000, step=20,
            value=max(20, min(1000, int(self.settings.get("sessionsLimit", 300)))),
            palette=self.palette,
        )
        self.sessions_limit_scale.grid(row=0, column=0, sticky="ew")
        self.sessions_limit_label = tk.Label(
            limit_frame, text=str(int(self.sessions_limit_scale.get())),
            bg=self._section_bg, fg=self.palette["text_secondary"],
            font=theme.font_text(10), width=5, anchor="e",
        )
        self.sessions_limit_label.grid(row=0, column=1, padx=(_px(8, scale), 0))

        def _on_limit(_value: Any) -> None:
            try:
                self.sessions_limit_label.configure(text=str(int(float(_value))))
            except (tk.TclError, TypeError, ValueError):
                pass

        self.sessions_limit_scale._command = _on_limit

        self.history_settings_button = BrandButton(
            card, "ИСТОРИЯ →", self._history_clicked, palette=self.palette,
            kind="ghost", height=_px(32, scale), font=theme.font_text(10),
        )
        self.history_settings_button.grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(_px(2, scale), 0)
        )

    def _history_clicked(self) -> None:
        if self.on_history is not None:
            try:
                self.on_history()
            except Exception:  # noqa: BLE001
                LOG.exception("Ошибка открытия истории из настроек")

    # -- section 8: LLM improvement ---------------------------------------

    def _build_llm_section(self, row: int) -> None:
        scale = self.ui_scale
        from dictate import llm as llm_module

        card = self._card(row, "settings", "08 / Улучшение текста", "Улучшение текста")

        self._field_label(card, 1, 0, "API-ключ DeepSeek")
        key_row = tk.Frame(card, bg=self._section_bg)
        key_row.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale)))
        key_row.columnconfigure(0, weight=1)
        self.llm_key_entry = tk.Entry(
            key_row, show="•", bg=self.palette["surface"], fg=self.palette["text"],
            insertbackground=self.palette["accent"], relief="flat", bd=0,
            font=theme.font_text(11), highlightthickness=1,
            highlightbackground=self.palette["border"],
            highlightcolor=self.palette["accent"],
        )
        self.llm_key_entry.insert(0, str(self.settings.get("llmApiKey", "")))
        self.llm_key_entry.grid(row=0, column=0, sticky="ew", ipady=_px(4, scale))
        self.show_key_button = BrandButton(
            key_row, "ПОКАЗАТЬ", self._toggle_key_visibility, palette=self.palette,
            kind="ghost", height=_px(30, scale), font=theme.font_text(9),
        )
        self.show_key_button.grid(row=0, column=1, padx=(_px(6, scale), 0))
        self._key_visible = False

        self._field_label(card, 2, 0, "Модель")
        self.llm_model_combo = ttk.Combobox(
            card, values=list(llm_module.MODELS), state="readonly", style="Sa.TCombobox"
        )
        model = str(self.settings.get("llmModel", llm_module.DEFAULT_MODEL))
        self.llm_model_combo.set(model if model in llm_module.MODELS else llm_module.DEFAULT_MODEL)
        self.llm_model_combo.grid(row=2, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale)))

        self._field_label(card, 3, 0, "Пресет")
        self.llm_preset_labels = dict(llm_module.PRESETS)
        self.llm_preset_values = {label: key for key, label in llm_module.PRESETS.items()}
        self.llm_preset_combo = ttk.Combobox(
            card, values=list(self.llm_preset_labels.values()),
            state="readonly", style="Sa.TCombobox",
        )
        preset = str(self.settings.get("llmPreset", "standard"))
        self.llm_preset_combo.set(self.llm_preset_labels.get(preset, "Стандарт"))
        self.llm_preset_combo.grid(row=3, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale)))
        self.llm_preset_combo.bind("<<ComboboxSelected>>", self._on_llm_preset)

        self._field_label(card, 4, 0, "Сила")
        strength_frame = tk.Frame(card, bg=self._section_bg)
        strength_frame.grid(row=4, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale)))
        strength_frame.columnconfigure(0, weight=1)
        self.llm_strength_scale = BrandSlider(
            strength_frame, from_=1, to=5, step=1,
            value=max(1, min(5, int(self.settings.get("llmStrength", 3)))),
            palette=self.palette,
        )
        self.llm_strength_scale.grid(row=0, column=0, sticky="ew")
        self.llm_strength_label = tk.Label(
            strength_frame, text=str(int(self.llm_strength_scale.get())),
            bg=self._section_bg, fg=self.palette["text_secondary"],
            font=theme.font_text(10), width=3, anchor="e",
        )
        self.llm_strength_label.grid(row=0, column=1, padx=(_px(8, scale), 0))
        self.llm_strength_scale._command = lambda value: self.llm_strength_label.configure(
            text=str(int(float(value)))
        )

        self._field_label(card, 5, 0, "Качество")
        quality_frame = tk.Frame(card, bg=self._section_bg)
        quality_frame.grid(row=5, column=1, columnspan=3, sticky="ew", pady=(0, _px(4, scale)))
        quality_frame.columnconfigure(0, weight=1)
        self.llm_quality_scale = BrandSlider(
            quality_frame, from_=1, to=5, step=1,
            value=max(1, min(5, int(self.settings.get("llmQuality", 3)))),
            palette=self.palette,
        )
        self.llm_quality_scale.grid(row=0, column=0, sticky="ew")
        self.llm_quality_label = tk.Label(
            quality_frame, text=str(int(self.llm_quality_scale.get())),
            bg=self._section_bg, fg=self.palette["text_secondary"],
            font=theme.font_text(10), width=3, anchor="e",
        )
        self.llm_quality_label.grid(row=0, column=1, padx=(_px(8, scale), 0))
        self.llm_quality_scale._command = lambda value: self.llm_quality_label.configure(
            text=str(int(float(value)))
        )

        actions = tk.Frame(card, bg=self._section_bg)
        actions.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(_px(2, scale), 0))
        self.llm_check_button = BrandButton(
            actions, "ПРОВЕРИТЬ КЛЮЧ →", self.check_key, palette=self.palette,
            kind="ghost", height=_px(32, scale), font=theme.font_text(10),
        )
        self.llm_check_button.pack(side="left")
        self.llm_status_label = tk.Label(
            actions, text="", bg=self._section_bg, fg=self.palette["text_muted"],
            font=theme.font_text(9), anchor="w",
        )
        self.llm_status_label.pack(side="left", padx=(_px(10, scale), 0))
        tk.Label(
            card,
            text=(
                "Ключ хранится в settings.json; запросы уходят только по кнопке "
                "«Улучшить»."
            ),
            bg=self._section_bg, fg=self.palette["text_muted"],
            font=theme.font_text(9), anchor="w", justify="left",
        ).grid(row=7, column=0, columnspan=4, sticky="w", pady=(_px(6, scale), 0))

    def _toggle_key_visibility(self) -> None:
        self._key_visible = not self._key_visible
        try:
            self.llm_key_entry.configure(show="" if self._key_visible else "•")
            self.show_key_button.set_text("СКРЫТЬ" if self._key_visible else "ПОКАЗАТЬ")
        except tk.TclError:
            pass

    def _on_llm_preset(self, _event: tk.Event | None = None) -> None:
        from dictate import llm as llm_module

        label = str(self.llm_preset_combo.get())
        preset = self.llm_preset_values.get(label, "standard")
        strength, quality = llm_module.PRESET_DEFAULTS.get(preset, (3, 3))
        try:
            self.llm_strength_scale.set(strength)
            self.llm_quality_scale.set(quality)
            self.llm_strength_label.configure(text=str(strength))
            self.llm_quality_label.configure(text=str(quality))
        except tk.TclError:
            pass

    def check_key_sync(self) -> tuple[bool, str]:
        """Проверка ключа в текущем потоке (кнопка запускает её в фоне)."""
        key = self.llm_key_entry.get().strip()
        model = str(self.llm_model_combo.get()) or "deepseek-chat"
        if self.on_check_key is not None:
            try:
                return self.on_check_key(key, model)
            except Exception as exc:  # noqa: BLE001
                return False, f"Не удалось проверить ключ: {exc}"
        try:
            from dictate import llm as llm_module

            return llm_module.check_key(key, model=model)
        except Exception as exc:  # noqa: BLE001
            return False, f"Не удалось проверить ключ: {exc}"

    def check_key(self) -> None:
        """Кнопка «Проверить ключ»: запрос в фоне, UI не блокируется."""
        try:
            self.llm_check_button.set_enabled(False)
            self.llm_check_button.set_text("ПРОВЕРЯЮ…")
            self.set_llm_status("Проверяю ключ…", error=False)
        except tk.TclError:
            pass
        import threading

        def worker() -> None:
            ok, message = self.check_key_sync()
            try:
                self.win.after(0, lambda: self._finish_key_check(ok, message))
            except tk.TclError:
                pass

        threading.Thread(target=worker, name="WinDictateKeyCheck", daemon=True).start()

    def _finish_key_check(self, ok: bool, message: str) -> None:
        try:
            self.llm_check_button.set_enabled(True)
            self.llm_check_button.set_text("ПРОВЕРИТЬ КЛЮЧ →")
        except tk.TclError:
            pass
        self.set_llm_status(message, error=not ok)

    def set_llm_status(self, message: str, error: bool = False) -> None:
        try:
            self.llm_status_label.configure(
                text=message,
                fg=self.palette["danger"] if error else self.palette["text_muted"],
            )
        except tk.TclError:
            pass

    def _build_about_section(self, row: int) -> None:
        from dictate import config

        scale = self.ui_scale
        card = self._card(row, "info", "06 / О программе", "О программе")
        lines = (
            f"Версия: {theme.APP_VERSION}",
            f"Данные: {config.data_dir()}",
            f"Логи: {config.logs_dir()}",
        )
        for index, line in enumerate(lines, start=1):
            tk.Label(
                card, text=line, bg=self._section_bg,
                fg=self.palette["text_secondary"], font=theme.font_text(8),
                anchor="w", justify="left",
            ).grid(row=index, column=0, columnspan=4, sticky="w")
        if self.on_open_logs is not None:
            logs_button = BrandButton(
                card, "ОТКРЫТЬ ПАПКУ ЛОГОВ →", self.on_open_logs, palette=self.palette,
                kind="secondary", icon="info", font=theme.font_text(9),
            )
            logs_button.grid(row=4, column=0, columnspan=2, sticky="w", pady=(_px(4, scale), 0))

    # -- callbacks ---------------------------------------------------------

    def _update_beam_label(self, raw: Any) -> None:
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = 5
        self.beam_label.configure(text=str(value))

    def _update_ui_scale_label(self, raw: Any) -> None:
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = 100
        self.ui_scale_label.configure(text=f"{value / 100:.2f}".replace(".", ","))

    def _update_history_label(self, raw: Any) -> None:
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = 50
        self.history_label.configure(text=str(value))

    def _update_pause_label(self, raw: Any) -> None:
        try:
            milliseconds = int(float(raw))
        except (TypeError, ValueError):
            milliseconds = 800
        self.pause_label.configure(text=f"{milliseconds / 1000:.1f} с".replace(".", ","))

    def _update_limit_label(self, raw: Any) -> None:
        try:
            seconds = int(float(raw))
        except (TypeError, ValueError):
            seconds = 600
        self.limit_label.configure(text=f"{seconds // 60}:{seconds % 60:02d}")

    def _current_model_status(self) -> tuple[str, bool]:
        if self.on_model_status is None:
            return ("", False)
        try:
            text, error = self.on_model_status()
            return (str(text or ""), bool(error))
        except Exception:  # noqa: BLE001
            LOG.exception("Не удалось получить статус модели")
            return ("Статус модели недоступен", True)

    def _schedule_model_status(self) -> None:
        if self.on_model_status is None:
            return
        try:
            self._model_job = self.win.after(1500, self._tick_model_status)
        except tk.TclError:
            self._model_job = None

    def _tick_model_status(self) -> None:
        self._model_job = None
        if not self.visible:
            return
        self.set_model_status(*self._current_model_status())
        self._schedule_model_status()

    def set_model_status(self, text: str, error: bool = False) -> None:
        try:
            self.model_status_label.configure(
                text=text or "",
                fg=self.palette["danger"] if error else self.palette["text_secondary"],
            )
        except tk.TclError:
            pass

    def _reload_model(self) -> None:
        if self.on_reload_model is None:
            self.set_model_status("Перезагрузка модели недоступна", True)
            return
        self.set_model_status("Перезагружаю модель…", False)
        self.reload_button.set_enabled(False)
        try:
            self.on_reload_model(self.get_settings())
        except Exception as exc:  # noqa: BLE001
            self.reload_button.set_enabled(True)
            self.set_model_status(f"Не удалось перезагрузить модель: {exc}", True)

    def set_reload_enabled(self, enabled: bool = True) -> None:
        try:
            self.reload_button.set_enabled(enabled)
        except tk.TclError:
            pass

    # -- accent ------------------------------------------------------------

    def _select_accent(self, color: str) -> None:
        normalized = theme.normalize_hex(color) or theme.DEFAULT_ACCENT
        self._accent = normalized
        palette = theme.colors(self.theme_name, normalized)
        for swatch, item, (preset, preset_name) in zip(
            self.accent_swatches, self._accent_items, theme.ACCENT_PRESETS
        ):
            selected = theme.normalize_hex(preset) == normalized
            try:
                swatch.itemconfigure(
                    item,
                    outline=palette["accent"] if selected else palette["border"],
                    width=2 if selected else 1,
                )
            except tk.TclError:
                pass
        value_name = next(
            (
                name
                for preset, name in theme.ACCENT_PRESETS
                if theme.normalize_hex(preset) == normalized
            ),
            None,
        )
        try:
            self.accent_value_label.configure(
                text=value_name or theme.tracked("Свой цвет")
            )
        except tk.TclError:
            pass

    # -- hotkey capture ----------------------------------------------------

    def _start_capture(self, target: str) -> None:
        self._cancel_capture()
        self._capture_target = target
        button = self.hotkey_button if target == "hotkey" else self.settings_hotkey_button
        self._capture_button = button
        button.set_text("Нажмите сочетание…")
        self.win.bind("<KeyPress>", self._capture_key)
        self.win.focus_force()

    def _cancel_capture(self, *_args: Any) -> None:
        if self._capture_target is None:
            return
        if self._capture_button is not None:
            try:
                self._capture_button.set_text("ЗАПИСАТЬ СОЧЕТАНИЕ")
            except tk.TclError:
                pass
        self._capture_target = None
        self._capture_button = None
        try:
            self.win.unbind("<KeyPress>")
        except tk.TclError:
            pass

    def _capture_key(self, event: tk.Event) -> str:
        target = self._capture_target
        if target is None:
            return "break"
        if str(getattr(event, "keysym", "")) == "Escape":
            self._cancel_capture()
            return "break"
        spec = spec_from_tk_event(event)
        if spec is None:
            self.set_mic_result("Нужно сочетание с Ctrl/Alt/Shift или клавиша F1–F24.")
            return "break"
        entry = self.hotkey_entry if target == "hotkey" else self.settings_hotkey_entry
        entry.configure(state="normal")
        entry.delete(0, "end")
        entry.insert(0, spec)
        entry.configure(state="readonly")
        self._cancel_capture()
        self._check_captured_hotkey(spec)
        return "break"

    def _check_captured_hotkey(self, spec: str) -> None:
        if self.on_probe_hotkey is None:
            return
        try:
            available = bool(self.on_probe_hotkey(spec))
        except Exception:  # noqa: BLE001
            return
        if available:
            self.set_status(f"Сочетание «{spec}» свободно — можно сохранять.", ok=True)
            return
        free: str | None = None
        for candidate in HOTKEY_CANDIDATES:
            if candidate == spec:
                continue
            try:
                if self.on_probe_hotkey(candidate):
                    free = candidate
                    break
            except Exception:  # noqa: BLE001
                continue
        message = f"Сочетание «{spec}» занято другой программой — выберите другое."
        if free:
            message += f" Свободно, например: {free}."
        self.set_status(message, error=True)

    # -- actions -----------------------------------------------------------

    def _test_mic(self) -> None:
        if self.on_test_mic is None:
            self.set_mic_result("Проверка микрофона недоступна.")
            return
        self.set_mic_result("Записываю 3 секунды…")
        try:
            self.on_test_mic(self.get_settings())
        except Exception as exc:  # noqa: BLE001
            self.set_mic_result(f"Ошибка проверки: {exc}")

    def set_mic_result(self, text: str) -> None:
        try:
            self.mic_result_label.configure(text=text or "")
        except tk.TclError:
            pass

    def set_status(self, text: str, error: bool = False, ok: bool = False) -> None:
        self._status_text = str(text or "")
        color = (
            self.palette["danger"] if error
            else self.palette["ok"] if ok
            else self.palette["text_muted"]
        )
        try:
            self.status_label.configure(text=self._status_text, fg=color)
        except tk.TclError:
            pass

    def _save(self) -> bool:
        data = self.get_settings()
        self.set_status("")
        result: Any = None
        if self.on_save is not None:
            try:
                result = self.on_save(data)
            except Exception as exc:  # noqa: BLE001
                LOG.exception("Не удалось сохранить настройки")
                self.set_status(f"Не удалось сохранить: {exc}", error=True)
                return False
        if result is False:
            if not self._status_text:
                self.set_status("Не удалось применить настройки — исправьте ошибку и повторите.", error=True)
            return False
        self.set_status("Настройки сохранены.", ok=True)
        self._schedule_hide(700)
        return True

    def _reset(self) -> bool:
        from tkinter import messagebox

        try:
            confirmed = messagebox.askyesno(
                "Сбросить настройки",
                "Вернуть все настройки к значениям по умолчанию?",
                parent=self.win,
            )
        except Exception:  # noqa: BLE001
            confirmed = False
        if not confirmed:
            return False
        from dictate import config as config_module

        defaults = dict(config_module.DEFAULTS)
        defaults["autostart"] = bool(self.autostart_var.get())
        result: Any = None
        if self.on_save is not None:
            try:
                result = self.on_save(defaults)
            except Exception as exc:  # noqa: BLE001
                LOG.exception("Не удалось применить настройки по умолчанию")
                self.set_status(f"Не удалось сбросить настройки: {exc}", error=True)
                return False
        if result is False:
            if not self._status_text:
                self.set_status("Не удалось применить значения по умолчанию.", error=True)
            return False
        self.settings = dict(config_module.DEFAULTS)
        self.theme_name = normalize_theme(self.settings.get("theme", "scene"))
        self._accent = theme.normalize_hex(self.settings.get("accent")) or theme.DEFAULT_ACCENT
        self.palette = theme.colors(self.theme_name, self._accent)
        for child in self.win.winfo_children():
            child.destroy()
        self._configure_ttk()
        self._build()
        self._center()
        _apply_dark_titlebar(self.win, self.theme_name == "scene")
        self.set_status("Настройки сброшены к значениям по умолчанию.", ok=True)
        self._schedule_hide(900)
        return True

    def _schedule_hide(self, delay_ms: int) -> None:
        self._cancel_hide()
        try:
            self._hide_job = self.win.after(delay_ms, self.hide)
        except tk.TclError:
            self._hide_job = None

    def _cancel_hide(self) -> None:
        job = self._hide_job
        self._hide_job = None
        if job is not None:
            try:
                self.win.after_cancel(job)
            except tk.TclError:
                pass

    # -- collect -----------------------------------------------------------

    def get_settings(self) -> dict[str, Any]:
        device_index: int | None = None
        position = int(self.device_combo.current())
        if position > 0 and position - 1 < len(self._devices):
            device_index = self._devices[position - 1][0]
        language = _LANGUAGE_VALUES.get(self.language_combo.get(), "ru")
        settings = dict(self.settings)
        settings.update(
            {
                "device": device_index,
                "language": language,
                "model": self.model_combo.get() or "small",
                "sttDevice": STT_DEVICE_LABELS.get(self.stt_device_combo.get(), "auto"),
                "vad": bool(self.vad_var.get()),
                "beamSize": int(float(self.beam_scale.get())),
                "pauseMs": int(float(self.pause_scale.get())),
                "maxRecordMs": int(float(self.limit_scale.get())) * 1000,
                "hotkey": self.hotkey_entry.get().strip() or "Ctrl+Alt+Space",
                "settingsHotkey": self.settings_hotkey_entry.get().strip() or "Ctrl+Alt+S",
                "autoCopy": bool(self.auto_copy_var.get()),
                "pasteOnCtrlEnter": bool(self.paste_ctrl_enter_var.get()),
                "singleLine": bool(self.single_line_var.get()),
                "historyLimit": int(float(self.history_scale.get())),
                "uiScale": round(float(self.ui_scale_slider.get()) / 100.0, 2),
                "theme": THEME_VALUES.get(self.theme_combo.get(), "scene"),
                "accent": self._accent,
                "sizeMode": SIZE_VALUES.get(self.size_combo.get(), "normal"),
                "pinned": bool(self.pinned_var.get()),
                "showElapsed": bool(self.show_elapsed_var.get()),
                "autoPostprocess": bool(self.auto_postprocess_var.get()),
                "suggestContested": bool(self.suggest_contested_var.get()),
                "sessionsLimit": max(20, min(1000, int(float(self.sessions_limit_scale.get())))),
                "llmApiKey": self.llm_key_entry.get().strip(),
                "llmModel": str(self.llm_model_combo.get()) or "deepseek-chat",
                "llmPreset": self.llm_preset_values.get(
                    str(self.llm_preset_combo.get()), "standard"
                ),
                "llmStrength": max(1, min(5, int(float(self.llm_strength_scale.get())))),
                "llmQuality": max(1, min(5, int(float(self.llm_quality_scale.get())))),
                "settingsW": max(560, int(self.win.winfo_width() or 0)),
                "settingsH": max(520, int(self.win.winfo_height() or 0)),
                "autostart": bool(self.autostart_var.get()),
            }
        )
        return settings

    # -- visibility --------------------------------------------------------

    def show(self) -> None:
        self.win.deiconify()
        self.win.lift()
        try:
            self.win.attributes("-topmost", True)
            self.win.focus_force()
        except tk.TclError:
            pass
        _apply_dark_titlebar(self.win, self.theme_name == "scene")
        self.visible = True
        self.set_model_status(*self._current_model_status())
        self._schedule_model_status()
        self._on_body_configure()

    def hide(self) -> None:
        self._cancel_capture()
        self._cancel_hide()
        self._cancel_model_status()
        try:
            self.win.withdraw()
        except tk.TclError:
            pass
        self.visible = False

    def _cancel_model_status(self) -> None:
        job = self._model_job
        self._model_job = None
        if job is not None:
            try:
                self.win.after_cancel(job)
            except tk.TclError:
                pass

    def destroy(self) -> None:
        self._cancel_capture()
        self._cancel_hide()
        self._cancel_model_status()
        try:
            self.canvas.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass
        try:
            self.win.after_cancel(self._size_job) if self._size_job else None
        except tk.TclError:
            pass
        try:
            self.win.destroy()
        except tk.TclError:
            pass


def config_models() -> tuple[str, ...]:
    from dictate import config

    return tuple(config.MODELS)
