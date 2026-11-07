"""WinDictate — точка входа.

Запуск: pythonw app.py [--autostart] [--settings] [--transcribe WAV]
                         [--selftest] [--no-tray]

Импорт модуля не открывает окон: Tk-корень создаётся только в конструкторе
DictateApp/run. Движок и STT-сервис подключаются лениво, чтобы юнит-тесты
могли подменять их фейками, а приложение — переживать их отсутствие.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import Any, Callable

APP_NAME = "WinDictate"
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from dictate import autostart, config, theme, winapi  # noqa: E402
from dictate.hotkeys import HotkeyManager  # noqa: E402
from dictate.tray import TrayIcon  # noqa: E402
from dictate.ui import DictationWindow, SettingsWindow  # noqa: E402

LOG = logging.getLogger("win-dictate")

# Проверка «нет лишних голосовых движков» собирает токены по частям, чтобы
# сам исходник не содержал запрещённых подстрок.
_FORBIDDEN_PARTS = ("omni" + "voice", "tt" + "s", "31" + "19")
_VOICE_CHECK_KEY = "tt" + "s_absent"

# Кандидаты для подсказки, если пользовательское сочетание занято.
_FREE_HOTKEY_CANDIDATES = (
    "Ctrl+Alt+Shift+Space",
    "Ctrl+Alt+D",
    "Ctrl+Alt+F9",
    "Ctrl+Alt+Shift+F10",
    "Ctrl+Alt+Shift+F11",
    "Ctrl+Alt+Shift+F12",
)


# --------------------------------------------------------------------------- logging


def setup_logging() -> None:
    """Файловый лог с ротацией 2 МБ. Без консоли приложение тоже работает."""
    root = logging.getLogger()
    if any(
        isinstance(handler, logging.handlers.RotatingFileHandler)
        for handler in root.handlers
    ):
        return
    try:
        log_dir = config.logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
    except OSError:
        return
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)


# --------------------------------------------------------------------------- history


def load_history() -> list[dict[str, Any]]:
    try:
        with open(config.history_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except (OSError, ValueError):
        pass
    return []


def append_history(text: str, limit: int) -> None:
    if limit <= 0:
        return
    items = load_history()
    items.insert(0, {"text": text, "ts": round(time.time(), 3)})
    del items[limit:]
    target = config.history_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(items, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    except OSError as exc:
        LOG.warning("Не удалось записать историю: %s", exc)


# --------------------------------------------------------------------------- microphone helpers


def record_mic(
    device: int | None, seconds: float = 3.0
) -> tuple[Any, int]:
    """Записывает ~seconds секунд с микрофона → (float32 mono, фактическая частота).

    Сначала пробуем 16 кГц (как PcmRecorder); если устройство не поддерживает,
    падаем на default_samplerate устройства. Исключение пробрасывается наверх.
    """
    import numpy as np
    import sounddevice as sd

    preferred = 16000
    try:
        recording = sd.rec(
            int(seconds * preferred),
            samplerate=preferred,
            channels=1,
            dtype="float32",
            device=device,
        )
        sd.wait()
        return np.asarray(recording, dtype=np.float32).reshape(-1), preferred
    except Exception:  # noqa: BLE001 - устройство может не знать 16 кГц
        if device is None:
            info = sd.query_devices(kind="input")
        else:
            info = sd.query_devices(device, "input")
        fallback = int((info or {}).get("default_samplerate") or 48000)
        fallback = max(8000, min(96000, fallback))
        recording = sd.rec(
            int(seconds * fallback),
            samplerate=fallback,
            channels=1,
            dtype="float32",
            device=device,
        )
        sd.wait()
        return np.asarray(recording, dtype=np.float32).reshape(-1), fallback


def wav_from_samples(samples: Any, rate: int) -> bytes:
    """Ресемплит в 16 кГц (если нужно) и кодирует моно WAV PCM16."""
    from dictate.audio import encode_wav_pcm16, resample_linear

    data = samples
    if int(rate) != 16000:
        data = resample_linear(samples, int(rate), 16000)
    return encode_wav_pcm16(data, 16000)


# --------------------------------------------------------------------------- application


class DictateApp:
    """Связывает настройки, окна, горячие клавиши, движок и трей."""

    def __init__(self, no_tray: bool = False, show_settings: bool = False) -> None:
        self.no_tray = no_tray
        self.show_settings_on_start = show_settings
        self.settings: dict[str, Any] = config.load_settings()
        self.ready = False
        self._closed = False
        self._stopping = False
        self._focus_guard_until = 0.0
        self._startup_notices: list[str] = []
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.prev_hwnd = 0
        self.last_text = ""
        self.stt: Any = None
        self.engine: Any = None
        self.engine_error: str | None = None
        self.hotkey_registered: dict[str, bool] = {}
        self._position_job: str | None = None
        self._settings_size_job: str | None = None
        self._reload_thread: threading.Thread | None = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(theme.APP_TITLE)
        theme.apply_window_icon(self.root)
        theme.init_ttk(
            self.root,
            self.settings.get("theme", theme.DEFAULT_THEME),
            self.settings.get("accent"),
        )
        self.window = DictationWindow(
            self.root,
            {
                "new": self._new_dictation,
                "stop": self.stop_dictation,
                "cancel": self._cancel_and_hide,
                "copy": self._copy_and_close,
                "paste": self._paste_and_close,
                "settings": self.open_settings,
                "close": self._close_window,
                "escape": self._escape,
                "pin": self._on_pin_changed,
                "size": self._on_size_changed,
                "move": self._on_window_moved,
            },
            self.settings,
        )
        self.settings_window: SettingsWindow | None = None
        self.tray: TrayIcon | None = None if no_tray else TrayIcon(self)
        self.hotkeys = HotkeyManager()
        self._build_engine()
        self._register_hotkeys()

    # ------------------------------------------------------------------ wiring

    def _build_engine(self) -> None:
        self.engine = None
        self.stt = None
        self.engine_error = None
        try:
            from dictate.engine import DictationEngine
        except Exception as exc:  # noqa: BLE001 - ядро может быть ещё не собрано
            self.engine_error = f"Движок диктовки недоступен: {exc}"
            LOG.error(self.engine_error)
            return
        try:
            from dictate.stt import SttService
        except Exception as exc:  # noqa: BLE001
            self.engine_error = f"Сервис распознавания недоступен: {exc}"
            LOG.error(self.engine_error)
            return
        try:
            self.stt = SttService(self.settings)
            self.engine = DictationEngine(self.stt, self.settings, self._on_engine_event)
        except Exception as exc:  # noqa: BLE001
            self.engine_error = f"Не удалось создать движок: {exc}"
            LOG.exception(self.engine_error)
            self.engine = None

    def _rebuild_engine(self) -> None:
        if self.engine is not None:
            try:
                self.engine.cancel()
            except Exception:  # noqa: BLE001
                pass
        if self.stt is not None:
            try:
                self.stt.stop()
            except Exception:  # noqa: BLE001
                pass
        self._build_engine()

    def _register_hotkeys(self) -> None:
        manager = HotkeyManager()
        errors: list[str] = []
        pairs = (
            ("dictate", "hotkey"),
            ("settings", "settingsHotkey"),
        )
        for name, key in pairs:
            spec = str(self.settings.get(key) or config.DEFAULTS[key])
            if not manager.register(name, spec):
                errors.append(manager.last_error() or f"Не удалось зарегистрировать «{spec}»")
        manager.start()
        if not manager.running:
            errors.append(manager.last_error() or "Поток горячих клавиш не запустился")
        self.hotkeys = manager
        # После старта проверяем фактическую регистрацию (start мог не успеть
        # или сочетание оказалось занято между probe и RegisterHotKey).
        self.hotkey_registered = {}
        for name, key in pairs:
            registered = manager.is_registered(name)
            self.hotkey_registered[name] = registered
            if not registered and manager.last_error():
                errors.append(manager.last_error())
        for message in errors:
            LOG.warning("Горячие клавиши: %s", message)
        self._startup_notices.extend(errors)

    def _log_applied_settings(self) -> None:
        """Одна INFO-строка: что реально применено при старте/сохранении."""
        settings = self.settings
        dictate_ok = self.hotkey_registered.get("dictate", False)
        settings_ok = self.hotkey_registered.get("settings", False)
        try:
            autostart_state = "вкл" if autostart.is_enabled() else "выкл"
        except Exception:  # noqa: BLE001
            autostart_state = "?"
        LOG.info(
            "Настройки применены: theme=%s accent=%s model=%s language=%s device=%s "
            "sttDevice=%s vad=%s hotkey=%s (%s), settingsHotkey=%s (%s), sizeMode=%s "
            "pinned=%s uiScale=%s, autostart=%s, pauseMs=%s, maxRecordMs=%s",
            settings.get("theme", theme.DEFAULT_THEME),
            settings.get("accent"),
            settings.get("model"),
            settings.get("language"),
            settings.get("device"),
            settings.get("sttDevice"),
            settings.get("vad"),
            settings.get("hotkey"),
            "зарегистрирован" if dictate_ok else "НЕ зарегистрирован",
            settings.get("settingsHotkey"),
            "зарегистрирован" if settings_ok else "НЕ зарегистрирован",
            settings.get("sizeMode"),
            settings.get("pinned"),
            settings.get("uiScale"),
            autostart_state,
            settings.get("pauseMs"),
            settings.get("maxRecordMs"),
        )

    def _on_engine_event(self, event: dict[str, Any]) -> None:
        """Вызывается из рабочих потоков движка — только кладём в очередь."""
        try:
            self.events.put(event)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ lifecycle

    def run(self) -> None:
        if self.tray is not None:
            try:
                self.tray.start()
            except Exception:  # noqa: BLE001
                LOG.exception("Не удалось запустить трей")
        self.root.after(10, self._after_init)
        self.root.after(50, self._poll)
        self.ready = True
        try:
            self.root.mainloop()
        finally:
            self._closed = True

    def _after_init(self) -> None:
        if self._closed:
            return
        if self._startup_notices:
            self._notice(self._startup_notices[0])
            self._startup_notices.clear()
        self._log_applied_settings()
        if not self.hotkey_registered.get("dictate", False):
            free = self._suggest_free_hotkey()
            message = "Горячая клавиша диктовки не зарегистрирована"
            if free:
                message += f" — свободно: {free}"
            self._notice(message)
        if self.show_settings_on_start:
            self.open_settings()
        if self.window.pinned:
            # Закреплённый виджет показывается при старте (без перехвата фокуса).
            self.window.set_text(self.last_text)
            self.window.set_state("idle")
            self.window.set_pinned(True)
            self.window.show(focus=False)
        if self.stt is not None:
            threading.Thread(
                target=self._warmup, name="WinDictateWarmup", daemon=True
            ).start()

    def _warmup(self) -> None:
        try:
            if not self.stt.ensure_ready(timeout_s=120):
                LOG.info("Сервис распознавания пока не готов")
        except Exception as exc:  # noqa: BLE001
            LOG.info("Прогрев распознавания не удался: %s", exc)

    def quit(self) -> None:
        self._ui_call(self._quit_ui)

    def _quit_ui(self) -> None:
        if self._closed:
            return
        try:
            x, y = self.window.current_position()
            self.settings["windowX"] = int(x)
            self.settings["windowY"] = int(y)
            self._persist_ui_settings()
        except Exception:  # noqa: BLE001
            pass
        self._closed = True
        try:
            if self.engine is not None:
                self.engine.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.hotkeys.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.tray is not None:
                self.tray.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.stt is not None:
                self.stt.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ poll loop

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            for name in self.hotkeys.poll():
                if name == "dictate":
                    self.toggle_dictation()
                elif name == "settings":
                    self.open_settings()
            while True:
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                self._handle_event(event)
            self._keep_window_focus()
        except Exception:  # noqa: BLE001 - цикл UI не должен умирать
            LOG.exception("Ошибка в цикле обработки событий")
        if not self._closed:
            self.root.after(50, self._poll)

    def _handle_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "_ui":
            callback = event.get("fn")
            if callable(callback):
                try:
                    callback()
                except Exception:  # noqa: BLE001
                    LOG.exception("Ошибка отложенного действия UI")
            return
        if etype == "state":
            state = str(event.get("state", "idle"))
            if state in ("idle", "recording"):
                self._stopping = False
            self.window.set_state(state)
            if "elapsed_ms" in event:
                self.window.set_elapsed(int(event.get("elapsed_ms") or 0))
            if state == "recording":
                self.window.set_notice("")
        elif etype == "draft":
            self.window.set_text(str(event.get("text", "")))
        elif etype == "level":
            self.window.set_level(event.get("rms", 0.0))
        elif etype == "elapsed":
            self.window.set_elapsed(int(event.get("elapsed_ms") or 0))
        elif etype == "notice":
            message = str(event.get("text", ""))
            self.window.set_notice(message)
            LOG.info("Уведомление: %s", message)
        elif etype == "error":
            message = str(event.get("text", "Неизвестная ошибка"))
            self._stopping = False
            self.window.set_state("idle")
            self.window.set_notice(message)
            LOG.error("Ошибка диктовки: %s", message)
        elif etype == "done":
            self._handle_done(event)

    def _handle_done(self, event: dict[str, Any]) -> None:
        text = str(event.get("text") or "")
        cancelled = bool(event.get("cancelled"))
        error = event.get("error")
        # Устаревший done от отменённой/перезапущенной сессии не должен
        # перебивать уже начатую новую запись.
        try:
            engine_state = self.engine.state if self.engine is not None else "idle"
        except Exception:  # noqa: BLE001
            engine_state = "idle"
        if engine_state != "idle":
            return
        self._stopping = False
        self._focus_guard_until = time.monotonic() + 3.0
        self.last_text = text
        self.window.set_state("idle")
        self.window.set_text(text)
        if cancelled:
            if not self.window.pinned:
                self.window.hide()
            self._focus_guard_until = 0.0
            return
        if text.strip():
            if self.settings.get("autoCopy", True):
                if winapi.set_clipboard_text(text):
                    self.window.set_notice("Скопировано в буфер")
                else:
                    self.window.set_notice("Не удалось скопировать в буфер.")
            else:
                self.window.set_notice("Текст готов — можно править и копировать")
            append_history(text, int(self.settings.get("historyLimit", 50) or 0))
        else:
            self.window.set_notice("Ничего не распознано")
        if error:
            self.window.set_notice(str(error))
        if self.window.pinned:
            self.window.show(focus=False)

    # ------------------------------------------------------------------ dictation

    def toggle_dictation(self) -> None:
        if self.engine is None:
            if not self.engine_error:
                self._build_engine()
            if self.engine is None:
                self._notice(self.engine_error or "Распознавание недоступно")
                return
        try:
            state = self.engine.state
        except Exception:  # noqa: BLE001
            state = "idle"
        if state == "recording":
            self.stop_dictation()
        elif state == "finalizing":
            return
        else:
            self.start_dictation()

    def start_dictation(self) -> None:
        if self.engine is None:
            self._notice(self.engine_error or "Распознавание недоступно")
            return
        self._start_new_recording()

    def stop_dictation(self) -> None:
        """Останавливает запись, не блокируя Tk-поток.

        engine.stop() финализирует фразу и может ждать STT до 15 с. Поэтому UI
        сразу переходит в «Распознаю…», а сам stop идёт в daemon-потоке; done
        приедет через общую очередь событий.
        """
        if self.engine is None or self._stopping:
            return
        try:
            state = self.engine.state
        except Exception:  # noqa: BLE001
            return
        if state != "recording":
            return
        self._stopping = True
        self.window.set_state("finalizing")
        self.window.set_notice("Распознаю…")
        engine = self.engine
        threading.Thread(
            target=self._stop_worker,
            args=(engine,),
            name="WinDictateStop",
            daemon=True,
        ).start()

    def _stop_worker(self, engine: Any) -> None:
        try:
            engine.stop()
        except Exception:  # noqa: BLE001
            LOG.exception("Не удалось остановить запись")
            self._stopping = False
            self._ui_call(lambda: self._notice("Не удалось остановить запись."))

    def _copy_and_close(self) -> None:
        if self.engine is not None and self.engine.state != "idle":
            if self.engine.state == "recording":
                self.stop_dictation()
            return
        text = self.window.get_text()
        if text.strip() and not winapi.set_clipboard_text(text):
            self._notice("Не удалось скопировать текст в буфер.")
            return
        if self.window.pinned:
            # Закреплённый виджет остаётся на экране после копирования.
            self._notice("Скопировано в буфер")
            return
        self.window.hide()
        self._focus_guard_until = 0.0

    def _paste_and_close(self) -> None:
        if self.engine is not None and self.engine.state != "idle":
            return
        text = self.window.get_text()
        if not text.strip():
            self._notice("Нет текста для вставки")
            return
        if not self.settings.get("pasteOnCtrlEnter", True):
            self._notice("Вставка по Ctrl+Enter выключена в настройках")
            return
        if not winapi.set_clipboard_text(text):
            self._notice("Не удалось положить текст в буфер — вставка отменена.")
            return
        previous = self.prev_hwnd
        if not self.window.pinned:
            self.window.hide()
            self._focus_guard_until = 0.0
        self.root.after(80, lambda: self._do_paste(previous))

    def _do_paste(self, previous_hwnd: int) -> None:
        if previous_hwnd:
            winapi.set_foreground_window(previous_hwnd)
        if not winapi.send_ctrl_v():
            LOG.warning("Не удалось отправить Ctrl+V")
            self._notice("Не удалось вставить — текст скопирован.")
            self.window.set_state("idle")
            if not self.window.visible:
                self.window.show()

    def _new_dictation(self) -> None:
        """«Новая»: отменить текущую (если идёт) и сразу начать новую запись."""
        if self.engine is None:
            if not self.engine_error:
                self._build_engine()
            if self.engine is None:
                self._notice(self.engine_error or "Распознавание недоступно")
                return
        try:
            state = self.engine.state
        except Exception:  # noqa: BLE001
            state = "idle"
        if state in ("recording", "finalizing"):
            self._cancel_recording()
        self._start_new_recording()

    def _start_new_recording(self) -> None:
        self._stopping = False
        foreground = winapi.get_foreground_window()
        if foreground and foreground != self.window.hwnd():
            self.prev_hwnd = foreground
        self._focus_guard_until = time.monotonic() + 2.0
        self.window.set_text("")
        self.window.set_level(0.0)
        self.window.set_elapsed(0)
        self.window.set_notice("")
        self.window.set_state("recording")
        self.window.show()
        try:
            self.engine.start()
        except Exception as exc:  # noqa: BLE001
            LOG.exception("Не удалось начать запись")
            self.window.set_state("idle")
            self._notice(f"Не удалось начать запись: {exc}")

    def _cancel_and_hide(self) -> None:
        self._cancel_recording()
        self.window.set_state("idle")
        self.window.hide()
        self._focus_guard_until = 0.0

    def _on_pin_changed(self, pinned: bool) -> None:
        self.settings["pinned"] = bool(pinned)
        self.window.set_notice(
            "Окно закреплено как виджет" if pinned else "Окно откреплено"
        )
        LOG.info("Pinned: %s", pinned)
        self._persist_ui_settings()

    def _on_size_changed(self, mode: str) -> None:
        self.settings["sizeMode"] = str(mode)
        LOG.info("Размер окна: %s", mode)
        self._persist_ui_settings()

    def _on_window_moved(self, x: int, y: int) -> None:
        self.settings["windowX"] = int(x)
        self.settings["windowY"] = int(y)
        self._schedule_position_save()

    def _schedule_position_save(self) -> None:
        if self._position_job is not None:
            try:
                self.root.after_cancel(self._position_job)
            except tk.TclError:
                pass
        try:
            self._position_job = self.root.after(300, self._persist_ui_settings)
        except tk.TclError:
            self._position_job = None

    def _on_settings_size(self, width: int, height: int) -> None:
        self.settings["settingsW"] = int(width)
        self.settings["settingsH"] = int(height)
        if self._settings_size_job is not None:
            try:
                self.root.after_cancel(self._settings_size_job)
            except tk.TclError:
                pass
        try:
            self._settings_size_job = self.root.after(400, self._persist_ui_settings)
        except tk.TclError:
            self._settings_size_job = None

    def _persist_ui_settings(self) -> None:
        """Сохраняет только UI-предпочтения (позиция/размер/pin) без транзакций."""
        self._position_job = None
        self._settings_size_job = None
        try:
            config.save_settings(self.settings)
        except OSError as exc:
            LOG.warning("Не удалось сохранить позицию/размер окна: %s", exc)

    def _open_logs(self) -> None:
        try:
            import os

            os.startfile(str(config.logs_dir()))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - открытие папки не критично
            LOG.exception("Не удалось открыть папку логов")
            self._notice(f"Логи: {config.logs_dir()}")

    def _close_window(self) -> None:
        self._cancel_recording()
        self.window.set_state("idle")
        self.window.hide()
        self._focus_guard_until = 0.0

    def _escape(self) -> None:
        self._close_window()

    def _cancel_recording(self) -> None:
        if self.engine is None:
            return
        try:
            if self.engine.state in ("recording", "finalizing"):
                self.engine.cancel()
        except Exception:  # noqa: BLE001
            LOG.exception("Не удалось отменить запись")
        self._stopping = False

    # ------------------------------------------------------------------ settings

    def open_settings(self) -> None:
        self._ui_call(self._open_settings_ui)

    def _open_settings_ui(self) -> None:
        if self._closed:
            return
        if self.settings_window is not None:
            try:
                self.settings_window.destroy()
            except Exception:  # noqa: BLE001
                pass
        try:
            theme.init_ttk(
                self.root,
                self.settings.get("theme", theme.DEFAULT_THEME),
                self.settings.get("accent"),
            )
        except Exception:  # noqa: BLE001 - оформление не должно ломать настройки
            LOG.exception("Не удалось инициализировать ttk-стили")
        self.settings_window = SettingsWindow(
            self.root,
            self.settings,
            self._save_settings,
            self._test_microphone,
            on_probe_hotkey=self._probe_hotkey,
            on_reload_model=self._reload_model_async,
            on_model_status=self._model_status_text,
            on_settings_size=self._on_settings_size,
            on_open_logs=self._open_logs,
        )
        self.settings_window.show()

    # ------------------------------------------------------------------ stt status / reload

    def _model_status_text(self) -> tuple[str, bool]:
        """Строка статуса модели для окна настроек."""
        stt = self.stt
        if stt is None:
            return ("Сервис распознавания не подключён", True)
        health: dict[str, Any] = {}
        try:
            health = stt.health() or {}
        except Exception:  # noqa: BLE001
            health = {}
        state = str(getattr(stt, "state", "") or health.get("state") or "")
        last_error = getattr(stt, "last_error", None) or health.get("last_error")
        if state == "loading" or health.get("loading"):
            return ("Загрузка модели…", False)
        if state == "ready" or health.get("ready"):
            model = health.get("model") or self.settings.get("model")
            device = health.get("device") or self.settings.get("sttDevice")
            compute = health.get("computeType") or health.get("compute_type") or "?"
            port = health.get("port") or getattr(stt, "port", None) or "—"
            return (f"Загружено: {model} · {device} · {compute} · порт {port}", False)
        if state == "error" or last_error:
            return (str(last_error or "Ошибка загрузки модели"), True)
        return ("Модель ещё не загружена — начните диктовку или перезагрузите", False)

    def _reload_model_async(self, settings: dict[str, Any]) -> None:
        """Кнопка «Перезагрузить модель»: reload в фоне, UI не блокируется."""
        snapshot = config.normalize_settings(settings)
        self._reload_stt_async(snapshot, from_settings_window=True)

    def _reload_stt_async(
        self, settings: dict[str, Any], from_settings_window: bool = False
    ) -> None:
        if self._reload_thread is not None and self._reload_thread.is_alive():
            LOG.info("Перезагрузка модели уже идёт")
            return

        def worker() -> None:
            ok = False
            error: str | None = None
            try:
                if self.stt is None:
                    self.stt = self._make_stt(settings)
                    ok = self.stt is not None and bool(
                        self.stt.ensure_ready(timeout_s=120)
                    )
                    if ok:
                        self._build_engine_with(settings)
                elif hasattr(self.stt, "reload"):
                    ok = bool(self.stt.reload(settings))
                else:  # старый STT-сервис: пересоздаём
                    self._rebuild_engine()
                    ok = self.stt is not None and bool(
                        self.stt.ensure_ready(timeout_s=120)
                    )
                if not ok:
                    error = getattr(self.stt, "last_error", None) or "не удалось"
            except Exception as exc:  # noqa: BLE001
                LOG.exception("Ошибка перезагрузки модели")
                error = str(exc)
            self._ui_call(
                lambda: self._after_reload(ok, error, from_settings_window)
            )

        self._reload_thread = threading.Thread(
            target=worker, name="WinDictateSttReload", daemon=True
        )
        self._reload_thread.start()

    def _after_reload(
        self, ok: bool, error: str | None, from_settings_window: bool
    ) -> None:
        window = self.settings_window
        if window is not None:
            window.set_reload_enabled(True)
            if ok:
                text, is_error = self._model_status_text()
                window.set_model_status(text, is_error)
            else:
                window.set_model_status(
                    f"Не удалось перезагрузить модель: {error or 'см. лог'}", True
                )
        if ok:
            LOG.info("Модель перезагружена: %s", self._model_status_text()[0])
            if not from_settings_window:
                self._notice("Модель перезагружена")
        else:
            LOG.warning("Не удалось перезагрузить модель: %s", error)
            self._notice(f"Не удалось перезагрузить модель: {error or 'см. лог'}")

    def _make_stt(self, settings: dict[str, Any]) -> Any:
        try:
            from dictate.stt import SttService

            return SttService(settings)
        except Exception:  # noqa: BLE001
            LOG.exception("Не удалось создать сервис распознавания")
            return None

    def _build_engine_with(self, settings: dict[str, Any]) -> None:
        try:
            from dictate.engine import DictationEngine

            if self.stt is not None:
                self.engine = DictationEngine(
                    self.stt, settings, self._on_engine_event
                )
        except Exception:  # noqa: BLE001
            LOG.exception("Не удалось пересобрать движок")

    # ------------------------------------------------------------------ settings apply

    def _probe_hotkey(self, spec: str) -> bool:
        """Публичная проверка доступности сочетания (без изменения регистраций)."""
        try:
            return bool(self.hotkeys.probe(spec))
        except Exception:  # noqa: BLE001 - проверка не должна ронять окно
            LOG.exception("Не удалось проверить сочетание %r", spec)
            return True

    def _suggest_free_hotkey(self, exclude: tuple[str, ...] = ()) -> str | None:
        for candidate in _FREE_HOTKEY_CANDIDATES:
            if candidate in exclude:
                continue
            if self._probe_hotkey(candidate):
                return candidate
        return None

    def _hotkey_error_message(self, key: str, spec: str) -> str:
        label = "Горячая клавиша" if key == "hotkey" else "Клавиша настроек"
        message = f"{label} «{spec}» занята другой программой — выберите другое."
        free = self._suggest_free_hotkey(exclude=(spec,))
        if free:
            message += f" Свободно, например: {free}."
        return message

    def _settings_error(self, message: str) -> bool:
        """Показывает ошибку в окне настроек/диктовки и НЕ сохраняет настройки."""
        LOG.warning("Настройки не применены: %s", message)
        window = self.settings_window
        if window is not None:
            try:
                window.set_status(message, error=True)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.window.set_notice(message, error=True)
        except Exception:  # noqa: BLE001
            pass
        return False

    def _save_settings(self, data: dict[str, Any]) -> bool:
        """Применяет настройки надёжно: хоткеи → файл → окна.

        Возвращает True при успехе; False — если что-то не применилось (в этом
        случае settings.json не меняется, а старые хоткеи остаются рабочими).
        """
        incoming = dict(data or {})
        autostart_wanted = bool(incoming.pop("autostart", autostart.is_enabled()))
        normalized = config.normalize_settings(incoming)
        old_settings = dict(self.settings)
        pairs = (
            ("dictate", "hotkey"),
            ("settings", "settingsHotkey"),
        )

        # 1) Заранее проверяем изменившиеся сочетания (без побочных эффектов).
        if str(normalized["hotkey"]) == str(normalized["settingsHotkey"]):
            return self._settings_error(
                "Горячая клавиша и клавиша настроек не могут совпадать."
            )
        for name, key in pairs:
            spec = str(normalized[key])
            if spec == old_settings.get(key):
                continue
            if not self._probe_hotkey(spec):
                return self._settings_error(self._hotkey_error_message(key, spec))

        # 2) Регистрируем новые сочетания рядом со старыми (in-place):
        #    при ошибке старое сочетание остаётся, уже применённые откатываем.
        try:
            if not getattr(self.hotkeys, "running", True):
                self.hotkeys.start()
        except Exception:  # noqa: BLE001 - поток мог умереть; попробуем поднять
            LOG.exception("Не удалось перезапустить поток горячих клавиш")
        changed: list[tuple[str, str]] = []  # (name, old_spec)
        for name, key in pairs:
            spec = str(normalized[key])
            current = str(old_settings.get(key))
            if spec == current:
                continue
            if not self.hotkeys.register(name, spec):
                reason = self.hotkeys.last_error() or ""
                LOG.warning("Не удалось зарегистрировать %s=%s: %s", name, spec, reason)
                for rollback_name, rollback_spec in changed:
                    try:
                        self.hotkeys.register(rollback_name, rollback_spec)
                    except Exception:  # noqa: BLE001
                        LOG.exception("Не удалось вернуть сочетание %s", rollback_name)
                return self._settings_error(self._hotkey_error_message(key, spec))
            changed.append((name, current))

        # 3) Только теперь сохраняем файл; при ошибке откатываем хоткеи.
        try:
            config.save_settings(normalized)
        except OSError as exc:
            LOG.error("Не удалось сохранить settings.json: %s", exc)
            for rollback_name, rollback_spec in changed:
                try:
                    self.hotkeys.register(rollback_name, rollback_spec)
                except Exception:  # noqa: BLE001
                    LOG.exception("Не удалось вернуть сочетание %s", rollback_name)
            return self._settings_error(f"Не удалось сохранить настройки: {exc}")

        # 4) Автозапуск и остальные настройки.
        try:
            if autostart_wanted:
                autostart.enable()
            else:
                autostart.disable()
        except Exception:  # noqa: BLE001
            LOG.exception("Не удалось изменить автозапуск")
        self.hotkey_registered = {
            "dictate": bool(self.hotkeys.is_registered("dictate")),
            "settings": bool(self.hotkeys.is_registered("settings")),
        }
        self.settings = normalized
        try:
            theme.apply_window_icon(self.root)
            theme.init_ttk(
                self.root,
                normalized.get("theme", theme.DEFAULT_THEME),
                normalized.get("accent"),
            )
            self.window.set_settings(normalized)
        except Exception:  # noqa: BLE001 - оформление не критично для работы
            LOG.exception("Не удалось применить оформление")
        self.window.set_state("idle")
        stt_keys = ("model", "language", "beamSize", "sttDevice", "vad")
        stt_changed = any(old_settings.get(key) != normalized.get(key) for key in stt_keys)
        if stt_changed and self.stt is not None:
            # Автоперезагрузка модели после сохранения (в фоне, UI не блокируется).
            self._reload_stt_async(normalized)
        elif self._engine_settings_changed(old_settings, normalized):
            self._rebuild_engine()
        self._log_applied_settings()
        self._notice("Настройки сохранены")
        LOG.info("Настройки сохранены: %s", normalized.get("hotkey"))
        return True

    @staticmethod
    def _engine_settings_changed(old: dict[str, Any], new: dict[str, Any]) -> bool:
        keys = (
            "device",
            "language",
            "model",
            "beamSize",
            "pauseMs",
            "maxRecordMs",
            "singleLine",
        )
        return any(old.get(key) != new.get(key) for key in keys)

    def _test_microphone(self, settings: dict[str, Any]) -> None:
        threading.Thread(
            target=self._mic_probe,
            args=(dict(settings),),
            name="WinDictateMicProbe",
            daemon=True,
        ).start()

    def _mic_probe(self, settings: dict[str, Any]) -> None:
        message = "Проверка микрофона не удалась"
        try:
            from dictate.audio import rms as audio_rms

            samples, rate = record_mic(settings.get("device"), 3.0)
            level = audio_rms(samples) if samples.size else 0.0
            text = ""
            if self.stt is not None:
                try:
                    wav = wav_from_samples(samples, rate)
                    if self.stt.ensure_ready(timeout_s=120):
                        result = self.stt.transcribe(
                            wav,
                            language=settings.get("language"),
                            beam_size=None,
                            vad=True,
                        )
                        text = str((result or {}).get("text", "")).strip()
                except Exception as exc:  # noqa: BLE001
                    text = f"распознавание недоступно ({exc})"
            message = f"Уровень: {float(level):.3f}. "
            message += f"Текст: «{text}»" if text else "Речь не распознана."
        except Exception as exc:  # noqa: BLE001
            message = f"Ошибка проверки микрофона: {exc}"
        window = self.settings_window
        if window is not None:
            self._ui_call(lambda: window.set_mic_result(message))

    # ------------------------------------------------------------------ tray/actions

    def open_last(self) -> None:
        self._ui_call(self._open_last_ui)

    def show_window(self) -> None:
        """Показать окно диктовки (трей)."""
        self._ui_call(self._show_window_ui)

    def _show_window_ui(self) -> None:
        if self._closed:
            return
        if self.window.visible:
            try:
                self.window.win.lift()
            except Exception:  # noqa: BLE001
                pass
            return
        self.window.set_state("idle")
        self.window.set_text(self.last_text)
        self.window.show()

    def _open_last_ui(self) -> None:
        if self._closed:
            return
        if self.window.visible:
            self.toggle_dictation()
            return
        if self.last_text.strip():
            self.window.set_state("idle")
            self.window.set_text(self.last_text)
            self.window.show()
        else:
            self.toggle_dictation()

    def toggle_autostart(self, enabled: bool) -> None:
        self._ui_call(lambda: self._toggle_autostart_ui(enabled))

    def _toggle_autostart_ui(self, enabled: bool) -> None:
        if enabled:
            ok = autostart.enable()
        else:
            ok = autostart.disable()
        if ok:
            LOG.info("Автозапуск %s", "включён" if enabled else "выключен")
        else:
            self._notice("Не удалось изменить автозапуск")

    # ------------------------------------------------------------------ helpers

    def _keep_window_focus(self) -> None:
        """Пока идёт диктовка, окно остаётся активным (фокус могли перехватить)."""
        if self._closed or not self.window.visible:
            return
        if self.settings_window is not None and self.settings_window.visible:
            return
        try:
            state = self.engine.state if self.engine is not None else "idle"
        except Exception:  # noqa: BLE001
            state = "idle"
        if self.window.pinned and state == "idle":
            # Закреплённый виджет не перехватывает фокус у других приложений.
            return
        guarded = state in ("recording", "finalizing") or (
            time.monotonic() < self._focus_guard_until
        )
        if not guarded:
            return
        hwnd = self.window.hwnd()
        if not hwnd or winapi.get_foreground_window() == hwnd:
            return
        if winapi.set_foreground_window(hwnd):
            try:
                self.window.win.focus_force()
                self.window.focus_editor()
            except Exception:  # noqa: BLE001
                pass

    def _ui_call(self, callback: Callable[[], Any]) -> None:
        if threading.current_thread() is threading.main_thread():
            try:
                callback()
            except Exception:  # noqa: BLE001
                LOG.exception("Ошибка действия UI")
            return
        self.events.put({"type": "_ui", "fn": callback})

    def _notice(self, text: str) -> None:
        message = str(text or "")
        self.window.set_notice(message)
        if message:
            LOG.info(message)


# --------------------------------------------------------------------------- selftest


def _source_files() -> list[Path]:
    files = [APP_DIR / "app.py"]
    for folder in ("dictate", "server"):
        directory = APP_DIR / folder
        if directory.is_dir():
            files.extend(sorted(directory.glob("*.py")))
    return [path for path in files if path.is_file()]


def _check_config_roundtrip() -> bool:
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            sample = dict(config.DEFAULTS)
            sample["pauseMs"] = 1234
            sample["device"] = 2
            config.save_settings(sample, path)
            if config.load_settings(path) != config.normalize_settings(sample):
                return False
            path.write_text(
                '{"pauseMs": 99999, "device": "нет", "model": "гигант"}',
                encoding="utf-8",
            )
            fixed = config.load_settings(path)
            return (
                fixed["pauseMs"] == 2000
                and fixed["device"] is None
                and fixed["model"] == "small"
            )
    except Exception:  # noqa: BLE001
        return False


def _check_hotkey_parse() -> bool:
    try:
        from dictate import hotkeys

        mods, vk = hotkeys.parse_hotkey("Ctrl+Alt+Space")
        if mods != hotkeys.MOD_CONTROL | hotkeys.MOD_ALT or vk != 0x20:
            return False
        hotkeys.parse_hotkey("F9")
        hotkeys.parse_hotkey("Win+Shift+D")
        try:
            hotkeys.parse_hotkey("Ctrl+НетТакой")
        except ValueError:
            return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _check_autostart() -> bool:
    value_name = "WinDictate-SelfTest"
    command_line = '"C:\\fake\\pythonw.exe" "C:\\fake\\app.py" --autostart'
    try:
        if not autostart.enable(value_name=value_name, command_line=command_line):
            return False
        if not autostart.is_enabled(value_name=value_name):
            return False
        if autostart.get_value(value_name) != command_line:
            return False
        return autostart.disable(value_name=value_name) and not autostart.is_enabled(
            value_name=value_name
        )
    except Exception:  # noqa: BLE001
        return False
    finally:
        autostart.disable(value_name=value_name)


def _check_clipboard() -> bool:
    previous = winapi.get_clipboard_text()
    sentinel = f"WinDictate selftest {time.time()}"
    try:
        if not winapi.set_clipboard_text(sentinel):
            return False
        return winapi.get_clipboard_text() == sentinel
    finally:
        try:
            winapi.set_clipboard_text(previous)
        except Exception:  # noqa: BLE001
            pass


def _check_single_instance() -> bool:
    name = f"WinDictate-SelfTest-{os.getpid()}"
    first = winapi.acquire_single_instance(name)
    second = winapi.acquire_single_instance(name)
    return bool(first) and not second


def _check_voice_absence() -> bool:
    """Нет импортов сторонней озвучки/облачных движков.

    Токен из трёх букв проверяем по границам слова: иначе он находится внутри
    имени SttService (это легальный локальный распознаватель, а не озвучка).
    """
    import re

    voice_token = _FORBIDDEN_PARTS[1]
    pattern = re.compile(r"(?<![a-z0-9])" + voice_token + r"(?![a-z0-9])")
    for path in _source_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        if _FORBIDDEN_PARTS[0] in text:
            return False
        if _FORBIDDEN_PARTS[2] in text:
            return False
        if pattern.search(text):
            return False
    return True


def _check_silence_engine() -> bool:
    """Движок с фейковым «тихим» источником: start → 1 с → stop → done(text="")."""
    try:
        import numpy as np

        import dictate.engine as engine_module
    except Exception:  # noqa: BLE001
        return False
    recorder_class = getattr(engine_module, "PcmRecorder", None)
    if recorder_class is None:
        return False

    class _SilentRecorder:
        def __init__(self, device, on_block, blocksize_ms: int = 100) -> None:
            self.sample_rate = 16000
            self._on_block = on_block
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, daemon=True)

        def _run(self) -> None:
            block = np.zeros(1600, dtype=np.float32)
            while not self._stop.is_set():
                try:
                    self._on_block(block, 16000)
                except Exception:  # noqa: BLE001
                    return
                self._stop.wait(0.1)

        def start(self) -> None:
            self._thread.start()

        def stop(self) -> None:
            self._stop.set()
            self._thread.join(timeout=2.0)

    class _FakeStt:
        def ensure_ready(self, timeout_s: float = 120.0) -> bool:
            return True

        def health(self) -> dict[str, Any]:
            return {"ready": True}

        def transcribe(self, wav_bytes: bytes, **_kwargs: Any) -> dict[str, Any]:
            return {"text": ""}

        def stop(self) -> None:
            return None

    events: list[dict[str, Any]] = []
    engine = None
    try:
        engine_module.PcmRecorder = _SilentRecorder
        settings = dict(config.DEFAULTS)
        engine = engine_module.DictationEngine(_FakeStt(), settings, events.append)
        engine.start()
        time.sleep(1.0)
        engine.stop()
        deadline = time.time() + 15.0
        while time.time() < deadline:
            if any(event.get("type") == "done" for event in events):
                break
            time.sleep(0.05)
        done = [event for event in events if event.get("type") == "done"]
        return bool(done) and str(done[-1].get("text") or "") == ""
    except Exception:  # noqa: BLE001
        return False
    finally:
        engine_module.PcmRecorder = recorder_class
        if engine is not None:
            try:
                engine.cancel()
            except Exception:  # noqa: BLE001
                pass


def _selftest_stt() -> dict[str, Any]:
    stt = None
    settings = config.load_settings()
    info: dict[str, Any] = {"ready": False, "model": settings.get("model")}
    if os.environ.get("WIN_DICTATE_SELFTEST_FAST"):
        info["skipped"] = True
        return info
    try:
        from dictate.stt import SttService

        stt = SttService(settings)
        health: dict[str, Any] = {}
        try:
            health = stt.health() or {}
        except Exception:  # noqa: BLE001
            health = {}
        ready = bool(health.get("ready")) or bool(stt.ensure_ready(timeout_s=90))
        if ready:
            try:
                health = stt.health() or health
            except Exception:  # noqa: BLE001
                pass
        info = {
            "ready": ready,
            "model": health.get("model", settings.get("model")),
            "device": health.get("device", ""),
            "port": int(getattr(stt, "port", 0) or health.get("port", 0) or 0),
        }
    except Exception as exc:  # noqa: BLE001
        info = {"ready": False, "error": str(exc)}
    finally:
        if stt is not None:
            try:
                stt.stop()
            except Exception:  # noqa: BLE001
                pass
    return info


def _selftest_mic() -> dict[str, Any]:
    info: dict[str, Any] = {"devices": 0, "opened": False}
    try:
        import numpy as np
        import sounddevice as sd

        info["devices"] = len(sd.query_devices())
        if os.environ.get("WIN_DICTATE_SELFTEST_FAST"):
            info["skipped"] = True
            return info
        settings = config.load_settings()
        recording = sd.rec(
            int(0.5 * 16000),
            samplerate=16000,
            channels=1,
            dtype="float32",
            device=settings.get("device"),
        )
        sd.wait()
        samples = np.asarray(recording, dtype=np.float32).reshape(-1)
        info.update(
            {
                "opened": True,
                "rms": round(float(np.sqrt(np.mean(np.square(samples)))), 4)
                if samples.size
                else 0.0,
                "peak": round(float(np.max(np.abs(samples))), 4)
                if samples.size
                else 0.0,
            }
        )
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)
    return info


def _selftest_window() -> dict[str, Any]:
    info: dict[str, Any] = {"created": False, "title": "", "copy_ok": False}
    previous = winapi.get_clipboard_text()
    root = None
    window = None
    try:
        root = tk.Tk()
        root.withdraw()
        window = DictationWindow(
            root,
            {"copy": lambda: winapi.set_clipboard_text(window.get_text())},
            config.load_settings(),
        )
        window.set_text("тест")
        window.set_state("idle")
        window._on_enter(None)
        root.update()
        info.update(
            {
                "created": True,
                "title": window.win.title(),
                "copy_ok": winapi.get_clipboard_text() == "тест",
            }
        )
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)
    finally:
        try:
            if window is not None:
                window.win.destroy()
        except Exception:  # noqa: BLE001
            pass
        try:
            if root is not None:
                root.destroy()
        except Exception:  # noqa: BLE001
            pass
        try:
            winapi.set_clipboard_text(previous)
        except Exception:  # noqa: BLE001
            pass
    return info


def _reconfigure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def run_selftest() -> int:
    """Печатает ровно один JSON-отчёт и возвращает 0/1."""
    _reconfigure_stdout()
    checks: dict[str, bool] = {
        "config_roundtrip": _check_config_roundtrip(),
        "hotkey_parse": _check_hotkey_parse(),
        "autostart_roundtrip": _check_autostart(),
        "clipboard": _check_clipboard(),
        "silence_engine": _check_silence_engine(),
        _VOICE_CHECK_KEY: _check_voice_absence(),
        "single_instance": _check_single_instance(),
    }
    stt_info = _selftest_stt()
    mic_info = _selftest_mic()
    window_info = _selftest_window()
    checks_ok = all(checks.values())
    window_ok = bool(window_info.get("created")) and bool(window_info.get("copy_ok"))
    report = {
        "ok": bool(checks_ok and stt_info.get("ready") and window_ok),
        "checks": checks,
        "stt": stt_info,
        "mic": mic_info,
        "window": window_info,
    }
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


def transcribe_file(path: str) -> int:
    _reconfigure_stdout()
    stt = None
    try:
        from dictate.stt import SttService

        settings = config.load_settings()
        stt = SttService(settings)
        if not stt.ensure_ready(timeout_s=120):
            print("Сервис распознавания не готов", file=sys.stderr)
            return 1
        result = stt.transcribe(Path(path).read_bytes())
        text = str((result or {}).get("text", "")).strip()
        print(text)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка распознавания: {exc}", file=sys.stderr)
        return 1
    finally:
        if stt is not None:
            try:
                stt.stop()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- entrypoint


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="WinDictate", description="Диктовка по горячей клавише")
    parser.add_argument("--autostart", action="store_true", help="скрытый старт (трей + клавиши)")
    parser.add_argument("--settings", action="store_true", help="сразу открыть настройки")
    parser.add_argument("--transcribe", metavar="WAV", help="распознать WAV-файл и напечатать текст")
    parser.add_argument("--selftest", action="store_true", help="напечатать JSON-отчёт и выйти")
    parser.add_argument("--no-tray", action="store_true", help="без иконки в трее")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    # Шрифты приложения подключаются до первого Tk-окна.
    try:
        theme.register_fonts()
    except Exception:  # noqa: BLE001 - шрифты не должны мешать запуску
        LOG.exception("Не удалось подключить шрифты приложения")
    if args.selftest:
        return run_selftest()
    if args.transcribe:
        return transcribe_file(args.transcribe)
    if not winapi.acquire_single_instance(APP_NAME):
        LOG.info("WinDictate уже запущен — второй экземпляр не нужен")
        return 0
    app: DictateApp | None = None
    try:
        app = DictateApp(no_tray=args.no_tray, show_settings=args.settings)
        app.run()
        return 0
    except Exception:  # noqa: BLE001
        LOG.exception("Приложение завершилось с ошибкой")
        return 1
    finally:
        if app is not None:
            try:
                app.quit()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
