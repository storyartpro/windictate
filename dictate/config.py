"""Настройки WinDictate: схема, дефолты, загрузка/сохранение.

Файл: %LOCALAPPDATA%\\WinDictate\\settings.json.
Загрузка всегда возвращает полный словарь: недостающие ключи заполняются
дефолтами, битые типы и значения вне диапазона чинятся. Сохранение атомарное
(.tmp + os.replace), чтобы падение посреди записи не оставило обрезанный JSON.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

APP_NAME = "WinDictate"

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "device": None,  # int — индекс входного устройства sounddevice; None = системный
    "language": "ru",  # ru | en | auto
    "model": "small",  # tiny|base|small|medium|large-v3-turbo
    "beamSize": 5,  # 1..8
    "pauseMs": 800,  # 300..2000
    "maxRecordMs": 600000,  # 10000..600000
    "hotkey": "Ctrl+Alt+Space",
    "settingsHotkey": "Ctrl+Alt+S",
    "autoCopy": True,
    "pasteOnCtrlEnter": True,
    "showElapsed": True,
    "singleLine": True,
    "historyLimit": 50,  # 0..200
    "uiScale": 1.0,  # 0.8..1.6
    "theme": "paper",  # paper | scene (бумага по умолчанию — канон СТОРИАРТ)
    "accent": "#C0603C",  # любой #RRGGBB
    "sizeMode": "normal",  # compact | normal | wide
    "pinned": False,  # закреплённый виджет
    "windowX": None,  # позиция окна диктовки
    "windowY": None,
    "settingsW": None,  # размер окна настроек
    "settingsH": None,
    "sttDevice": "auto",  # auto | cuda | cpu
    "vad": True,  # Silero VAD
    "autoPostprocess": True,  # пунктуация/грамматика после диктовки
    "suggestContested": True,  # показывать разбор «спорных» слов
    "sessionsLimit": 300,  # сколько сессий хранить (20..1000)
    "llmApiKey": "",  # ключ DeepSeek API (хранится в settings.json)
    "llmModel": "deepseek-chat",  # deepseek-chat | deepseek-reasoner
    "llmPreset": "standard",  # light | standard | strong
    "llmStrength": 3,  # 1..5 — насколько смело можно менять текст
    "llmQuality": 3,  # 1..5 — качество/глубина проверки
}

LANGUAGES = ("ru", "en", "auto")
MODELS = ("tiny", "base", "small", "medium", "large-v3-turbo")
THEMES = ("scene", "paper")
SIZE_MODES = ("compact", "normal", "wide")
STT_DEVICES = ("auto", "cuda", "cpu")
LLM_MODELS = ("deepseek-chat", "deepseek-reasoner")
LLM_PRESETS = ("light", "standard", "strong")
DEFAULT_ACCENT = "#C0603C"

_KEYS = tuple(DEFAULTS.keys())


# --------------------------------------------------------------------------- paths


def data_dir() -> Path:
    """Каталог данных: %LOCALAPPDATA%\\WinDictate.

    В портативной версии лаунчер передаёт WIN_DICTATE_DATA=<папка версии>\\data,
    чтобы настройки, история и логи оставались рядом с программой.
    """
    override = os.environ.get("WIN_DICTATE_DATA")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / "AppData" / "Local" / APP_NAME


def logs_dir() -> Path:
    return data_dir() / "logs"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def history_path() -> Path:
    return data_dir() / "history.json"


# --------------------------------------------------------------------------- coercion


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else int(round(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                number = float(text)
            except ValueError:
                return None
            return int(round(number))
    return None


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    number = _as_int(value)
    if number is None:
        return default
    return max(low, min(high, number))


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on", "да", "вкл"):
            return True
        if text in ("0", "false", "no", "off", "нет", "выкл"):
            return False
    return default


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_hotkey(value: Any, default: str) -> str:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return default


def normalize_settings(raw: Any) -> dict[str, Any]:
    """Мержит произвольный словарь с DEFAULTS и чинит типы/диапазоны."""
    source: dict[str, Any] = raw if isinstance(raw, dict) else {}
    settings: dict[str, Any] = dict(DEFAULTS)

    device = _as_int(source.get("device"))
    settings["device"] = device if device is not None and device >= 0 else None

    language = source.get("language")
    settings["language"] = language if language in LANGUAGES else DEFAULTS["language"]

    model = source.get("model")
    settings["model"] = model if model in MODELS else DEFAULTS["model"]

    settings["version"] = 1
    settings["beamSize"] = _clamp_int(source.get("beamSize"), DEFAULTS["beamSize"], 1, 8)
    settings["pauseMs"] = _clamp_int(source.get("pauseMs"), DEFAULTS["pauseMs"], 300, 2000)
    settings["maxRecordMs"] = _clamp_int(
        source.get("maxRecordMs"), DEFAULTS["maxRecordMs"], 10_000, 600_000
    )
    settings["hotkey"] = _as_hotkey(source.get("hotkey"), DEFAULTS["hotkey"])
    settings["settingsHotkey"] = _as_hotkey(
        source.get("settingsHotkey"), DEFAULTS["settingsHotkey"]
    )
    settings["autoCopy"] = _as_bool(source.get("autoCopy"), DEFAULTS["autoCopy"])
    settings["pasteOnCtrlEnter"] = _as_bool(
        source.get("pasteOnCtrlEnter"), DEFAULTS["pasteOnCtrlEnter"]
    )
    settings["showElapsed"] = _as_bool(source.get("showElapsed"), DEFAULTS["showElapsed"])
    settings["singleLine"] = _as_bool(source.get("singleLine"), DEFAULTS["singleLine"])
    settings["historyLimit"] = _clamp_int(
        source.get("historyLimit"), DEFAULTS["historyLimit"], 0, 200
    )

    scale = _as_float(source.get("uiScale"))
    if scale is None:
        scale = DEFAULTS["uiScale"]
    settings["uiScale"] = round(max(0.8, min(1.6, scale)), 2)

    theme = source.get("theme")
    settings["theme"] = theme if theme in THEMES else DEFAULTS["theme"]

    accent = _normalize_accent(source.get("accent"))
    settings["accent"] = accent or DEFAULT_ACCENT

    size_mode = source.get("sizeMode")
    settings["sizeMode"] = size_mode if size_mode in SIZE_MODES else DEFAULTS["sizeMode"]
    settings["pinned"] = _as_bool(source.get("pinned"), DEFAULTS["pinned"])

    settings["windowX"] = _clamp_coord(source.get("windowX"))
    settings["windowY"] = _clamp_coord(source.get("windowY"))
    settings["settingsW"] = _clamp_size(source.get("settingsW"), 560, 6000)
    settings["settingsH"] = _clamp_size(source.get("settingsH"), 520, 6000)

    stt_device = source.get("sttDevice")
    settings["sttDevice"] = (
        stt_device if stt_device in STT_DEVICES else DEFAULTS["sttDevice"]
    )
    settings["vad"] = _as_bool(source.get("vad"), DEFAULTS["vad"])

    settings["autoPostprocess"] = _as_bool(
        source.get("autoPostprocess"), DEFAULTS["autoPostprocess"]
    )
    settings["suggestContested"] = _as_bool(
        source.get("suggestContested"), DEFAULTS["suggestContested"]
    )
    settings["sessionsLimit"] = _clamp_int(
        source.get("sessionsLimit"), DEFAULTS["sessionsLimit"], 20, 1000
    )
    api_key = source.get("llmApiKey")
    settings["llmApiKey"] = api_key.strip() if isinstance(api_key, str) else ""
    llm_model = source.get("llmModel")
    settings["llmModel"] = (
        llm_model if llm_model in LLM_MODELS else DEFAULTS["llmModel"]
    )
    preset = source.get("llmPreset")
    settings["llmPreset"] = preset if preset in LLM_PRESETS else DEFAULTS["llmPreset"]
    settings["llmStrength"] = _clamp_int(
        source.get("llmStrength"), DEFAULTS["llmStrength"], 1, 5
    )
    settings["llmQuality"] = _clamp_int(
        source.get("llmQuality"), DEFAULTS["llmQuality"], 1, 5
    )

    # Только известные ключи: мусор из старого/чужого файла отбрасываем.
    return {key: settings[key] for key in _KEYS}


def _normalize_accent(value: Any) -> str | None:
    """#RRGGBB / #RGB / без решётки → #RRGGBB; иначе None."""
    if not isinstance(value, str):
        return None
    text = value.strip().lstrip("#").upper()
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return "#" + text


def _clamp_coord(value: Any) -> int | None:
    if value is None:
        return None
    number = _as_int(value)
    if number is None:
        return None
    return max(-20000, min(20000, number))


def _clamp_size(value: Any, low: int, high: int) -> int | None:
    if value is None:
        return None
    number = _as_int(value)
    if number is None:
        return None
    return max(low, min(high, number))


# --------------------------------------------------------------------------- load / save


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """Читает настройки, мержит с дефолтами. Битый файл = дефолты, без исключений."""
    target = Path(path) if path is not None else settings_path()
    raw: Any = {}
    try:
        with open(target, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        raw = {}
    return normalize_settings(raw)


def save_settings(settings: dict[str, Any], path: Path | None = None) -> None:
    """Атомарно пишет нормализованные настройки (.tmp + os.replace)."""
    target = Path(path) if path is not None else settings_path()
    payload = normalize_settings(settings)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
