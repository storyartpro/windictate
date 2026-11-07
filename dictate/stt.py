"""STT sidecar manager + HTTP client for WinDictate.

The heavy lifting lives in ``server/stt_server.py`` (local faster-whisper sidecar).
This module starts it lazily on a free port from 3211..3299 (3210 is left free for
other local tools), waits for ``/health`` to report ``ready``, proxies ``POST /stt``
and shuts the process down on exit.

All public methods are serialised with a re-entrant lock: the engine may call
``transcribe`` from its worker thread while the app calls ``ensure_ready`` from a
startup thread.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT_FIRST = 3211
PORT_LAST = 3299
LOG_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MODEL = "small"
DEFAULT_LANGUAGE = "ru"
DEFAULT_BEAM_SIZE = 5
MIN_BEAM_SIZE = 1
MAX_BEAM_SIZE = 8

APP_DIR = Path(__file__).resolve().parent.parent


def default_data_dir() -> Path:
    """``%LOCALAPPDATA%\\WinDictate`` (settings/history/logs live there)."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "WinDictate"
    return Path.home() / "AppData" / "Local" / "WinDictate"


# --------------------------------------------------------------------------- Job Object
# A force-killed or crashed app must not leave stt_server.py running (it would keep a
# CUDA model loaded and squat a port). On Windows the sidecar tree is placed into a
# Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: when the last handle to the job
# closes — including when our process dies abruptly — the kernel terminates every
# process in the job. Job protection is best effort: any API failure degrades to the
# previous shutdown behaviour and never blocks STT.

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JobIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_KERNEL32_API = None
_KERNEL32_READY = False


def _win_kernel32():
    """kernel32 with 64-bit-safe signatures, or None off Windows / on failure."""
    global _KERNEL32_API, _KERNEL32_READY
    if _KERNEL32_READY:
        return _KERNEL32_API
    _KERNEL32_READY = True
    if os.name != "nt":
        return None
    try:
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        api.CreateJobObjectW.restype = ctypes.c_void_p
        api.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        api.SetInformationJobObject.restype = ctypes.c_int
        api.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        api.AssignProcessToJobObject.restype = ctypes.c_int
        api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        api.OpenProcess.restype = ctypes.c_void_p
        api.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        api.TerminateProcess.restype = ctypes.c_int
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        api.CloseHandle.restype = ctypes.c_int
    except (AttributeError, OSError):
        _KERNEL32_API = None
        return None
    _KERNEL32_API = api
    return api


class SttService:
    """Owns the faster-whisper sidecar process and talks to it over HTTP."""

    def __init__(
        self,
        settings: dict,
        python_exe: str | None = None,
        server_script: str | None = None,
        log_path: str | None = None,
    ) -> None:
        self.settings = settings if isinstance(settings, dict) else {}
        self.python_exe = python_exe or sys.executable
        self.server_script = str(server_script or (APP_DIR / "server" / "stt_server.py"))
        self.log_path = Path(log_path) if log_path else default_data_dir() / "logs" / "stt.log"
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._port: int | None = None
        self._server_pid: int | None = None
        self._job_handle: int | None = None
        self._log_handle = None
        self._ready = False
        self._last_error: str | None = None

    # -- configuration -----------------------------------------------------
    def _model(self) -> str:
        return str(self.settings.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL

    def _language(self) -> str:
        language = str(self.settings.get("language") or DEFAULT_LANGUAGE).strip().lower()
        return "auto" if language in ("", "auto", "detect") else language

    def _beam_size(self) -> int:
        try:
            value = int(self.settings.get("beamSize", DEFAULT_BEAM_SIZE))
        except (TypeError, ValueError):
            value = DEFAULT_BEAM_SIZE
        return max(MIN_BEAM_SIZE, min(MAX_BEAM_SIZE, value))

    def _vad(self) -> bool:
        value = self.settings.get("vad", True)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "off", "disabled")
        return bool(value)

    def _device(self) -> str:
        """Requested Whisper device: auto | cuda | cpu (unknown values fall back to auto)."""
        value = str(self.settings.get("sttDevice") or "auto").strip().lower()
        return value if value in ("auto", "cuda", "cpu") else "auto"

    def _sync_config(self) -> tuple[str, str, int, bool, str]:
        return self._model(), self._language(), self._beam_size(), self._vad(), self._device()

    # -- status ------------------------------------------------------------
    @property
    def state(self) -> str:
        """High-level service state for the UI: idle | loading | ready | error."""
        with self._lock:
            if self._ready and self._is_running():
                return "ready"
            if self._last_error:
                return "error"
            if self._is_running():
                return "loading"
            return "idle"

    @property
    def last_error(self) -> str | None:
        """Last launch/reload error in Russian (None when there is nothing to report)."""
        with self._lock:
            return self._last_error

    # -- public API --------------------------------------------------------
    @property
    def port(self) -> int | None:
        """Port of the running sidecar (None when it is not started)."""
        with self._lock:
            return self._port

    def ensure_ready(self, timeout_s: float = 120.0) -> bool:
        """Start the sidecar if needed and wait until ``/health`` reports ready."""
        with self._lock:
            if self._is_running() and self._ready:
                return True
            if not self._is_running():
                if not self._start_locked():
                    return False
            deadline = time.monotonic() + max(1.0, float(timeout_s))
            while True:
                if not self._is_running():
                    self._last_error = "STT-сервис завершился при запуске — см. лог"
                    return False
                payload = self._get_health_locked(1.0)
                if payload is not None:
                    if not self._health_matches_locked(payload):
                        self._stop_locked()
                        self._last_error = "Порт STT-сервиса занят другим процессом"
                        return False
                    if payload.get("ready"):
                        self._ready = True
                        self._last_error = None
                        return True
                    if payload.get("error"):
                        self._last_error = f"STT-сервис не смог загрузить модель: {payload['error']}"
                        return False
                if time.monotonic() >= deadline:
                    self._last_error = "STT-сервис не ответил вовремя"
                    return False
                time.sleep(0.2)

    def health(self) -> dict:
        """Status for the UI: the sidecar ``/health`` payload plus our own fields.

        Always returns a dict (never raises when the sidecar is down) with at least:
        ``ok``, ``ready``, ``state``, ``last_error``, ``model``, ``device``,
        ``computeType``, ``port``, ``lang``, ``pid``.
        """
        with self._lock:
            report: dict = {
                "ok": False,
                "ready": bool(self._ready),
                "model": self._model(),
                "device": self._device(),
                "computeType": "",
                "port": self._port,
                "lang": self._language(),
                "pid": None,
                "state": self.state,
                "last_error": self._last_error,
            }
            if not self._is_running():
                report["error"] = "STT-сервис не запущен"
                return report
            payload = self._get_health_locked(2.0)
            if payload is None:
                report["error"] = "STT-сервис не отвечает"
                return report
            if not self._health_matches_locked(payload):
                report["error"] = "Порт STT-сервиса занят другим процессом"
                report["state"] = "error"
                return report
            self._ready = bool(payload.get("ready"))
            if payload.get("error"):
                self._last_error = f"STT-сервис не смог загрузить модель: {payload['error']}"
            report.update(payload)
            report["port"] = self._port
            report["lang"] = payload.get("language") or self._language()
            report["state"] = self.state
            report["last_error"] = self._last_error
            return report

    def transcribe(
        self,
        wav_bytes: bytes,
        language: str | None = None,
        beam_size: int | None = None,
        vad: bool | None = None,
        timeout_s: float = 30.0,
    ) -> dict:
        """POST ``/stt`` and return the JSON response (dict with ``text`` among others)."""
        with self._lock:
            if not self._is_running():
                # Lazy start is allowed here, but never block the caller for long.
                if not self.ensure_ready(timeout_s=min(10.0, max(1.0, timeout_s))):
                    raise RuntimeError(self._last_error or "Сервис распознавания не готов")
            elif not self._ready:
                payload = self._get_health_locked(1.0)
                if payload is not None and self._health_matches_locked(payload) and payload.get("ready"):
                    self._ready = True
                else:
                    raise RuntimeError("Сервис распознавания ещё загружает модель")
            port = self._port
            if port is None:
                raise RuntimeError("Сервис распознавания не запущен")
            body: dict = {"audioBase64": base64.b64encode(wav_bytes).decode("ascii")}
            if language:
                body["language"] = str(language)
            if beam_size is not None:
                body["beamSize"] = max(MIN_BEAM_SIZE, min(MAX_BEAM_SIZE, int(beam_size)))
            if vad is not None:
                body["vad"] = bool(vad)
            try:
                data = self._post_json_locked(port, "/stt", body, timeout_s)
            except urllib.error.HTTPError as exc:
                detail = self._http_error_detail(exc)
                raise RuntimeError(f"Ошибка распознавания: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self._ready = False
                raise RuntimeError("Сервис распознавания недоступен — проверьте лог") from exc
            except ValueError as exc:  # broken JSON from the sidecar
                raise RuntimeError("Некорректный ответ сервиса распознавания") from exc
            self._ready = True
            return data

    def restart(self) -> None:
        """Kill the sidecar (model settings may have changed); next call starts it anew."""
        with self._lock:
            self._stop_locked()
            self._last_error = None

    def reload(self, settings: dict | None = None) -> bool:
        """Apply recognition settings and restart the sidecar with them.

        ``settings`` (optional) replaces the current settings dict; the sidecar is
        stopped and started again, so a new model/device/VAD/language is really used.
        Thread-safe, never raises, leaves no processes behind (Job Object included).
        Returns True only when the new sidecar reported ``ready``.
        """
        with self._lock:
            if isinstance(settings, dict):
                self.settings = dict(settings)
            self._stop_locked()
            self._last_error = None
            try:
                return bool(self.ensure_ready(timeout_s=120.0))
            except Exception as exc:  # noqa: BLE001 - the app must never crash because of STT
                self._last_error = f"Не удалось перезагрузить модель: {exc}"
                self._stop_locked()
                return False

    def stop(self) -> None:
        """Shut the sidecar down. Idempotent, never raises."""
        with self._lock:
            self._stop_locked()
            self._last_error = None

    # -- sidecar lifecycle -------------------------------------------------
    def _is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start_locked(self) -> bool:
        script = Path(self.server_script)
        if not script.is_file():
            self._last_error = f"Не найден сервер распознавания: {script}"
            return False
        model, language, beam_size, vad, device = self._sync_config()
        last_error: str | None = None
        # Ports can be stolen between the probe and the child bind (or another app may
        # hold one); the /health pid check below tells us whether the child owns it.
        for _attempt in range(5):
            try:
                port = self._free_port()
            except RuntimeError as exc:
                self._last_error = str(exc)
                return False
            command = [
                self.python_exe,
                str(script),
                "--port",
                str(port),
                "--model",
                model,
                "--device",
                device,
                "--language",
                language,
                "--beam-size",
                str(beam_size),
                "--vad" if vad else "--no-vad",
            ]
            try:
                self._open_log_locked()
                self._create_job_locked()  # best effort: kill the tree if we die abruptly
                creationflags = 0
                if os.name == "nt":
                    creationflags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
                # The log handle is the child's stdout/stderr target; it stays open until stop().
                self._proc = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=self._log_handle,
                    stderr=subprocess.STDOUT,
                    cwd=str(script.parent),
                    creationflags=creationflags,
                )
            except OSError as exc:
                self._last_error = f"Не удалось запустить STT-сервис: {exc}"
                self._close_job_locked()
                self._close_log_locked()
                self._proc = None
                return False
            # One level of defence for the launcher shim itself...
            self._assign_to_job_locked(self._proc)
            self._port = port
            self._ready = False
            self._server_pid = None
            # The process that answers /health may be a grandchild: on Windows the venv
            # python.exe can be a launcher shim, so remember the real pid reported by the
            # service and use it for the ownership/liveness checks below.
            payload = self._wait_health_locked(8.0)
            if payload is not None and self._matches_request_locked(payload, model, language):
                self._server_pid = self._payload_pid(payload)
                # ...and the real interpreter by pid, in case it was created before the
                # launcher entered the job (job membership is not retroactive).
                self._assign_pid_to_job_locked(self._server_pid)
                self._last_error = None
                return True
            last_error = f"STT-сервис не занял порт {port} — пробую следующий"
            self._stop_locked()
        self._last_error = last_error or "Не удалось поднять STT-сервис"
        return False

    def _stop_locked(self) -> None:
        proc = self._proc
        server_pid = self._server_pid
        self._proc = None
        self._port = None
        self._ready = False
        self._server_pid = None
        # Closing the job kills the whole sidecar tree immediately (KILL_ON_JOB_CLOSE).
        self._close_job_locked()
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=5.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=3.0)
                except Exception:
                    pass
        # The launcher shim may not take its child down with it: kill the real server too.
        if server_pid and (proc is None or server_pid != proc.pid):
            self._terminate_pid(server_pid)
        self._close_log_locked()

    # -- job object helpers (caller must hold the lock) ---------------------
    def _create_job_locked(self) -> bool:
        self._close_job_locked()
        kernel32 = _win_kernel32()
        if kernel32 is None:
            return False
        try:
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return False
            info = _JobExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            configured = kernel32.SetInformationJobObject(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.addressof(info),
                ctypes.sizeof(info),
            )
            if not configured:
                kernel32.CloseHandle(handle)
                return False
            self._job_handle = int(handle)
            return True
        except Exception:  # noqa: BLE001 - best effort, never break STT
            return False

    def _assign_to_job_locked(self, proc: subprocess.Popen | None) -> bool:
        if not self._job_handle or proc is None:
            return False
        kernel32 = _win_kernel32()
        if kernel32 is None:
            return False
        try:
            process_handle = getattr(proc, "_handle", None)
            if not process_handle:
                return False
            return bool(kernel32.AssignProcessToJobObject(self._job_handle, int(process_handle)))
        except Exception:  # noqa: BLE001
            return False

    def _assign_pid_to_job_locked(self, pid: int | None) -> bool:
        """Assign a process by pid — needed for the real interpreter behind the venv shim."""
        if not self._job_handle or not pid:
            return False
        kernel32 = _win_kernel32()
        if kernel32 is None:
            return False
        process_handle = None
        try:
            process_handle = kernel32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid)
            )
            if not process_handle:
                return False
            return bool(kernel32.AssignProcessToJobObject(self._job_handle, process_handle))
        except Exception:  # noqa: BLE001
            return False
        finally:
            if process_handle:
                try:
                    kernel32.CloseHandle(process_handle)
                except Exception:  # noqa: BLE001
                    pass

    def _close_job_locked(self) -> None:
        handle = self._job_handle
        self._job_handle = None
        if not handle:
            return
        kernel32 = _win_kernel32()
        if kernel32 is None:
            return
        try:
            kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass

    def _open_log_locked(self) -> None:
        path = self.log_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Keep the sidecar log bounded: truncate instead of rotating on every spawn.
            if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
                with open(path, "wb"):
                    pass
            self._log_handle = open(path, "ab")
        except OSError:
            self._log_handle = subprocess.DEVNULL

    def _close_log_locked(self) -> None:
        handle = self._log_handle
        self._log_handle = None
        if handle is not None and handle is not subprocess.DEVNULL:
            try:
                handle.close()
            except OSError:
                pass

    @staticmethod
    def _free_port() -> int:
        """First free port in 3211..3299: bind on 127.0.0.1, then release it.

        No SO_REUSEADDR here: on Windows it would let us bind a port another
        process is already listening on and report it as free.
        """
        for port in range(PORT_FIRST, PORT_LAST + 1):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    continue
                return port
        raise RuntimeError("Нет свободного порта для STT в диапазоне 3211–3299")

    def _health_matches_locked(self, payload: dict | None) -> bool:
        """True when /health belongs to the server this service started."""
        pid = self._payload_pid(payload)
        return pid is None or self._server_pid is None or pid == self._server_pid

    @staticmethod
    def _matches_request_locked(payload: dict, model: str, language: str) -> bool:
        """Reject a /health answer that reports a different model/language than requested."""
        reported_model = str(payload.get("model") or "").strip().lower()
        if reported_model and reported_model != model.strip().lower():
            return False
        reported_language = str(payload.get("language") or "").strip().lower()
        if reported_language and reported_language != (language.strip().lower() or "auto"):
            return False
        return True

    @staticmethod
    def _payload_pid(payload: dict | None) -> int | None:
        value = (payload or {}).get("pid")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return None

    @staticmethod
    def _terminate_pid(pid: int) -> None:
        """Terminate a process by pid (needed when the venv python is a launcher shim)."""
        if pid <= 0:
            return
        if os.name != "nt":
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            return
        kernel32 = _win_kernel32()
        if kernel32 is None:
            return
        handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, int(pid))
        if not handle:
            return
        try:
            kernel32.TerminateProcess(handle, 1)
        finally:
            try:
                kernel32.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass

    def _wait_health_locked(self, timeout_s: float = 8.0) -> dict | None:
        """Wait until the freshly spawned service answers /health at all."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return None
            payload = self._get_health_locked(0.5)
            if payload is not None:
                return payload
            time.sleep(0.05)
        return None

    # -- HTTP helpers ------------------------------------------------------
    def _get_health_locked(self, timeout_s: float) -> dict | None:
        port = self._port
        if port is None:
            return None
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=max(0.5, float(timeout_s))
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _post_json_locked(self, port: int, path: str, body: dict, timeout_s: float) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=max(1.0, float(timeout_s))) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Некорректный ответ сервиса распознавания")
        return payload

    @staticmethod
    def _http_error_detail(exc: urllib.error.HTTPError) -> str:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            payload = json.loads(raw)
            if isinstance(payload, dict) and payload.get("error"):
                return str(payload["error"])
        except Exception:
            pass
        return f"HTTP {exc.code}"
