"""Глобальные горячие клавиши Windows (ctypes RegisterHotKey).

Схема: в отдельном потоке создаётся скрытое message-only окно, регистрируются
сочетания, крутится цикл GetMessage. WM_HOTKEY разбирается оконной процедурой и
складывается в потокобезопасную очередь; UI забирает срабатывания через poll().

Коды ошибок Windows: 1409 (ERROR_HOTKEY_ALREADY_REGISTERED) — сочетание занято.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LOG = logging.getLogger("win-dictate.hotkeys")

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_DESTROY = 0x0002
WM_APP = 0x8000
WM_HOTKEY_TASK = WM_APP + 1  # внутренняя задача для потока горячих клавиш

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

ERROR_HOTKEY_ALREADY_REGISTERED = 1409

HWND_MESSAGE = wintypes.HWND(-3)

# --------------------------------------------------------------------------- key tables

_MODIFIERS = {
    "CTRL": MOD_CONTROL,
    "CONTROL": MOD_CONTROL,
    "ALT": MOD_ALT,
    "SHIFT": MOD_SHIFT,
    "WIN": MOD_WIN,
    "WINDOWS": MOD_WIN,
    "LWIN": MOD_WIN,
    "RWIN": MOD_WIN,
}

_MOD_ORDER = (
    (MOD_CONTROL, "Ctrl"),
    (MOD_ALT, "Alt"),
    (MOD_SHIFT, "Shift"),
    (MOD_WIN, "Win"),
)

_NAMED_KEYS = {
    "SPACE": 0x20,
    "ENTER": 0x0D,
    "RETURN": 0x0D,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "TAB": 0x09,
    "BACKSPACE": 0x08,
    "BKSP": 0x08,
    "INSERT": 0x2D,
    "INS": 0x2D,
    "DELETE": 0x2E,
    "DEL": 0x2E,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PGUP": 0x21,
    "PAGEDOWN": 0x22,
    "PGDN": 0x22,
    "UP": 0x26,
    "DOWN": 0x28,
    "LEFT": 0x25,
    "RIGHT": 0x27,
    "ARROWUP": 0x26,
    "ARROWDOWN": 0x28,
    "ARROWLEFT": 0x25,
    "ARROWRIGHT": 0x27,
    # Знаки препинания (US-раскладка, OEM-клавиши).
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
    ";": 0xBA,
    "'": 0xDE,
    "[": 0xDB,
    "]": 0xDD,
    "-": 0xBD,
    "=": 0xBB,
    "\\": 0xDC,
    "`": 0xC0,
}

_VK_NAMES = {value: name for name, value in _NAMED_KEYS.items()}

# Канонические подписи для format_hotkey.
_VK_DISPLAY = {
    0x20: "Space",
    0x0D: "Enter",
    0x1B: "Esc",
    0x09: "Tab",
    0x08: "Backspace",
    0x2D: "Insert",
    0x2E: "Delete",
    0x24: "Home",
    0x23: "End",
    0x21: "PageUp",
    0x22: "PageDown",
    0x26: "Up",
    0x28: "Down",
    0x25: "Left",
    0x27: "Right",
}


def _key_code(token: str) -> int | None:
    """VK-код клавиши по токену спецификации (без модификаторов)."""
    upper = token.upper()
    if upper in _NAMED_KEYS:
        return _NAMED_KEYS[upper]
    if len(token) == 1:
        char = token.upper()
        if "A" <= char <= "Z":
            return ord(char)
        if "0" <= char <= "9":
            return ord(char)
        if char in _NAMED_KEYS:
            return _NAMED_KEYS[char]
    if upper.startswith("F") and upper[1:].isdigit():
        number = int(upper[1:])
        if 1 <= number <= 24:
            return 0x70 + (number - 1)
    return None


def parse_hotkey(spec: str) -> tuple[int, int]:
    """Разбирает «Ctrl+Alt+Space» → (mods, vk). Бросает ValueError с русским текстом."""
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("Пустое сочетание клавиш")
    tokens = [part.strip() for part in spec.split("+")]
    if any(not token for token in tokens):
        raise ValueError(f"Некорректное сочетание «{spec}»")
    mods = 0
    key_token: str | None = None
    for token in tokens:
        upper = token.upper()
        if upper in _MODIFIERS:
            if key_token is not None:
                raise ValueError(f"В сочетании «{spec}» модификатор стоит после клавиши")
            mods |= _MODIFIERS[upper]
            continue
        if key_token is not None:
            raise ValueError(f"В сочетании «{spec}» лишняя клавиша «{token}»")
        key_token = token
    if key_token is None:
        raise ValueError(f"В сочетании «{spec}» не указана клавиша")
    vk = _key_code(key_token)
    if vk is None:
        raise ValueError(f"Неизвестная клавиша «{key_token}»")
    return mods, vk


def format_hotkey(spec: str) -> str:
    """Канонический вид сочетания (для подписей); при ошибке возвращает исходное."""
    try:
        mods, vk = parse_hotkey(spec)
    except ValueError:
        return spec
    parts = [label for bit, label in _MOD_ORDER if mods & bit]
    parts.append(_vk_display(vk))
    return "+".join(parts)


def _vk_display(vk: int) -> str:
    if vk in _VK_DISPLAY:
        return _VK_DISPLAY[vk]
    if vk in _VK_NAMES:
        return _VK_NAMES[vk]
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x70 + 1}"
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    return f"0x{vk:02X}"


# --------------------------------------------------------------------------- Win32 structures

WNDPROC = ctypes.WINFUNCTYPE(
    wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
user32.UnregisterClassW.restype = wintypes.BOOL
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.DefWindowProcW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.DefWindowProcW.restype = wintypes.LPARAM
user32.RegisterHotKey.argtypes = [
    wintypes.HWND,
    ctypes.c_int,
    wintypes.UINT,
    wintypes.UINT,
]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = wintypes.LPARAM
user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.PostThreadMessageW.restype = wintypes.BOOL
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

_CLASS_NAME = "WinDictateHotkeyWindow"
_PROBE_ID = 0xBEEF


class HotkeyManager:
    """Регистрация глобальных сочетаний и очередь срабатываний."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._wanted: dict[str, str] = {}  # name -> spec (запрошено)
        self._active: dict[str, tuple[int, int, int]] = {}  # name -> (id, mods, vk)
        self._id_to_name: dict[int, str] = {}
        self._name_to_id: dict[str, int] = {}
        self._next_id = 1
        self._queue: list[str] = []
        self._last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._hwnd: int | None = None
        self._class_atom = 0
        self._wndproc_cb = None  # держим ссылку, иначе ctypes освободит callback
        self._ready = threading.Event()
        self._tasks: queue.Queue = queue.Queue()
        # Уникальное имя класса на экземпляр: несколько менеджеров в процессе
        # (тесты, пересоздание) не должны конфликтовать на RegisterClassExW.
        self._class_name = f"WinDictateHotkeyWindow_{id(self):x}"

    # ------------------------------------------------------------------ public API

    def register(self, name: str, spec: str) -> bool:
        """Регистрирует сочетание. False — занято/невалидно (причина в last_error).

        Если менеджер уже работает, новое сочетание сначала ставится рядом со
        старым: при ошибке старое сочетание остаётся зарегистрированным (никакой
        потери рабочего хоткея из-за занятой комбинации).
        """
        try:
            mods, vk = parse_hotkey(spec)
        except ValueError as exc:
            self._last_error = str(exc)
            return False

        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            hwnd = self._hwnd
            existing = self._active.get(name)

        if running and hwnd:
            if existing is not None and existing[1] == mods and existing[2] == vk:
                with self._lock:
                    self._last_error = None
                return True
            return self._replace_registration(name, spec, mods, vk, int(hwnd))

        # Ещё не запущены: пробно проверяем занятость, только потом запоминаем.
        if not self._probe_free(mods, vk, spec):
            return False
        with self._lock:
            self._wanted[name] = spec
            self._last_error = None
        return True

    def probe(self, spec: str) -> bool:
        """Проверяет доступность сочетания без побочных эффектов для менеджера.

        Если это сочетание уже зарегистрировано нами — True (оно «свободно» для
        нас). Иначе делается временный RegisterHotKey/UnregisterHotKey.
        """
        try:
            mods, vk = parse_hotkey(spec)
        except ValueError as exc:
            self._last_error = str(exc)
            return False
        with self._lock:
            for _hotkey_id, active_mods, active_vk in self._active.values():
                if active_mods == mods and active_vk == vk:
                    self._last_error = None
                    return True
            temp_id = self._next_id
            self._next_id += 1

        ok = user32.RegisterHotKey(None, temp_id, mods | MOD_NOREPEAT, vk)
        if ok:
            user32.UnregisterHotKey(None, temp_id)
            with self._lock:
                self._last_error = None
            return True
        error = ctypes.get_last_error()
        with self._lock:
            if error == ERROR_HOTKEY_ALREADY_REGISTERED:
                self._last_error = (
                    f"Сочетание «{format_hotkey(spec)}» уже занято другим приложением"
                )
            else:
                self._last_error = (
                    f"Не удалось проверить сочетание «{format_hotkey(spec)}» "
                    f"(код {error})"
                )
        return False

    def is_registered(self, name: str) -> bool:
        with self._lock:
            return name in self._active

    def registered_spec(self, name: str) -> str | None:
        """Фактически зарегистрированное сочетание для имени (None — нет)."""
        with self._lock:
            if name not in self._active:
                return None
            return self._wanted.get(name)

    def start(self) -> None:
        """Создаёт окно и поток сообщений, регистрирует накопленные сочетания."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._last_error = None
            thread = threading.Thread(
                target=self._message_loop, name="WinDictateHotkeys", daemon=True
            )
            self._thread = thread
        thread.start()
        self._ready.wait(3.0)

    def stop(self) -> None:
        """Синхронно снимает регистрации и завершает поток сообщений.

        Ждёт завершения message loop, чтобы новый менеджер не столкнулся с ещё
        держащимися клавишами старого (гонка «stop() не успел»).
        """
        with self._lock:
            thread = self._thread
            thread_id = self._thread_id
        if thread is not None and thread.is_alive():
            if thread_id:
                user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
            deadline = time.monotonic() + 5.0
            while thread.is_alive() and time.monotonic() < deadline:
                thread.join(timeout=0.1)
            if thread.is_alive():
                with self._lock:
                    self._last_error = "Поток горячих клавиш не завершился за 5 с"
                LOG.warning("Поток горячих клавиш не завершился за 5 с")
        with self._lock:
            self._thread = None
            self._thread_id = None
            self._hwnd = None
            self._active.clear()
            self._id_to_name.clear()
            self._name_to_id.clear()

    def poll(self) -> list[str]:
        """Имена сработавших сочетаний с прошлого вызова (потокобезопасно)."""
        with self._lock:
            if not self._queue:
                return []
            fired = list(self._queue)
            self._queue.clear()
            return fired

    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    # ------------------------------------------------------------------ internals

    def _probe_free(self, mods: int, vk: int, spec: str) -> bool:
        ok = user32.RegisterHotKey(None, _PROBE_ID, mods | MOD_NOREPEAT, vk)
        if ok:
            user32.UnregisterHotKey(None, _PROBE_ID)
            return True
        error = ctypes.get_last_error()
        if error == ERROR_HOTKEY_ALREADY_REGISTERED:
            self._last_error = f"Сочетание «{format_hotkey(spec)}» уже занято другим приложением"
        else:
            self._last_error = (
                f"Не удалось зарегистрировать сочетание «{format_hotkey(spec)}» "
                f"(код {error})"
            )
        return False

    def _replace_registration(
        self, name: str, spec: str, mods: int, vk: int, hwnd: int
    ) -> bool:
        """Ставит новое сочетание рядом со старым и только затем снимает старое.

        Регистрация выполняется в потоке, которому принадлежит hwnd: иначе
        Windows может вернуть ошибку вместо ожидаемого ERROR_HOTKEY_ALREADY_REGISTERED
        (и занятое сочетание выглядело бы «неизвестной ошибкой»).
        """
        if threading.current_thread() is self._thread:
            return self._replace_registration_now(name, spec, mods, vk, hwnd)
        ready = threading.Event()
        result = {"ok": False}

        def task() -> None:
            try:
                result["ok"] = self._replace_registration_now(
                    name, spec, mods, vk, hwnd
                )
            finally:
                ready.set()

        self._tasks.put(task)
        thread_id = self._thread_id
        if not thread_id or not user32.PostThreadMessageW(
            thread_id, WM_HOTKEY_TASK, 0, 0
        ):
            with self._lock:
                self._last_error = (
                    f"Не удалось применить сочетание «{format_hotkey(spec)}»: "
                    "поток горячих клавиш недоступен"
                )
            return False
        if not ready.wait(5.0):
            with self._lock:
                self._last_error = (
                    f"Не удалось применить сочетание «{format_hotkey(spec)}»: "
                    "поток горячих клавиш не ответил"
                )
            return False
        return bool(result.get("ok"))

    def _replace_registration_now(
        self, name: str, spec: str, mods: int, vk: int, hwnd: int
    ) -> bool:
        with self._lock:
            hotkey_id = self._next_id
            self._next_id += 1
        ok = user32.RegisterHotKey(hwnd, hotkey_id, mods | MOD_NOREPEAT, vk)
        if not ok:
            error = ctypes.get_last_error()
            with self._lock:
                if error == ERROR_HOTKEY_ALREADY_REGISTERED:
                    self._last_error = (
                        f"Сочетание «{format_hotkey(spec)}» уже занято другим приложением"
                    )
                else:
                    self._last_error = (
                        f"Не удалось зарегистрировать сочетание «{format_hotkey(spec)}» "
                        f"(код {error})"
                    )
            return False
        with self._lock:
            old = self._active.get(name)
            if old is not None:
                user32.UnregisterHotKey(hwnd, old[0])
                self._id_to_name.pop(old[0], None)
            self._active[name] = (hotkey_id, mods, vk)
            self._id_to_name[hotkey_id] = name
            self._name_to_id[name] = hotkey_id
            self._wanted[name] = spec
            self._last_error = None
        return True

    def _register_now(self, name: str, spec: str, mods: int, vk: int, hwnd: int) -> bool:
        with self._lock:
            hotkey_id = self._next_id
            self._next_id += 1
        ok = user32.RegisterHotKey(hwnd, hotkey_id, mods | MOD_NOREPEAT, vk)
        if not ok:
            error = ctypes.get_last_error()
            if error == ERROR_HOTKEY_ALREADY_REGISTERED:
                self._last_error = (
                    f"Сочетание «{format_hotkey(spec)}» уже занято другим приложением"
                )
            else:
                self._last_error = (
                    f"Не удалось зарегистрировать сочетание «{format_hotkey(spec)}» "
                    f"(код {error})"
                )
            return False
        with self._lock:
            self._active[name] = (hotkey_id, mods, vk)
            self._id_to_name[hotkey_id] = name
            self._name_to_id[name] = hotkey_id
            self._last_error = None
        return True

    def _unregister_locked(self, name: str) -> None:
        entry = self._active.pop(name, None)
        if entry is None:
            return
        hotkey_id = entry[0]
        hwnd = self._hwnd
        if hwnd:
            user32.UnregisterHotKey(int(hwnd), hotkey_id)
        self._id_to_name.pop(hotkey_id, None)
        self._name_to_id.pop(name, None)

    def _dispatch_id(self, hotkey_id: int) -> None:
        """Внутренний обработчик WM_HOTKEY (в тестах вызывается напрямую)."""
        with self._lock:
            name = self._id_to_name.get(hotkey_id)
            if name is not None:
                self._queue.append(name)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_HOTKEY:
                self._dispatch_id(int(wparam))
                return 0
            if msg == WM_DESTROY:
                return 0
        except Exception:  # noqa: BLE001 - оконная процедура не должна падать
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _create_window(self) -> int:
        instance = kernel32.GetModuleHandleW(None)
        self._wndproc_cb = WNDPROC(self._wndproc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = self._wndproc_cb
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = instance
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = self._class_name
        wc.hIconSm = None
        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            raise OSError(ctypes.get_last_error(), "RegisterClassExW")
        self._class_atom = int(atom)
        hwnd = user32.CreateWindowExW(
            0,
            self._class_name,
            "WinDictateHotkeys",
            0,
            0,
            0,
            0,
            0,
            HWND_MESSAGE,
            None,
            instance,
            None,
        )
        if not hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW")
        return int(hwnd)

    def _drain_tasks(self) -> None:
        """Выполняет задачи, присланные из других потоков (регистрация хоткеев)."""
        while True:
            try:
                task = self._tasks.get_nowait()
            except queue.Empty:
                return
            try:
                task()
            except Exception:  # noqa: BLE001 - поток не должен падать из-за задачи
                LOG.exception("Ошибка задачи горячих клавиш")

    def _message_loop(self) -> None:
        thread_id = int(kernel32.GetCurrentThreadId())
        with self._lock:
            self._thread_id = thread_id
        try:
            hwnd = self._create_window()
        except OSError as exc:
            with self._lock:
                self._last_error = f"Не удалось создать окно горячих клавиш: {exc}"
            self._ready.set()
            return
        with self._lock:
            self._hwnd = hwnd
            items = list(self._wanted.items())

        for name, spec in items:
            try:
                mods, vk = parse_hotkey(spec)
            except ValueError as exc:
                with self._lock:
                    self._last_error = str(exc)
                continue
            if not self._register_now(name, spec, mods, vk, hwnd):
                continue
        # Регистрации готовы — только теперь отпускаем start().
        self._ready.set()

        msg = wintypes.MSG()
        try:
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    break
                if msg.message == WM_HOTKEY_TASK:
                    self._drain_tasks()
                    continue
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            with self._lock:
                names = list(self._active.keys())
            for name in names:
                with self._lock:
                    self._unregister_locked(name)
            if hwnd:
                user32.DestroyWindow(hwnd)
            if self._class_atom:
                user32.UnregisterClassW(self._class_name, kernel32.GetModuleHandleW(None))
                self._class_atom = 0
            with self._lock:
                self._hwnd = None
                self._thread_id = None
