"""Win32-помощники WinDictate: буфер обмена, активное окно, Ctrl+V, mutex.

Все функции «мягкие»: при отказе доступа/неожиданной ошибке возвращают False или
пустую строку вместо исключения. Только Windows; на других платформах модуль
импортируется, но функции сразу возвращают отказ.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover - приложение Windows-only
    user32 = None  # type: ignore[assignment]
    kernel32 = None  # type: ignore[assignment]

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
ERROR_ALREADY_EXISTS = 183

SW_RESTORE = 9
VK_CONTROL = 0x11
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1

_mutex_handle: int | None = None


def _bind() -> None:
    """Прописывает argtypes/restypes — иначе на 64-бит ctypes режет указатели."""
    if not _IS_WINDOWS:
        return
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


if _IS_WINDOWS:
    _bind()


# --------------------------------------------------------------------------- clipboard


def _open_clipboard(attempts: int = 8, delay_s: float = 0.02) -> bool:
    if not _IS_WINDOWS:
        return False
    for _ in range(max(1, attempts)):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay_s)
    return False


def set_clipboard_text(text: str) -> bool:
    """Кладёт текст в буфер (CF_UNICODETEXT). False при отказе."""
    if not _IS_WINDOWS:
        return False
    try:
        payload = "" if text is None else str(text)
        if not _open_clipboard():
            return False
        handle = None
        try:
            if not user32.EmptyClipboard():
                return False
            # UTF-16LE + завершающий ноль. Считаем в байтах, а не в символах:
            # эмодзи занимает суррогатную пару, len(str) дал бы меньше байт.
            payload_bytes = payload.encode("utf-16-le") + b"\x00\x00"
            size = len(payload_bytes)
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                return False
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                kernel32.GlobalFree(handle)
                return False
            try:
                source = ctypes.create_string_buffer(payload_bytes, size)
                ctypes.memmove(pointer, source, size)
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                return False
            # Память после успешного SetClipboardData принадлежит буферу — не освобождаем.
            handle = None
            return True
        finally:
            if handle:
                try:
                    kernel32.GlobalFree(handle)
                except Exception:  # noqa: BLE001
                    pass
            user32.CloseClipboard()
    except Exception:  # noqa: BLE001 - буфер не должен ронять приложение
        return False


def get_clipboard_text() -> str:
    """Читает текст из буфера. Пустая строка, если буфер пуст/недоступен."""
    if not _IS_WINDOWS:
        return ""
    try:
        if not _open_clipboard():
            return ""
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                return ctypes.wstring_at(pointer) or ""
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- foreground window


def get_foreground_window() -> int:
    if not _IS_WINDOWS:
        return 0
    try:
        hwnd = user32.GetForegroundWindow()
        return int(hwnd or 0)
    except Exception:  # noqa: BLE001
        return 0


def set_foreground_window(hwnd: int) -> bool:
    """Восстанавливает и поднимает окно. False, если Windows не отдала фокус."""
    if not _IS_WINDOWS or not hwnd:
        return False
    try:
        handle = wintypes.HWND(int(hwnd))
        if not user32.IsWindow(handle):
            return False
        user32.ShowWindow(handle, SW_RESTORE)
        user32.BringWindowToTop(handle)
        if user32.SetForegroundWindow(handle):
            return True
        # Запасной путь: разрешаем себе фокус через AttachThreadInput.
        current = kernel32.GetCurrentThreadId()
        target = user32.GetWindowThreadProcessId(handle, None)
        attached = False
        try:
            if target and current and target != current:
                attached = bool(user32.AttachThreadInput(current, target, True))
            return bool(user32.SetForegroundWindow(handle))
        finally:
            if attached:
                user32.AttachThreadInput(current, target, False)
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- SendInput


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _key(vk: int, flags: int = 0) -> INPUT:
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return item


def send_ctrl_v() -> bool:
    """Синтезирует Ctrl+V через SendInput. False, если ввод заблокирован."""
    if not _IS_WINDOWS:
        return False
    try:
        events = (_key(VK_CONTROL) , _key(VK_V), _key(VK_V, KEYEVENTF_KEYUP), _key(VK_CONTROL, KEYEVENTF_KEYUP))
        array = (INPUT * len(events))(*events)
        sent = user32.SendInput(len(events), ctypes.cast(array, ctypes.c_void_p), ctypes.sizeof(INPUT))
        return int(sent) == len(events)
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- single instance


def acquire_single_instance(name: str = "WinDictate") -> bool:
    """True, если это первый экземпляр. Держит mutex до конца процесса."""
    global _mutex_handle
    if not _IS_WINDOWS:
        return True
    try:
        handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
        if not handle:
            return False
        last_error = ctypes.get_last_error()
        if last_error == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _mutex_handle = int(handle)
        return True
    except Exception:  # noqa: BLE001
        return False


def release_single_instance() -> None:
    global _mutex_handle
    if _IS_WINDOWS and _mutex_handle:
        try:
            kernel32.CloseHandle(wintypes.HANDLE(_mutex_handle))
        except Exception:  # noqa: BLE001
            pass
        _mutex_handle = None
