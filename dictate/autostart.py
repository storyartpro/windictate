"""Автозапуск WinDictate: HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run.

Боевое значение — «WinDictate». Для самотеста и юнит-тестов можно передать
другое имя значения (суффикс), не трогая боевую запись.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:  # winreg есть только на Windows
    import winreg
except ImportError:  # pragma: no cover - на не-Windows просто деградируем
    winreg = None  # type: ignore[assignment]

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "WinDictate"
APP_NAME = "WinDictate"


def app_dir() -> Path:
    """Каталог с app.py (установленная копия или исходники)."""
    return Path(__file__).resolve().parent.parent


def _pythonw(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    executable = Path(sys.executable)
    candidate = executable.with_name("pythonw.exe")
    if candidate.exists():
        return candidate
    return executable


def command(explicit_python: str | None = None, app_script: str | None = None) -> str:
    """Командная строка автозапуска: «pythonw.exe» «app.py» --autostart."""
    python = _pythonw(explicit_python)
    script = Path(app_script) if app_script else app_dir() / "app.py"
    return f'"{python}" "{script}" --autostart'


def _open_run_key(access: int):
    if winreg is None:
        return None
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def get_value(value_name: str = VALUE_NAME) -> str | None:
    """Текущая команда автозапуска (None, если значения нет)."""
    if winreg is None:
        return None
    try:
        with _open_run_key(winreg.KEY_READ) as key:
            data, _kind = winreg.QueryValueEx(key, value_name)
            return str(data)
    except OSError:
        return None


def is_enabled(value_name: str = VALUE_NAME) -> bool:
    return bool(get_value(value_name))


def enable(value_name: str = VALUE_NAME, command_line: str | None = None) -> bool:
    """Записывает команду в HKCU Run. False при отказе доступа."""
    if winreg is None:
        return False
    line = command_line or command()
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, line)
        return True
    except OSError:
        return False


def disable(value_name: str = VALUE_NAME) -> bool:
    """Удаляет значение из HKCU Run. Отсутствие значения — тоже успех."""
    if winreg is None:
        return False
    try:
        with _open_run_key(winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, value_name)
            except FileNotFoundError:
                return True
        return True
    except OSError:
        return False


def sync(enabled: bool) -> bool:
    return enable() if enabled else disable()


if __name__ == "__main__":  # небольшая ручная проверка без запуска приложения
    print(f"{VALUE_NAME}: {get_value()!r}")
    print(f"команда: {command()}")
