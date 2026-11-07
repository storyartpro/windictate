"""Headless-тесты оболочки WinDictate.

Проверяются настройки, парсер/менеджер горячих клавиш, автозапуск, буфер
обмена, окна Tk (создаются и update(), без mainloop), а также режим
--selftest. Реальный микрофон и faster-whisper не используются.

Запуск: python tests/test_shell.py
Ожидаемо: строка «PASS n/n» и код возврата 0.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import traceback
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dictate import autostart, config, hotkeys, theme, winapi  # noqa: E402
from dictate import ui as ui_module  # noqa: E402
from dictate.ui import (  # noqa: E402
    SETTINGS_TITLE,
    THEME_LABELS,
    WINDOW_TITLE,
    BrandSlider,
    DictationWindow,
    SettingsWindow,
    format_elapsed,
    icons_available,
    spec_from_tk_event,
)

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
_ROOT = None


def check(name: str, function) -> None:
    try:
        function()
    except Exception:  # noqa: BLE001 - тест-раннер печатает все ошибки
        FAILED.append((name, traceback.format_exc()))
    else:
        PASSED.append(name)


def get_root():
    global _ROOT
    if _ROOT is None:
        import tkinter as tk

        _ROOT = tk.Tk()
        _ROOT.withdraw()
        _ROOT.title("WinDictate tests")
    return _ROOT


def make_isolated_app(app_module):
    """DictateApp на дефолтных настройках — без чтения/записи реального settings.json."""
    with mock.patch.object(
        app_module.config, "load_settings", lambda path=None: dict(config.DEFAULTS)
    ):
        return app_module.DictateApp(no_tray=True)


# --------------------------------------------------------------------------- config


def test_config_defaults() -> None:
    defaults = config.DEFAULTS
    required = {
        "version",
        "device",
        "language",
        "model",
        "beamSize",
        "pauseMs",
        "maxRecordMs",
        "hotkey",
        "settingsHotkey",
        "autoCopy",
        "pasteOnCtrlEnter",
        "showElapsed",
        "singleLine",
        "historyLimit",
        "uiScale",
        "theme",
    }
    assert required <= set(defaults), f"нет ключей: {required - set(defaults)}"
    assert defaults["hotkey"] == "Ctrl+Alt+Space"
    assert defaults["maxRecordMs"] == 600000
    assert defaults["theme"] == "paper"
    assert defaults["accent"] == "#C0603C"
    assert set(config.THEMES) == {"paper", "scene"}
    assert config.DEFAULT_ACCENT == "#C0603C"


def test_config_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        sample = dict(config.DEFAULTS)
        sample.update({"pauseMs": 1234, "device": 3, "model": "medium", "uiScale": 1.3})
        config.save_settings(sample, path)
        loaded = config.load_settings(path)
        assert loaded == config.normalize_settings(sample), "roundtrip не совпал"
        assert loaded["pauseMs"] == 1234 and loaded["device"] == 3


def test_config_partial_merge() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text('{"pauseMs": 500}', encoding="utf-8")
        loaded = config.load_settings(path)
        assert loaded["pauseMs"] == 500
        assert loaded["hotkey"] == config.DEFAULTS["hotkey"]
        assert loaded["historyLimit"] == config.DEFAULTS["historyLimit"]
        assert set(loaded) == set(config.DEFAULTS)


def test_config_repairs_bad_values() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "pauseMs": 99999,
                    "maxRecordMs": 1,
                    "beamSize": "много",
                    "device": "микрофон",
                    "uiScale": 99,
                    "historyLimit": -5,
                    "language": "klingon",
                    "model": "gigant",
                    "autoCopy": "нет",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        fixed = config.load_settings(path)
        assert fixed["pauseMs"] == 2000
        assert fixed["maxRecordMs"] == 10000
        assert fixed["beamSize"] == 5
        assert fixed["device"] is None
        assert fixed["uiScale"] == 1.6
        assert fixed["historyLimit"] == 0
        assert fixed["language"] == "ru"
        assert fixed["model"] == "small"
        assert fixed["autoCopy"] is False
    # Битый JSON не роняет загрузку.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text("{это не json", encoding="utf-8")
        assert config.load_settings(path) == config.DEFAULTS


def test_config_atomic_save() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        config.save_settings(config.DEFAULTS, path)
        config.save_settings(dict(config.DEFAULTS, pauseMs=900), path)
        leftovers = [item.name for item in Path(tmp).iterdir() if item.name != "settings.json"]
        assert not leftovers, f"остались временные файлы: {leftovers}"
        assert json.loads(path.read_text(encoding="utf-8"))["pauseMs"] == 900


def test_config_paths() -> None:
    path = config.settings_path()
    assert str(path).endswith("WinDictate\\settings.json"), str(path)
    assert str(config.history_path()).endswith("history.json")
    assert str(config.logs_dir()).endswith("logs")


# --------------------------------------------------------------------------- hotkeys


def test_hotkey_parse_valid() -> None:
    mods, vk = hotkeys.parse_hotkey("Ctrl+Alt+Space")
    assert mods == hotkeys.MOD_CONTROL | hotkeys.MOD_ALT
    assert vk == 0x20
    assert hotkeys.parse_hotkey("Win+Shift+D")[0] == hotkeys.MOD_WIN | hotkeys.MOD_SHIFT
    assert hotkeys.parse_hotkey("F9")[1] == 0x78
    assert hotkeys.parse_hotkey("ctrl + alt + s")[1] == ord("S")
    assert hotkeys.parse_hotkey("Ctrl+Enter")[1] == 0x0D
    assert hotkeys.parse_hotkey("Ctrl+,")[1] == 0xBC
    assert hotkeys.parse_hotkey("Alt+PageDown")[1] == 0x22
    assert hotkeys.parse_hotkey("Ctrl+Left")[1] == 0x25
    assert hotkeys.format_hotkey("ctrl+alt+space") == "Ctrl+Alt+Space"


def test_hotkey_parse_invalid() -> None:
    for spec in ("", "   ", "Ctrl+", "+Alt+S", "Ctrl+Alt+НетТакой", "Ctrl+S+Alt"):
        try:
            hotkeys.parse_hotkey(spec)
        except ValueError:
            continue
        raise AssertionError(f"spec принят, но должен быть отклонён: {spec!r}")


def test_hotkey_register_poll_stop() -> None:
    manager = hotkeys.HotkeyManager()
    spec = None
    for candidate in (
        "Ctrl+Alt+Shift+F12",
        "Ctrl+Alt+Shift+F11",
        "Ctrl+Alt+Shift+F10",
        "Ctrl+Alt+Shift+F9",
        "Ctrl+Alt+Shift+F8",
    ):
        if manager.register("dictate", candidate):
            spec = candidate
            break
    assert spec is not None, "не удалось занять ни одно тестовое сочетание"
    manager.start()
    try:
        assert manager.running, "поток горячих клавиш не запустился"
        assert manager.last_error() is None, manager.last_error()
        hotkey_id = manager._name_to_id.get("dictate")
        assert hotkey_id is not None, "сочетание не зарегистрировано в окне"
        manager._dispatch_id(int(hotkey_id))
        assert manager.poll() == ["dictate"]
        assert manager.poll() == []
    finally:
        manager.stop()
        manager.stop()  # повторный stop безопасен
    assert not manager.running


def test_hotkey_busy_detected() -> None:
    first = hotkeys.HotkeyManager()
    spec = None
    for candidate in ("Ctrl+Alt+Shift+F7", "Ctrl+Alt+Shift+F6", "Ctrl+Alt+Shift+F5"):
        if first.register("dictate", candidate):
            spec = candidate
            break
    assert spec is not None
    first.start()
    second = hotkeys.HotkeyManager()
    try:
        assert first.running
        assert second.register("dictate", spec) is False, "занятое сочетание принято"
        error = second.last_error() or ""
        assert "занято" in error.lower(), f"непонятная ошибка: {error!r}"
    finally:
        first.stop()
        second.stop()


def test_hotkey_probe_busy_free_and_own() -> None:
    holder = hotkeys.HotkeyManager()
    busy = None
    for candidate in (
        "Ctrl+Alt+Shift+F10",
        "Ctrl+Alt+Shift+F9",
        "Ctrl+Alt+Shift+F8",
        "Ctrl+Alt+Shift+F7",
    ):
        if holder.register("dictate", candidate):
            busy = candidate
            break
    assert busy is not None, "не удалось занять тестовое сочетание"
    holder.start()
    prober = hotkeys.HotkeyManager()
    try:
        assert holder.is_registered("dictate")
        # Своё уже зарегистрированное сочетание probe считает доступным.
        assert holder.probe(busy) is True, "probe не распознал своё сочетание"
        # Чужое занятое сочетание probe видит как занятое, без побочных эффектов.
        assert prober.probe(busy) is False
        assert "занято" in (prober.last_error() or "").lower()
        free = None
        for candidate in (
            "Ctrl+Alt+Shift+F3",
            "Ctrl+Alt+Shift+F2",
            "Ctrl+Alt+Shift+F1",
        ):
            if prober.probe(candidate):
                free = candidate
                break
        assert free is not None, "probe не нашёл свободное сочетание"
        # probe не занимает клавишу: её всё ещё можно зарегистрировать.
        assert holder.probe(free) is True
    finally:
        holder.stop()
        prober.stop()


def test_hotkey_register_keeps_old_on_busy() -> None:
    owner = hotkeys.HotkeyManager()
    busy = None
    for candidate in ("Ctrl+Alt+Shift+F11", "Ctrl+Alt+Shift+F10", "Ctrl+Alt+Shift+F9"):
        if owner.register("other", candidate):
            busy = candidate
            break
    assert busy is not None
    owner.start()
    manager = hotkeys.HotkeyManager()
    old = None
    for candidate in ("Ctrl+Alt+Shift+F6", "Ctrl+Alt+Shift+F5", "Ctrl+Alt+Shift+F4"):
        if manager.register("dictate", candidate):
            old = candidate
            break
    assert old is not None and old != busy
    manager.start()
    try:
        assert manager.is_registered("dictate")
        # Попытка занять занятое чужое сочетание не должна снимать старое.
        assert manager.register("dictate", busy) is False
        assert manager.is_registered("dictate"), "старое сочетание потеряно"
        assert manager.registered_spec("dictate") == old
        hotkey_id = manager._name_to_id.get("dictate")
        assert hotkey_id is not None
        manager._dispatch_id(int(hotkey_id))
        assert manager.poll() == ["dictate"], "старое сочетание перестало срабатывать"
        assert "занято" in (manager.last_error() or "").lower()
    finally:
        manager.stop()
        owner.stop()


# --------------------------------------------------------------------------- autostart


def test_autostart_roundtrip() -> None:
    value_name = f"WinDictate-Test-{os.getpid()}"
    command_line = '"C:\\fake\\pythonw.exe" "C:\\fake\\app.py" --autostart'
    try:
        assert autostart.is_enabled(value_name) is False
        assert autostart.enable(value_name=value_name, command_line=command_line)
        assert autostart.is_enabled(value_name)
        assert autostart.get_value(value_name) == command_line
        assert autostart.disable(value_name)
        assert autostart.is_enabled(value_name) is False
        assert autostart.disable(value_name) is True  # повторное удаление — успех
    finally:
        autostart.disable(value_name)
    line = autostart.command()
    assert "--autostart" in line and line.startswith('"')


def test_autostart_does_not_touch_production() -> None:
    # Тестовое значение не должно остаться в реестре и не совпадать с боевым.
    assert autostart.get_value(f"WinDictate-Test-{os.getpid()}") is None
    assert f"WinDictate-Test-{os.getpid()}" != autostart.VALUE_NAME


# --------------------------------------------------------------------------- winapi


def test_clipboard_roundtrip() -> None:
    previous = winapi.get_clipboard_text()
    try:
        assert winapi.set_clipboard_text("WinDictate clipboard")
        assert winapi.get_clipboard_text() == "WinDictate clipboard"
        assert winapi.set_clipboard_text("вторая строка\nи перенос")
        assert winapi.get_clipboard_text() == "вторая строка\nи перенос"
    finally:
        winapi.set_clipboard_text(previous)


def test_clipboard_unicode() -> None:
    previous = winapi.get_clipboard_text()
    try:
        sample = "Диктовка: ёжик, «кавычки», 😀"
        assert winapi.set_clipboard_text(sample)
        assert winapi.get_clipboard_text() == sample
    finally:
        winapi.set_clipboard_text(previous)


def test_foreground_window() -> None:
    hwnd = winapi.get_foreground_window()
    assert isinstance(hwnd, int)
    assert hwnd >= 0


def test_single_instance() -> None:
    name = f"WinDictate-UnitTest-{os.getpid()}"
    assert winapi.acquire_single_instance(name) is True
    assert winapi.acquire_single_instance(name) is False, "mutex не защищает"


# --------------------------------------------------------------------------- UI


def test_ui_window_created() -> None:
    root = get_root()
    window = DictationWindow(root, {}, config.load_settings())
    try:
        root.update()
        assert window.win.title() == WINDOW_TITLE
        assert window.visible is False
        assert window.state == "idle"
        window.show()
        root.update()
        assert window.visible is True
    finally:
        window.hide()
        window.win.destroy()


def test_ui_text_state_elapsed() -> None:
    root = get_root()
    window = DictationWindow(root, {}, config.load_settings())
    try:
        window.set_text("Привет, это проверка")
        assert window.get_text() == "Привет, это проверка"
        window.set_state("recording")
        assert str(window.text.cget("state")) == "disabled"
        window.set_elapsed(65_000)
        assert window.timer_label.cget("text") == "1:05"
        assert window.title_label.cget("text") == theme.micro("Запись")
        window.set_state("idle")
        assert str(window.text.cget("state")) == "normal"
        assert window.title_label.cget("text") == theme.micro("Готово")
    finally:
        window.win.destroy()


def test_ui_level_meter() -> None:
    root = get_root()
    window = DictationWindow(root, {}, config.load_settings())
    try:
        window.set_state("recording")
        window.set_level(0.0)
        start = window.level_canvas.coords(window._level_rect)
        window.set_level(0.5)
        loud = window.level_canvas.coords(window._level_rect)
        assert loud[2] > start[2], f"уровень не вырос: {start} -> {loud}"
        window.set_level(100.0)
        assert window.level_canvas.coords(window._level_rect)[2] <= window._level_width
    finally:
        window.win.destroy()


def test_ui_enter_copies() -> None:
    root = get_root()
    previous = winapi.get_clipboard_text()
    window = DictationWindow(
        root,
        {"copy": lambda: winapi.set_clipboard_text(window.get_text())},
        config.load_settings(),
    )
    try:
        window.set_text("Текст для буфера")
        window.set_state("idle")
        window._on_enter(None)
        assert winapi.get_clipboard_text() == "Текст для буфера"
    finally:
        winapi.set_clipboard_text(previous)
        window.win.destroy()


def test_ui_escape_and_close() -> None:
    root = get_root()
    fired: list[str] = []
    window = DictationWindow(
        root,
        {
            "escape": lambda: fired.append("escape"),
            "close": lambda: fired.append("close"),
            "stop": lambda: fired.append("stop"),
        },
        config.load_settings(),
    )
    try:
        window._on_escape(None)
        assert fired == ["escape"]
        window.set_state("recording")
        window._on_enter(None)  # Enter во время записи = остановить
        assert fired == ["escape", "stop"]
        window.close_button.invoke()
        assert fired[-1] == "close"
    finally:
        window.win.destroy()


def test_ui_buttons_visibility() -> None:
    root = get_root()
    window = DictationWindow(root, {}, config.load_settings())
    try:
        window.set_state("recording")
        root.update()
        assert window.stop_button.winfo_manager() != ""
        assert window.copy_button.winfo_manager() == ""
        window.set_state("idle")
        root.update()
        assert window.copy_button.winfo_manager() != ""
        assert window.paste_button.winfo_manager() != ""
        assert window.settings_button.winfo_manager() != ""
        assert window.stop_button.winfo_manager() == ""
    finally:
        window.win.destroy()


def test_ui_key_bindings() -> None:
    root = get_root()
    window = DictationWindow(root, {}, config.load_settings())
    try:
        assert window.win.bind("<Return>")
        assert window.win.bind("<Control-Return>")
        assert window.win.bind("<Escape>")
    finally:
        window.win.destroy()


def test_ui_scale_and_format() -> None:
    settings = dict(config.DEFAULTS)
    settings["uiScale"] = 1.2
    root = get_root()
    window = DictationWindow(root, {}, settings)
    try:
        assert abs(window.ui_scale - 1.2) < 1e-9
        assert window.width == int(860 * 1.2), window.width
        assert window._level_width == max(1, round(96 * 1.2))
    finally:
        window.win.destroy()
    assert format_elapsed(0) == "0:00"
    assert format_elapsed(65_000) == "1:05"
    assert format_elapsed(600_000) == "10:00"


def test_spec_from_tk_event() -> None:
    event = types.SimpleNamespace(keysym="space", state=0x0004 | 0x20000)
    assert spec_from_tk_event(event) == "Ctrl+Alt+Space"
    event = types.SimpleNamespace(keysym="F9", state=0)
    assert spec_from_tk_event(event) == "F9"
    event = types.SimpleNamespace(keysym="a", state=0)
    assert spec_from_tk_event(event) is None, "одиночная буква не должна становиться хоткеем"


def test_settings_window_collect() -> None:
    root = get_root()
    saved: list[dict] = []
    tested: list[dict] = []
    window = SettingsWindow(
        root,
        dict(config.DEFAULTS),
        on_save=saved.append,
        on_test_mic=tested.append,
    )
    try:
        root.update()
        assert window.win.title() == SETTINGS_TITLE
        collected = window.get_settings()
        assert collected["pauseMs"] == config.DEFAULTS["pauseMs"]
        assert collected["model"] == "small"
        assert collected["theme"] == "paper"
        assert collected["accent"] == config.DEFAULTS["accent"]
        assert collected["sizeMode"] == "normal"
        assert collected["pinned"] is False
        assert collected["sttDevice"] == "auto"
        assert collected["vad"] is True
        assert "autostart" in collected
        assert window.theme_combo.get() == THEME_LABELS["paper"]
        assert str(window.theme_combo.cget("style")).startswith("Sa")
        assert isinstance(window.pause_scale, BrandSlider)
        assert isinstance(window.limit_scale, BrandSlider)
        assert window.max_record_scale is window.limit_scale
        assert window.scrollbar.winfo_manager() != ""
        from tkinter import ttk

        style = ttk.Style(window.win)
        assert style.lookup("Sa.TCombobox", "fieldbackground")
        window.pause_scale.set(1500)
        window.limit_scale.set(240)
        window.model_combo.set("medium")
        window.theme_combo.set(THEME_LABELS["scene"])
        window.size_combo.set("Широкий")
        window._select_accent(theme.ACCENT_PRESETS[1][0])
        window.pinned_var.set(True)
        window.vad_var.set(False)
        window.stt_device_combo.set("CPU")
        window.auto_copy_var.set(False)
        window.hotkey_entry.configure(state="normal")
        window.hotkey_entry.delete(0, "end")
        window.hotkey_entry.insert(0, "Ctrl+Shift+F10")
        window.hotkey_entry.configure(state="readonly")
        window.test_mic_button.invoke()
        assert tested and tested[0]["pauseMs"] == 1500
        assert tested[0]["theme"] == "scene"
        window._save()
        assert saved, "on_save не вызван"
        assert saved[-1]["pauseMs"] == 1500
        assert saved[-1]["maxRecordMs"] == 240_000
        assert saved[-1]["model"] == "medium"
        assert saved[-1]["autoCopy"] is False
        assert saved[-1]["hotkey"] == "Ctrl+Shift+F10"
        assert saved[-1]["theme"] == "scene"
        assert saved[-1]["accent"] == theme.normalize_hex(theme.ACCENT_PRESETS[1][0])
        assert saved[-1]["sizeMode"] == "wide"
        assert saved[-1]["pinned"] is True
        assert saved[-1]["sttDevice"] == "cpu"
        assert saved[-1]["vad"] is False
        assert "сохранены" in window.status_label.cget("text").lower()
        assert window.visible is False
    finally:
        window.destroy()


def test_settings_status_and_stays_open_on_error() -> None:
    root = get_root()
    window = SettingsWindow(root, dict(config.DEFAULTS), on_save=lambda _data: False)
    try:
        window.show()
        root.update()
        assert window.visible is True
        assert window._save() is False
        root.update()
        assert window.visible is True, "окно настроек закрылось при ошибке"
        status = window.status_label.cget("text").lower()
        assert "не удалось" in status, status
        # Успешное сохранение показывает статус и закрывает окно чуть позже.
        window.on_save = lambda _data: True
        assert window._save() is True
        assert "сохранены" in window.status_label.cget("text").lower()
        root.update()
        time.sleep(0.9)
        root.update()
        assert window.visible is False, "окно не закрылось после успешного сохранения"
    finally:
        window.destroy()


def test_settings_reset_to_defaults() -> None:
    root = get_root()
    saved: list[dict] = []
    settings = dict(config.DEFAULTS)
    settings.update({"pauseMs": 1500, "theme": "scene", "autoCopy": False})
    window = SettingsWindow(
        root, settings, on_save=lambda data: (saved.append(data), True)[1]
    )
    try:
        window.show()
        root.update()
        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            assert window.reset_button.invoke() is None  # invoke вызывает _reset
            assert window._reset() is True
        assert saved, "_reset не вызвал on_save"
        reset_data = saved[-1]
        assert reset_data["pauseMs"] == config.DEFAULTS["pauseMs"]
        assert reset_data["theme"] == "paper"
        assert reset_data["hotkey"] == config.DEFAULTS["hotkey"]
        assert reset_data["autoCopy"] is True
        # Форма перестроена под значения по умолчанию.
        assert window.pause_scale.get() == config.DEFAULTS["pauseMs"]
        assert window.theme_combo.get() == THEME_LABELS["paper"]
        assert "сброш" in window.status_label.cget("text").lower()
    finally:
        window.destroy()


def test_settings_capture_warns_busy_hotkey() -> None:
    root = get_root()
    window = SettingsWindow(
        root,
        dict(config.DEFAULTS),
        on_probe_hotkey=lambda _spec: False,
    )
    try:
        window._start_capture("hotkey")
        window._capture_key(types.SimpleNamespace(keysym="d", state=0x0004 | 0x20000))
        assert window.hotkey_entry.get() == "Ctrl+Alt+D"
        status = window.status_label.cget("text").lower()
        assert "занято" in status, status
    finally:
        window.destroy()


# --------------------------------------------------------------------------- brand UI


def test_theme_tokens_and_fonts() -> None:
    registered = theme.register_fonts()
    assert isinstance(registered, bool), "register_fonts должен возвращать bool"
    scene = theme.colors("scene")
    paper = theme.colors("paper")
    for key in (
        "bg", "bg_alt", "surface", "surface_alt", "surface_hover", "border",
        "text", "text_secondary", "text_muted", "accent", "accent_strong",
        "accent_soft", "accent_deep", "on_accent", "danger", "ok", "track",
    ):
        assert key in scene, f"в scene нет {key}"
        assert key in paper, f"в paper нет {key}"
    # Канон 27.10.2026: белая бумага, тёплый графит, терракота, волосяная линия.
    assert paper["bg"] == "#FFFFFF"
    assert paper["text"] == "#24211C"
    assert paper["text_secondary"] == "#625E58"
    assert paper["text_muted"] == "#646A71"
    assert paper["border"] == "#E5E5E5"
    assert paper["accent"] == "#C0603C"
    assert scene["bg"] == "#14120F"
    assert scene["surface"] == "#191919"
    assert theme.DEFAULT_THEME == "paper"
    assert theme.FONT_DISPLAY == "Onest"
    assert theme.FONT_TEXT == "Onest"
    assert theme.font_text(12)[0] in ("Onest", theme.FALLBACK_TEXT)
    assert theme.RADIUS["window"] == 0
    assert theme.RADIUS["card"] == 0
    assert theme.RADIUS["button"] == 0
    assert theme.RADIUS["field"] == 2
    assert theme.APP_TITLE == "Диктовка"
    assert theme.APP_VERSION == "0.1.0"
    # Микролейблы канона — ALL CAPS в квадратных скобках.
    micro = theme.micro("Распознавание")
    assert micro.startswith("[") and micro.endswith("]")
    assert "РАСПОЗНАВАНИЕ" in micro.replace("\u2009", "").replace(" ", "")
    # Акцент — только терракотовая шкала.
    assert 3 <= len(theme.ACCENT_PRESETS) <= 5
    for value, _name in theme.ACCENT_PRESETS:
        red, green, blue = (int(value[i:i + 2], 16) for i in (1, 3, 5))
        assert red > green > blue, value  # терракота — тёплый красно-оранжевый
    custom = theme.colors("paper", "#C0603C")
    assert custom["accent"] == "#C0603C"
    assert theme.normalize_hex("c0603c") == "#C0603C"


def test_theme_mark_and_icons() -> None:
    root = get_root()
    assert theme.ICON_PATH.is_file(), theme.ICON_PATH
    assert theme.TRAY_ICON_PATH.is_file(), theme.TRAY_ICON_PATH
    for theme_name in ("paper", "scene"):
        assert theme.icon_photo(root, theme_name, 22) is not None
    for name in ("mic", "stop", "copy", "paste", "settings", "close", "pin", "new", "reset", "refresh", "model", "appearance", "keyboard", "volume", "info"):
        assert theme.icon(name), name
    with mock.patch.dict(
        sys.modules, {"PIL": None, "PIL.Image": None, "PIL.ImageTk": None}
    ):
        assert theme.icon_photo(root, "paper", 22) is None


def test_dictation_scene_brand() -> None:
    root = get_root()
    settings = dict(config.DEFAULTS)
    settings["theme"] = "scene"
    window = DictationWindow(root, {}, settings)
    try:
        scene = window.palette
        assert window.theme_name == "scene"
        assert window.win.cget("bg") == scene["bg"]
        assert window.text.cget("bg") == scene["surface"]
        assert window.text.cget("fg") == scene["text"]
        font = str(window.text.cget("font"))
        assert theme.FONT_TEXT in font or theme.FALLBACK_TEXT in font, font
        assert window.title_brand.cget("text") == "Диктовка"
        assert window.kicker_label.cget("text").startswith("[")
        assert window.mic_label is not None
        window.set_text("пример текста")
        assert window.copy_button.fill_color == scene["accent"]
        assert window.copy_button.text_color == scene["on_accent"]
        for attribute in (
            "win", "text", "title_label", "notice_label", "level_canvas",
            "new_button", "stop_button", "copy_button", "paste_button",
            "settings_button", "close_button", "pin_button", "size_button",
        ):
            assert hasattr(window, attribute), attribute
        assert window.win.title() == WINDOW_TITLE
    finally:
        window.win.destroy()


def test_dictation_paper_theme() -> None:
    root = get_root()
    window = DictationWindow(root, {}, dict(config.DEFAULTS))
    try:
        paper = window.palette
        assert window.theme_name == "paper"  # бумага по умолчанию
        assert window.win.cget("bg") == "#FFFFFF"
        assert window.text.cget("bg") == "#FFFFFF"
        assert window.text.cget("fg") == "#24211C"
        window.set_text("пример текста")
        assert window.copy_button.fill_color == paper["accent"]
        assert window.title_brand.cget("fg") == paper["text"]
        # Статус и кикер — микролейблы канона.
        assert window.title_label.cget("text").startswith("[")
        assert window.kicker_label.cget("text").startswith("[")
    finally:
        window.win.destroy()


def test_apply_theme_live() -> None:
    root = get_root()
    window = DictationWindow(root, {}, dict(config.DEFAULTS))
    try:
        window.set_text("живой текст")
        window.set_state("recording")
        window.set_elapsed(5000)
        changed = dict(config.DEFAULTS)
        changed.update(
            {"theme": "scene", "accent": theme.ACCENT_PRESETS[1][0], "sizeMode": "wide"}
        )
        window.apply_appearance(changed)
        dark = theme.colors("scene", theme.ACCENT_PRESETS[1][0])
        assert window.theme_name == "scene"
        assert window.accent == theme.ACCENT_PRESETS[1][0]
        assert window.win.cget("bg") == dark["bg"]
        assert window.get_text() == "живой текст"
        assert window.state == "recording"
        assert window.accent_strip.cget("bg") == dark["accent"]
        assert window.size_mode == "wide"
        window.apply_appearance(dict(config.DEFAULTS))
        assert window.win.cget("bg") == "#FFFFFF"
        assert window.get_text() == "живой текст"
    finally:
        window.win.destroy()


def test_brand_source_has_no_legacy_style() -> None:
    forbidden_colors = ("#17181c", "#202228", "#4f8cff", "#ff4d4d", "#3ddc84")
    forbidden_brand = ("сториарт", "storyartpro", "ds 1.17", "brand_kicker", "mark_photo")
    for name in ("ui.py", "tray.py", "app.py"):
        path = ROOT / name if name == "app.py" else ROOT / "dictate" / name
        text = path.read_text(encoding="utf-8").lower()
        for token in forbidden_colors:
            assert token not in text, f"{name}: остался старый цвет {token}"
        for token in forbidden_brand:
            assert token not in text, f"{name}: остался бренд {token}"
    ui_text = (ROOT / "dictate" / "ui.py").read_text(encoding="utf-8")
    assert "CreateRoundRectRgn" not in ui_text
    for phrase in (
        "КОПИРОВАТЬ", "ВСТАВИТЬ", "НАСТРОЙКИ", "ЗАКРЫТЬ",
        "Диктовка", "Запись", "Готово",
    ):
        assert phrase in ui_text, phrase


def test_settings_window_paper_theme() -> None:
    import tkinter as tk

    root = get_root()
    settings = dict(config.DEFAULTS)
    settings["theme"] = "paper"
    window = SettingsWindow(root, settings, on_save=lambda _data: None)
    try:
        paper = window.palette
        assert window.theme_name == "paper"
        assert window.win.cget("bg") == paper["bg"]
        assert window.header_label.cget("text") == "Настройки"
        assert window.header_kicker.cget("text").startswith("[")
        assert window.theme_combo.get() == THEME_LABELS["paper"]
        assert window.save_button.fill_color == paper["accent"]
        assert window.save_button.text_color == paper["on_accent"]
        assert window.version_label.cget("text").startswith("[")
        assert "WINDICTATE" in window.version_label.cget("text").replace(
            "\u2009", ""
        ).replace(" ", "").upper()
        assert window.win.title() == SETTINGS_TITLE == "Настройки — Диктовка"
        # Секции-полосы: микролейблы с номерами.
        def walk(widget: tk.Misc):
            for child in widget.winfo_children():
                yield child
                yield from walk(child)

        texts = [
            str(child.cget("text"))
            for child in walk(window.body)
            if isinstance(child, tk.Label)
        ]
        assert any(
            "РАСПОЗНАВАНИЕ" in text.replace("\u2009", "").replace(" ", "")
            for text in texts
        )
    finally:
        window.destroy()


def test_icons_and_tooltips() -> None:
    root = get_root()
    window = DictationWindow(root, {}, dict(config.DEFAULTS))
    try:
        assert window.pin_button.tooltip is not None
        assert window.size_button.tooltip is not None
        assert window.settings_button.tooltip is not None
        assert window.close_button.tooltip is not None
        if icons_available():
            assert window.pin_button._glyph
        else:
            assert window.pin_button._glyph == ""
        window.set_pinned(True)
        assert window.pinned is True
        window.set_pinned(False)
        assert window.pinned is False
    finally:
        window.win.destroy()


def test_accent_picker_and_hex() -> None:
    """Палитра акцента — только терракотовые пресеты, без HEX-поля."""
    root = get_root()
    window = SettingsWindow(root, dict(config.DEFAULTS))
    try:
        assert len(window.accent_swatches) == len(theme.ACCENT_PRESETS)
        assert not hasattr(window, "accent_entry")
        assert not hasattr(window, "accent_preview_button")
        window._select_accent(theme.ACCENT_PRESETS[2][0])
        assert window.get_settings()["accent"] == theme.normalize_hex(
            theme.ACCENT_PRESETS[2][0]
        )
        assert window.accent_value_label.cget("text")
        # В UI есть только значения из палитры канона.
        assert window.get_settings()["accent"] in {
            theme.normalize_hex(value) for value, _name in theme.ACCENT_PRESETS
        }
    finally:
        window.destroy()


def test_size_modes_geometry_and_persist() -> None:
    root = get_root()
    events: list[str] = []
    window = DictationWindow(root, {"size": events.append}, dict(config.DEFAULTS))
    try:
        window.set_size_mode("compact")
        assert window.size_mode == "compact"
        assert (window.width, window.height) == (560, 184)
        window.set_size_mode("wide")
        assert (window.width, window.height) == (1120, 480)
        window.set_size_mode("normal")
        assert (window.width, window.height) == (860, 400)
        assert events == ["compact", "wide", "normal"]
        for mode, (width, height) in ui_module.SIZE_SIZES.items():
            assert width <= 1366 and height <= 768, mode
    finally:
        window.win.destroy()
    saved = dict(config.DEFAULTS)
    saved["sizeMode"] = "wide"
    window2 = DictationWindow(root, {}, saved)
    try:
        assert window2.size_mode == "wide"
        assert (window2.width, window2.height) == (1120, 480)
    finally:
        window2.win.destroy()


def test_window_position_restore_and_clamp() -> None:
    root = get_root()
    saved = dict(config.DEFAULTS)
    saved["windowX"] = -99999
    saved["windowY"] = 999999
    window = DictationWindow(root, {}, saved)
    try:
        x, y = window.current_position()
        assert x >= 8 and y >= 8, (x, y)
        screen_w, screen_h = window._screen_bounds()
        assert x + window.width <= screen_w, (x, window.width, screen_w)
    finally:
        window.win.destroy()


def test_drag_emits_move_callback() -> None:
    root = get_root()
    moves: list[tuple[int, int]] = []
    window = DictationWindow(root, {"move": lambda x, y: moves.append((x, y))}, dict(config.DEFAULTS))
    try:
        window.win.deiconify()
        root.update()
        window._drag_start(types.SimpleNamespace(x_root=window.win.winfo_x() + 30, y_root=window.win.winfo_y() + 10))
        window._drag_move(types.SimpleNamespace(x_root=window.win.winfo_x() + 90, y_root=window.win.winfo_y() + 70))
        window._drag_end()
        assert moves, "перетаскивание не вызвало callback"
    finally:
        window.win.destroy()


def test_new_button_flow() -> None:
    root = get_root()
    calls: list[str] = []

    class FakeEngine:
        state = "idle"

        def start(self, *_args: Any, **_kwargs: Any) -> None:
            calls.append("start")
            self.state = "recording"

        def cancel(self) -> None:
            calls.append("cancel")
            self.state = "idle"

        def stop(self) -> None:
            calls.append("stop")
            self.state = "idle"

    window = DictationWindow(root, {}, dict(config.DEFAULTS))
    try:
        window.new_button.invoke()  # без callback — ничего не ломается
        assert window.state == "idle"
    finally:
        window.win.destroy()
    # С app: idle → очистить и начать; recording → cancel + start.
    import app as app_module

    instance = make_isolated_app(app_module)
    fake = FakeEngine()
    instance.engine = fake
    try:
        instance.window.set_text("старый текст")
        instance.engine = fake
        instance.window.set_state("idle")
        instance._new_dictation()
        assert calls[-1] == "start"
        assert instance.window.get_text() == ""
        fake.state = "recording"
        instance.window.set_state("recording")
        instance._new_dictation()
        assert calls[-2:] == ["cancel", "start"]
        assert "cancel" in calls
    finally:
        instance._quit_ui()


def test_copy_edited_text() -> None:
    root = get_root()
    previous = winapi.get_clipboard_text()
    window = DictationWindow(root, {}, dict(config.DEFAULTS))
    try:
        window.set_state("idle")
        window.set_text("исходный")
        window.text.insert("end", " исправленный")
        assert window.get_text() == "исходный исправленный"
        winapi.set_clipboard_text(window.get_text())
        assert winapi.get_clipboard_text() == "исходный исправленный"
    finally:
        winapi.set_clipboard_text(previous)
        window.win.destroy()


def test_pinned_widget_behavior() -> None:
    import app as app_module

    saved = dict(config.DEFAULTS)
    saved["pinned"] = True
    with mock.patch.object(
        app_module.config, "load_settings", lambda path=None: dict(saved)
    ):
        instance = app_module.DictateApp(no_tray=True)
    try:
        assert instance.window.pinned is True
        instance.window.set_text("готовый текст")
        instance.window.set_state("idle")
        instance.window.show(focus=False)
        previous = winapi.get_clipboard_text()
        try:
            instance._copy_and_close()
            assert instance.window.visible is True, "pinned-окно скрылось после копирования"
            assert winapi.get_clipboard_text() == "готовый текст"
            instance._handle_done(
                {"type": "done", "text": "готовый текст", "cancelled": False, "duration_ms": 100, "error": None}
            )
            assert instance.window.visible is True, "pinned-окно скрылось после done"
        finally:
            winapi.set_clipboard_text(previous)
    finally:
        instance._quit_ui()


def test_capture_ui_script_exists() -> None:
    script = ROOT / "tests" / "capture_ui.py"
    assert script.is_file(), "нет tests/capture_ui.py"
    text = script.read_text(encoding="utf-8").lower()
    for token in ("findwindoww", "getwindowrect", "imagegrab", "png"):
        assert token in text, f"capture_ui.py не содержит {token}"


def test_accent_strip_recording_only() -> None:
    root = get_root()
    window = DictationWindow(root, {}, dict(config.DEFAULTS))
    scene = window.palette
    try:
        assert int(window.accent_strip.cget("height")) >= 2
        window.set_state("idle")
        assert window.accent_strip.cget("bg") == scene["bg"], "в idle полоска невидима"
        window.set_state("recording")
        assert window.accent_strip.cget("bg") == scene["accent"], "в записи полоска accent"
        window.set_state("finalizing")
        assert window.accent_strip.cget("bg") == scene["accent"]
        window.set_state("idle")
        assert window.accent_strip.cget("bg") == scene["bg"]
    finally:
        window.win.destroy()


def test_brand_slider_colors_and_input() -> None:
    root = get_root()
    scene = theme.colors("scene")
    paper = theme.colors("paper")
    # Для настоящих мышиных/клавиатурных событий нужен отображённый Toplevel.
    import tkinter as tk

    top = tk.Toplevel(root)
    top.title("slider-test")
    top.geometry("420x110+140+140")
    top.attributes("-topmost", True)
    slider = BrandSlider(
        top, from_=300, to=2000, step=50, value=800, palette=scene
    )
    slider.configure(width=300)
    slider.pack(fill="x", padx=20, pady=12)
    try:
        top.update()
        assert slider.track_color == scene["border"]
        assert slider.fill_color == scene["accent"]
        assert slider.thumb_color == scene["accent"]
        # Наведение: синтетический <Enter> на скрытом root может не дойти,
        # поэтому дергаем обработчик наведения напрямую (он же висит на <Enter>).
        slider._on_enter()
        assert slider.thumb_color == scene["accent_strong"], "hover-ползунок не accent_strong"
        slider._on_leave()
        assert slider.thumb_color == scene["accent"]
        # Клавиатуре нужен фокус.
        slider.focus_force()
        top.update()
        # Клавиатура: ←/→ шаг, Home/End края.
        assert slider.get() == 800
        slider.event_generate("<Right>")
        top.update()
        assert slider.get() == 850, slider.get()
        slider.event_generate("<Left>")
        top.update()
        assert slider.get() == 800, slider.get()
        slider.event_generate("<End>")
        top.update()
        assert slider.get() == 2000, slider.get()
        slider.event_generate("<Home>")
        top.update()
        assert slider.get() == 300, slider.get()
        # Мышь: клик в правую часть и перетаскивание влево.
        width = max(100, slider.winfo_width())
        slider.event_generate("<Button-1>", x=int(width * 0.78), y=13)
        top.update()
        clicked = float(slider.get())
        assert 1200 < clicked <= 2000, clicked
        slider.event_generate("<B1-Motion>", x=int(width * 0.25), y=13)
        top.update()
        dragged = float(slider.get())
        assert dragged < clicked, (clicked, dragged)
        slider.event_generate("<ButtonRelease-1>", x=int(width * 0.25), y=13)
        # Программный интерфейс и callback.
        seen: list[float] = []
        second = BrandSlider(
            top,
            from_=10,
            to=600,
            step=10,
            value=600,
            command=seen.append,
            palette=paper,
        )
        second.pack(fill="x", padx=20)
        try:
            top.update()
            assert second.track_color == paper["border"]
            assert second.thumb_color == paper["accent"]
            assert second.get() == 600
            second.set(120, notify=True)
            assert second.get() == 120
            assert seen and seen[-1] == 120
        finally:
            second.destroy()
    finally:
        top.destroy()
    # Значение формы настроек приходит из брендового слайдера.
    window = SettingsWindow(root, dict(config.DEFAULTS))
    try:
        window.pause_scale.set(1500)
        window.limit_scale.set(240)
        collected = window.get_settings()
        assert collected["pauseMs"] == 1500, collected["pauseMs"]
        assert collected["maxRecordMs"] == 240_000, collected["maxRecordMs"]
    finally:
        window.destroy()


def test_ui_show_focuses_text() -> None:
    root = get_root()
    window = DictationWindow(root, {}, dict(config.DEFAULTS))
    calls: list[str] = []
    original = window.win.focus_force

    def spy() -> None:
        calls.append("focus")
        try:
            original()
        except Exception:  # noqa: BLE001 - в фоновом прогоне фокус может не даться
            pass

    window.win.focus_force = spy  # type: ignore[method-assign]
    try:
        window.show()
        root.update()
        time.sleep(0.08)
        root.update()
        assert len(calls) >= 2, f"focus_force вызван {len(calls)} раз (ожидали show + повтор)"
        assert window.visible is True
    finally:
        window.hide()
        window.win.destroy()


class _SlowStopEngine:
    """Фейковый движок: stop() «финализирует» 300 мс, чтобы поймать фриз UI."""

    def __init__(self) -> None:
        self.state = "idle"
        self.stop_calls = 0
        self.cancel_calls = 0

    def start(self, base_text: str = "") -> None:
        self.state = "recording"

    def stop(self) -> None:
        time.sleep(0.3)
        self.stop_calls += 1
        self.state = "idle"

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.state = "idle"


def test_stop_dictation_async_and_escape() -> None:
    import app as app_module

    instance = make_isolated_app(app_module)
    previous_clipboard = winapi.get_clipboard_text()
    original_history_path = app_module.config.history_path
    try:
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / "history.json"
            app_module.config.history_path = lambda: history
            instance.settings["autoCopy"] = True
            fake = _SlowStopEngine()
            instance.engine = fake
            fake.state = "recording"
            instance.window.set_state("recording")
            started = time.monotonic()
            instance.stop_dictation()
            elapsed = time.monotonic() - started
            assert elapsed < 0.15, f"stop_dictation() заблокировал Tk-поток на {elapsed:.3f} с"
            assert instance.window.state == "finalizing"
            assert "Распознаю" in instance.window.notice_label.cget("text")
            assert instance._stopping is True
            # Повторный stop во время финализации игнорируется.
            instance.stop_dictation()
            assert instance._stopping is True
            deadline = time.monotonic() + 3.0
            while fake.stop_calls == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert fake.stop_calls == 1, "engine.stop() так и не выполнился в фоне"
            # done приходит через очередь: копирование и история работают.
            instance._handle_event(
                {
                    "type": "done",
                    "text": "тест асинхронного стопа",
                    "cancelled": False,
                    "duration_ms": 1200,
                    "error": None,
                }
            )
            assert instance._stopping is False
            assert instance.window.get_text() == "тест асинхронного стопа"
            assert winapi.get_clipboard_text() == "тест асинхронного стопа"
            assert app_module.load_history(), "история не записалась после done"
            # Esc во время finalizing отменяет запись и закрывает окно.
            fake2 = _SlowStopEngine()
            instance.engine = fake2
            fake2.state = "finalizing"
            instance.window.show()
            instance.window.set_state("finalizing")
            instance._escape()
            assert fake2.cancel_calls == 1, "Esc в finalizing не отменил запись"
            assert instance.window.visible is False
    finally:
        app_module.config.history_path = original_history_path
        winapi.set_clipboard_text(previous_clipboard)
        instance._quit_ui()


def test_record_mic_fallback_and_wav() -> None:
    import app as app_module
    import numpy as np

    class FakeSd:
        def __init__(self) -> None:
            self.recordings: list[tuple[int, int]] = []

        def rec(self, frames, samplerate, channels, dtype, device):  # noqa: ANN001
            if int(samplerate) == 16000:
                raise RuntimeError("устройство не поддерживает 16 кГц")
            self.recordings.append((int(frames), int(samplerate)))
            return np.zeros((int(frames), 1), dtype=np.float32)

        def wait(self) -> None:
            return None

        def query_devices(self, device=None, kind=None):  # noqa: ANN001
            return {"default_samplerate": 48000.0}

    original = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = FakeSd()  # type: ignore[assignment]
    try:
        samples, rate = app_module.record_mic(None, 1.0)
        assert rate == 48000, f"фолбэк частоты не сработал: {rate}"
        assert samples.shape == (48000,), samples.shape
    finally:
        if original is not None:
            sys.modules["sounddevice"] = original
        else:
            sys.modules.pop("sounddevice", None)

    wav = app_module.wav_from_samples(np.zeros(48000, dtype=np.float32), 48000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert int.from_bytes(wav[22:24], "little") == 1, "ожидался моно"
    assert int.from_bytes(wav[24:28], "little") == 16000, "WAV не пересэмплирован в 16 кГц"
    assert int.from_bytes(wav[40:44], "little") == 32000, "длина данных не 1 с × 16 кГц × 2"


# --------------------------------------------------------------------------- app


def test_app_import_no_windows() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=str(ROOT),
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout.strip() == b"", "импорт app.py что-то напечатал"


def test_app_history_roundtrip() -> None:
    import app as app_module

    with tempfile.TemporaryDirectory() as tmp:
        history = Path(tmp) / "history.json"
        original = app_module.config.history_path
        app_module.config.history_path = lambda: history
        try:
            app_module.append_history("первая", 2)
            app_module.append_history("вторая", 2)
            app_module.append_history("третья", 2)
            items = app_module.load_history()
            assert [item["text"] for item in items] == ["третья", "вторая"]
            app_module.append_history("игнор", 0)
            assert len(app_module.load_history()) == 2
        finally:
            app_module.config.history_path = original


class _FakeHotkeys:
    """Фейковый менеджер хоткеев для проверки транзакционного сохранения."""

    def __init__(self, specs: dict[str, str], busy: set[str] | None = None) -> None:
        self._specs = dict(specs)
        self.busy = set(busy or ())
        self.probes: list[str] = []
        self._last_error: str | None = None
        self.running = True

    def probe(self, spec: str) -> bool:
        self.probes.append(spec)
        if spec in self._specs.values():
            return True
        return spec not in self.busy

    def register(self, name: str, spec: str) -> bool:
        if spec in self.busy:
            self._last_error = (
                f"Сочетание «{spec}» уже занято другим приложением"
            )
            return False
        self._specs[name] = spec
        self._last_error = None
        return True

    def is_registered(self, name: str) -> bool:
        return name in self._specs

    def registered_spec(self, name: str) -> str | None:
        return self._specs.get(name)

    def last_error(self) -> str | None:
        return self._last_error

    def stop(self) -> None:
        self.running = False


def test_app_save_busy_hotkey_keeps_old() -> None:
    import app as app_module

    instance = make_isolated_app(app_module)
    writes: list[dict] = []
    settings_window = None
    try:
        base = dict(config.DEFAULTS)
        instance.settings = dict(base)
        fake = _FakeHotkeys(
            {"dictate": base["hotkey"], "settings": base["settingsHotkey"]},
            busy={"Ctrl+Alt+Shift+D"},
        )
        instance.hotkeys = fake
        settings_window = SettingsWindow(instance.root, base)
        instance.settings_window = settings_window
        data = dict(base)
        data["hotkey"] = "Ctrl+Alt+Shift+D"
        data["autostart"] = False
        with mock.patch.object(
            app_module.config,
            "save_settings",
            lambda value, path=None: writes.append(dict(value)),
        ), mock.patch.object(app_module.autostart, "enable", lambda *a, **k: True), (
            mock.patch.object(app_module.autostart, "disable", lambda *a, **k: True)
        ):
            result = instance._save_settings(data)
        assert result is False, "сохранение с занятой клавишей прошло успешно"
        assert writes == [], "settings.json записан, хотя хоткей не применился"
        assert instance.settings["hotkey"] == base["hotkey"], "настройки потеряли старый хоткей"
        assert fake._specs["dictate"] == base["hotkey"], "старый хоткей снят"
        notice = str(instance.window.notice_label.cget("text")).lower()
        assert "занят" in notice, notice
        assert "Ctrl+Alt+Shift+D".lower() in notice
        # Та же ошибка видна в окне настроек (статус-строка).
        status = str(settings_window.status_label.cget("text")).lower()
        assert "занят" in status, status
        # Подсказка свободного варианта тоже проверяется.
        assert any(
            probe in ui_module.HOTKEY_CANDIDATES for probe in fake.probes
        ), "не предложен свободный вариант"
    finally:
        if settings_window is not None:
            settings_window.destroy()
        instance._quit_ui()


def test_app_save_free_hotkey_applied_immediately() -> None:
    import app as app_module

    instance = make_isolated_app(app_module)
    writes: list[dict] = []
    try:
        base = dict(config.DEFAULTS)
        instance.settings = dict(base)
        fake = _FakeHotkeys(
            {"dictate": base["hotkey"], "settings": base["settingsHotkey"]}
        )
        instance.hotkeys = fake
        free = "Ctrl+Alt+Shift+F12"
        data = dict(base)
        data["hotkey"] = free
        data["autostart"] = False
        with mock.patch.object(
            app_module.config,
            "save_settings",
            lambda value, path=None: writes.append(dict(value)),
        ), mock.patch.object(app_module.autostart, "enable", lambda *a, **k: True), (
            mock.patch.object(app_module.autostart, "disable", lambda *a, **k: True)
        ):
            result = instance._save_settings(data)
        assert result is True
        assert writes and writes[-1]["hotkey"] == free, "settings.json не обновлён"
        assert instance.settings["hotkey"] == free
        assert fake._specs["dictate"] == free, "новый хоткей не зарегистрирован"
        assert instance.hotkey_registered.get("dictate") is True
    finally:
        instance._quit_ui()


def test_startup_settings_logged() -> None:
    import logging

    import app as app_module

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("win-dictate")
    handler = _Capture()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    instance = make_isolated_app(app_module)
    try:
        instance.settings = dict(config.DEFAULTS)
        instance.hotkey_registered = {"dictate": True, "settings": True}
        instance._log_applied_settings()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        instance._quit_ui()
    assert any(
        "Настройки применены" in message
        and "hotkey=Ctrl+Alt+Space" in message
        and "зарегистрирован" in message
        for message in records
    ), records


def test_selftest_json_valid() -> None:
    env = dict(os.environ)
    env["WIN_DICTATE_SELFTEST_FAST"] = "1"
    result = subprocess.run(
        [sys.executable, "app.py", "--selftest"],
        cwd=str(ROOT),
        capture_output=True,
        timeout=180,
        env=env,
    )
    stdout = result.stdout.decode("utf-8", "replace").strip()
    assert result.returncode in (0, 1), f"код {result.returncode}: {result.stderr[:500]!r}"
    report = json.loads(stdout)
    assert isinstance(report, dict)
    for key in ("ok", "checks", "stt", "mic", "window"):
        assert key in report, f"в отчёте нет {key}"
    for key in (
        "config_roundtrip",
        "hotkey_parse",
        "autostart_roundtrip",
        "clipboard",
        "silence_engine",
        "tts_absent",
        "single_instance",
    ):
        assert key in report["checks"], f"в checks нет {key}"
        assert isinstance(report["checks"][key], bool)
    assert report["window"]["created"] is True
    assert report["window"]["title"] == WINDOW_TITLE
    assert report["window"]["copy_ok"] is True
    assert report["checks"]["config_roundtrip"] is True
    assert report["checks"]["autostart_roundtrip"] is True
    assert report["checks"]["clipboard"] is True
    assert report["checks"]["tts_absent"] is True


def test_config_new_keys() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "accent": "4f8cff",
                    "sizeMode": "wide",
                    "pinned": "true",
                    "windowX": -50000,
                    "windowY": 50000,
                    "settingsW": 100,
                    "settingsH": 99999,
                    "sttDevice": "gpu",
                    "vad": "false",
                }
            ),
            encoding="utf-8",
        )
        settings = config.load_settings(path)
        assert settings["accent"] == "#4F8CFF"
        assert settings["sizeMode"] == "wide"
        assert settings["pinned"] is True
        assert settings["windowX"] == -20000
        assert settings["windowY"] == 20000
        assert settings["settingsW"] == 560
        assert settings["settingsH"] == 6000
        assert settings["sttDevice"] == "auto"
        assert settings["vad"] is False
    defaults = config.normalize_settings({})
    assert defaults["accent"] == config.DEFAULT_ACCENT
    assert defaults["sizeMode"] == "normal"
    assert defaults["pinned"] is False
    assert defaults["windowX"] is None
    assert defaults["sttDevice"] == "auto"
    assert defaults["vad"] is True
    assert set(config.SIZE_MODES) == {"compact", "normal", "wide"}
    assert set(config.STT_DEVICES) == {"auto", "cuda", "cpu"}


def test_settings_model_status_and_reload() -> None:
    root = get_root()
    reloads: list[dict] = []
    window = SettingsWindow(
        root,
        dict(config.DEFAULTS),
        on_model_status=lambda: ("Загружено: small · cuda · float16 · порт 3211", False),
        on_reload_model=lambda data: reloads.append(data),
    )
    try:
        assert "Загружено" in window.model_status_label.cget("text")
        window.reload_button.invoke()
        assert reloads, "кнопка «Перезагрузить модель» не вызвала callback"
        assert "перезагружаю" in window.model_status_label.cget("text").lower()
        window.set_reload_enabled(True)
        window.set_model_status("Загружено: tiny · cpu · int8 · порт 3215", False)
        assert "tiny" in window.model_status_label.cget("text")
    finally:
        window.destroy()


def test_settings_resize_and_scroll_persist() -> None:
    root = get_root()
    sizes: list[tuple[int, int]] = []
    window = SettingsWindow(
        root, dict(config.DEFAULTS), on_settings_size=lambda w, h: sizes.append((w, h))
    )
    try:
        assert window.canvas.winfo_manager() != ""
        assert window.scrollbar.winfo_manager() != ""
        assert window.win.resizable() == (True, True)
        window._last_size = (700, 680)
        window._emit_size()
        assert sizes and sizes[-1] == (700, 680)
        collected = window.get_settings()
        assert collected["settingsW"] >= 560
        assert collected["settingsH"] >= 520
    finally:
        window.destroy()


def test_app_accent_theme_applied_live() -> None:
    import app as app_module

    instance = make_isolated_app(app_module)
    writes: list[dict] = []
    try:
        base = dict(config.DEFAULTS)
        instance.settings = dict(base)
        instance.hotkeys = _FakeHotkeys(
            {"dictate": base["hotkey"], "settings": base["settingsHotkey"]}
        )
        data = dict(base)
        data.update(
            {
                "theme": "paper",
                "accent": theme.ACCENT_PRESETS[1][0],
                "sizeMode": "wide",
                "autostart": False,
            }
        )
        with mock.patch.object(
            app_module.config,
            "save_settings",
            lambda value, path=None: writes.append(dict(value)),
        ), mock.patch.object(app_module.autostart, "enable", lambda *a, **k: True), (
            mock.patch.object(app_module.autostart, "disable", lambda *a, **k: True)
        ):
            assert instance._save_settings(data) is True
        assert writes and writes[-1]["accent"] == theme.normalize_hex(theme.ACCENT_PRESETS[1][0])
        assert instance.window.theme_name == "paper"
        assert instance.window.accent == theme.ACCENT_PRESETS[1][0]
        assert instance.window.size_mode == "wide"
        assert instance.window.palette["accent"] == theme.ACCENT_PRESETS[1][0]
    finally:
        instance._quit_ui()


def test_header_icon_buttons_visible() -> None:
    root = get_root()
    for theme_name, size_mode, pinned in (
        ("scene", "normal", False),
        ("paper", "wide", False),
        ("scene", "normal", True),
    ):
        settings = dict(config.DEFAULTS)
        settings.update({"theme": theme_name, "sizeMode": size_mode, "pinned": pinned})
        window = DictationWindow(root, {}, settings)
        try:
            window.set_text("текст")
            window.set_state("idle")
            window.show()
            root.update()
            for state in ("idle", "recording", "finalizing", "idle"):
                window.set_state(state)
                root.update()
                for name in ("pin_button", "size_button", "settings_button", "close_button"):
                    button = getattr(window, name)
                    assert button.winfo_ismapped(), f"{name} не отображена ({theme_name}/{size_mode}/{state})"
                    left = button.winfo_rootx() - window.win.winfo_rootx()
                    right = left + button.winfo_width()
                    assert left >= 0 and right <= window.width, (
                        name, state, left, right, window.width
                    )
                    assert button.tooltip is not None, name
        finally:
            window.hide()
            window.win.destroy()
            root.update()


def test_bottom_buttons_idle_and_recording() -> None:
    root = get_root()
    window = DictationWindow(root, {}, dict(config.DEFAULTS))
    try:
        window.set_text("готовый текст")
        window.set_state("idle")
        for name in (
            "new_button", "copy_button", "paste_button",
            "bottom_settings_button", "bottom_close_button",
        ):
            assert getattr(window, name).winfo_manager() != "", name
        assert "КОПИРОВАТЬ" in window.copy_button.cget("text")
        assert "ВСТАВИТЬ" in window.paste_button.cget("text")
        assert "НАСТРОЙКИ" in window.bottom_settings_button.cget("text")
        assert "ЗАКРЫТЬ" in window.bottom_close_button.cget("text")
        window.set_state("recording")
        for name in (
            "copy_button", "paste_button",
            "bottom_settings_button", "bottom_close_button",
        ):
            assert getattr(window, name).winfo_manager() == "", name
        assert window.stop_button.winfo_manager() != ""
        assert window.new_button.winfo_manager() != ""
        window.set_state("idle")
        assert window.paste_button.winfo_manager() != ""
    finally:
        window.win.destroy()


def test_compact_layout_fits_window() -> None:
    root = get_root()
    settings = dict(config.DEFAULTS)
    settings["sizeMode"] = "compact"
    window = DictationWindow(root, {}, settings)
    try:
        window.set_text("Компактный текст, одна строка диктовки и всё видно.")
        window.set_state("idle")
        window.show()
        root.update()
        assert (window.width, window.height) == (560, 184)
        for name in (
            "new_button", "copy_button", "paste_button",
            "bottom_settings_button", "bottom_close_button",
        ):
            button = getattr(window, name)
            assert button.winfo_ismapped(), f"compact: {name} не отображена"
            left = button.winfo_rootx() - window.win.winfo_rootx()
            right = left + button.winfo_width()
            assert left >= 0 and right <= window.width, (name, left, right)
        # «Новая» в compact — иконка + tooltip, текст не нужен.
        assert window.new_button._icon_only is True
        assert window.get_text().startswith("Компактный")
        # Шапка тоже в пределах окна.
        for name in ("pin_button", "size_button", "settings_button", "close_button"):
            button = getattr(window, name)
            assert button.winfo_ismapped(), f"compact: {name} не отображена"
            right = (
                button.winfo_rootx() - window.win.winfo_rootx() + button.winfo_width()
            )
            assert right <= window.width, (name, right)
    finally:
        window.hide()
        window.win.destroy()
        root.update()


def test_accent_apply_button_and_label() -> None:
    """Канон: акцент выбирается только палитрой терракотовых пресетов."""
    root = get_root()
    window = SettingsWindow(root, dict(config.DEFAULTS))
    try:
        assert not hasattr(window, "accent_entry")
        assert not hasattr(window, "accent_preview_button")
        assert len(window.accent_swatches) == len(theme.ACCENT_PRESETS) == 4
        window._select_accent(theme.ACCENT_PRESETS[3][0])
        assert window.get_settings()["accent"] == theme.normalize_hex(
            theme.ACCENT_PRESETS[3][0]
        )
        assert window.accent_value_label.cget("text")
    finally:
        window.destroy()


def test_settings_all_sections_reachable() -> None:
    root = get_root()
    window = SettingsWindow(root, dict(config.DEFAULTS))
    try:
        window.win.geometry("640x600")
        window.show()
        root.update()
        assert window.canvas.winfo_manager() != ""
        assert window.scrollbar.winfo_manager() != ""
        window.canvas.update_idletasks()
        scrollregion = window.canvas.cget("scrollregion")
        assert scrollregion, "scrollregion не рассчитан"
        window.canvas.yview_moveto(1.0)
        root.update()
        first, last = window.canvas.yview()
        assert last > 0.9, (first, last)
        # Нижняя панель с кнопками не уезжает в скролл.
        assert window.save_button.winfo_ismapped()
        assert window.reset_button.winfo_ismapped()
        assert window.cancel_button.winfo_ismapped()
    finally:
        window.destroy()


# --------------------------------------------------------------------------- runner


TESTS = [
    ("config_defaults", test_config_defaults),
    ("config_roundtrip", test_config_roundtrip),
    ("config_partial_merge", test_config_partial_merge),
    ("config_repairs_bad_values", test_config_repairs_bad_values),
    ("config_atomic_save", test_config_atomic_save),
    ("config_paths", test_config_paths),
    ("hotkey_parse_valid", test_hotkey_parse_valid),
    ("hotkey_parse_invalid", test_hotkey_parse_invalid),
    ("hotkey_register_poll_stop", test_hotkey_register_poll_stop),
    ("hotkey_busy_detected", test_hotkey_busy_detected),
    ("hotkey_probe_busy_free_and_own", test_hotkey_probe_busy_free_and_own),
    ("hotkey_register_keeps_old_on_busy", test_hotkey_register_keeps_old_on_busy),
    ("autostart_roundtrip", test_autostart_roundtrip),
    ("autostart_production_untouched", test_autostart_does_not_touch_production),
    ("clipboard_roundtrip", test_clipboard_roundtrip),
    ("clipboard_unicode", test_clipboard_unicode),
    ("foreground_window", test_foreground_window),
    ("single_instance", test_single_instance),
    ("ui_window_created", test_ui_window_created),
    ("ui_text_state_elapsed", test_ui_text_state_elapsed),
    ("ui_level_meter", test_ui_level_meter),
    ("ui_enter_copies", test_ui_enter_copies),
    ("ui_escape_and_close", test_ui_escape_and_close),
    ("ui_buttons_visibility", test_ui_buttons_visibility),
    ("ui_key_bindings", test_ui_key_bindings),
    ("ui_scale_and_format", test_ui_scale_and_format),
    ("ui_show_focuses_text", test_ui_show_focuses_text),
    ("spec_from_tk_event", test_spec_from_tk_event),
    ("settings_window_collect", test_settings_window_collect),
    ("settings_status_and_stays_open_on_error", test_settings_status_and_stays_open_on_error),
    ("settings_reset_to_defaults", test_settings_reset_to_defaults),
    ("settings_capture_warns_busy_hotkey", test_settings_capture_warns_busy_hotkey),
    ("theme_tokens_and_fonts", test_theme_tokens_and_fonts),
    ("theme_mark_and_icons", test_theme_mark_and_icons),
    ("dictation_scene_brand", test_dictation_scene_brand),
    ("dictation_paper_theme", test_dictation_paper_theme),
    ("apply_theme_live", test_apply_theme_live),
    ("accent_strip_recording_only", test_accent_strip_recording_only),
    ("brand_slider_colors_and_input", test_brand_slider_colors_and_input),
    ("brand_source_has_no_legacy_style", test_brand_source_has_no_legacy_style),
    ("settings_window_paper_theme", test_settings_window_paper_theme),
    ("icons_and_tooltips", test_icons_and_tooltips),
    ("accent_picker_and_hex", test_accent_picker_and_hex),
    ("size_modes_geometry_and_persist", test_size_modes_geometry_and_persist),
    ("window_position_restore_and_clamp", test_window_position_restore_and_clamp),
    ("drag_emits_move_callback", test_drag_emits_move_callback),
    ("new_button_flow", test_new_button_flow),
    ("copy_edited_text", test_copy_edited_text),
    ("pinned_widget_behavior", test_pinned_widget_behavior),
    ("config_new_keys", test_config_new_keys),
    ("settings_model_status_and_reload", test_settings_model_status_and_reload),
    ("settings_resize_and_scroll_persist", test_settings_resize_and_scroll_persist),
    ("app_accent_theme_applied_live", test_app_accent_theme_applied_live),
    ("header_icon_buttons_visible", test_header_icon_buttons_visible),
    ("bottom_buttons_idle_and_recording", test_bottom_buttons_idle_and_recording),
    ("compact_layout_fits_window", test_compact_layout_fits_window),
    ("accent_apply_button_and_label", test_accent_apply_button_and_label),
    ("settings_all_sections_reachable", test_settings_all_sections_reachable),
    ("capture_ui_script_exists", test_capture_ui_script_exists),
    ("record_mic_fallback_and_wav", test_record_mic_fallback_and_wav),
    ("stop_dictation_async_and_escape", test_stop_dictation_async_and_escape),
    ("app_import_no_windows", test_app_import_no_windows),
    ("app_history_roundtrip", test_app_history_roundtrip),
    ("app_save_busy_hotkey_keeps_old", test_app_save_busy_hotkey_keeps_old),
    ("app_save_free_hotkey_applied_immediately", test_app_save_free_hotkey_applied_immediately),
    ("startup_settings_logged", test_startup_settings_logged),
    ("selftest_json_valid", test_selftest_json_valid),
]


def main() -> int:
    # Тесты не должны сыпать предупреждениями приложения в stderr.
    logging.getLogger("win-dictate").addHandler(logging.NullHandler())
    try:
        for name, function in TESTS:
            check(name, function)
    finally:
        global _ROOT
        if _ROOT is not None:
            try:
                _ROOT.destroy()
            except Exception:  # noqa: BLE001
                pass
            _ROOT = None
    total = len(PASSED) + len(FAILED)
    for name, error in FAILED:
        print(f"FAIL {name}")
        print(error)
    if FAILED:
        print(f"PASS {len(PASSED)}/{total}")
        return 1
    print(f"PASS {len(PASSED)}/{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
