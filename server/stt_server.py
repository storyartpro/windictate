"""Local faster-whisper STT sidecar for WinDictate (stdlib HTTP server).

Endpoints (JSON, served over loopback by WinDictate):

    GET  /health                -> {ok, engine, model, device, computeType, models, ready, loading,
                                    language, beamSize, vad, pid}
    POST /stt     {audioBase64, language?, beamSize?, vad?}
                                -> {text, language, languageProbability, audioSeconds, elapsedMs,
                                    beamSize, vad}
    POST /warmup  {model?}      -> load the model without a real transcription

Per-request ``beamSize`` (int 1..8) and ``vad`` (bool) override the process defaults for
that call only (live streaming dictation sends ``{beamSize: 1, vad: true}`` for cheap
partials); the response echoes the effective values. Invalid overrides are 400 JSON.

Recognition knobs (used by the "Распознавание" settings section):
    --beam-size N    decoder beam size 1..8 (default 5; env STT_BEAM_SIZE)
    --vad/--no-vad   Silero VAD filtering on/off (default on; env STT_VAD=0/false/no/off disables)

Run:
    python stt_server.py --port 3210 --model small --device auto --language ru --beam-size 5 --vad

Device "auto" first tries CUDA float16 and falls back to CPU int8 (also at runtime if the
CUDA libraries are missing or the GPU runs out of memory). Models are resolved from the local
HuggingFace cache with ``local_files_only=True`` first; a download is only attempted on a
cache miss, so a fully cached machine works with no network.

No third-party HTTP framework is used: only ``http.server`` + faster-whisper.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import glob
import json
import os
import re
import site
import sys
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Windows consoles/log files use legacy encodings; never die while printing Russian text.
try:  # pragma: no cover - platform nicety
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _add_nvidia_dll_dirs() -> list[str]:
    """Windows: CUDA DLLs from pip packages (nvidia-cublas-cu12 / nvidia-cudnn-cu12).

    ctranslate2 does not find ``cublas64_12.dll`` unless the directories from
    ``site-packages/nvidia/*/bin`` are added to the DLL search path; without them the
    sidecar would silently degrade to CPU. Returns the directories that were added.
    """
    if os.name != "nt":
        return []
    bases: list[str] = []
    try:
        bases.extend(site.getsitepackages())
    except AttributeError:  # broken virtualenv
        pass
    bases.append(os.path.join(sys.prefix, "Lib", "site-packages"))
    found: list[str] = []
    for base in dict.fromkeys(bases):
        for directory in glob.glob(os.path.join(base, "nvidia", "*", "bin")):
            if directory in found:
                continue
            found.append(directory)
            try:
                os.add_dll_directory(directory)
            except OSError:
                pass
            os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
    return found


NVIDIA_DLL_DIRS = _add_nvidia_dll_dirs()

# short name -> HuggingFace repo id
SUPPORTED_MODELS: dict[str, str] = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}
MODEL_ALIASES = {"turbo": "large-v3-turbo"}
REPO_TO_SHORT = {repo: short for short, repo in SUPPORTED_MODELS.items()}

MAX_BODY_BYTES = 64 * 1024 * 1024  # 64 MiB; 60 s of 16 kHz mono PCM16 is ~2 MB
DEFAULT_READY_TIMEOUT = 180.0
DEFAULT_LANGUAGE = "ru"
DEFAULT_BEAM_SIZE = 5
MIN_BEAM_SIZE = 1
MAX_BEAM_SIZE = 8
DEFAULT_VAD = True

_WS_RE = re.compile(r"\s+")
_SILENCE_PHRASES = re.compile(
    r"^(?:"
    r"продолжение следует|спасибо за (?:просмотр|внимание|подписку)|"
    r"субтитры (?:сделал|подготовил|подготовлены)|редактор субтитров|"
    r"подпишись на канал|ставьте лайки|подписывайтесь на канал|"
    r"thank you(?: for watching)?|thanks for watching|subtitles by|amara\.org|"
    r"\[?(?:music|музыка|аплодисменты|смех|пауза)\]?[.!]?"
    r")[.!]?$",
    re.IGNORECASE,
)


def log(*parts: object) -> None:
    print("[stt]", *parts, flush=True)


# --------------------------------------------------------------------------- model resolution


def resolve_repo(name: str) -> str:
    """Map ``tiny`` / ``turbo`` / a full repo id to a HuggingFace repo id."""
    clean = (name or "").strip().strip("/")
    if not clean:
        return SUPPORTED_MODELS["small"]
    key = clean.lower()
    key = MODEL_ALIASES.get(key, key)
    if key in SUPPORTED_MODELS:
        return SUPPORTED_MODELS[key]
    if "/" in clean:  # already a repo id
        return clean
    return f"Systran/faster-whisper-{clean}"


def model_label(name: str) -> str:
    clean = (name or "").strip()
    key = MODEL_ALIASES.get(clean.lower(), clean.lower())
    if key in SUPPORTED_MODELS:
        return key
    return REPO_TO_SHORT.get(clean, clean)


def _hf_hub_cache_dir() -> Path:
    explicit = os.environ.get("HF_HUB_CACHE")
    if explicit:
        return Path(explicit)
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _cached_repos() -> set[str]:
    cache = _hf_hub_cache_dir()
    try:
        entries = list(cache.iterdir())
    except OSError:
        return set()
    repos: set[str] = set()
    for entry in entries:
        name = entry.name
        if not name.startswith("models--"):
            continue
        repos.add(name[len("models--"):].replace("--", "/"))
    return repos


def available_models() -> list[str]:
    """Models already present in the local HF cache (all supported ids if detection fails)."""
    cached = _cached_repos()
    found = [short for short, repo in SUPPORTED_MODELS.items() if repo in cached]
    return found or list(SUPPORTED_MODELS)


# --------------------------------------------------------------------------- runtime state

_load_lock = threading.Lock()  # serializes model loading / swapping (never held by /health)
_transcribe_lock = threading.Lock()  # one transcription at a time (one GPU)
_load_done = threading.Event()

_model = None
_model_repo = ""
_model_label = ""
_device = "cpu"
_compute = "int8"
_ready = False
_loading = False
_load_error: str | None = None
_default_language: str | None = None
_requested_model = "small"
_requested_device = "auto"
_requested_compute = ""
_beam_size = DEFAULT_BEAM_SIZE
_vad_filter = DEFAULT_VAD


def _cuda_device_count() -> int:
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:  # missing DLL, no GPU, broken install — treated as "no CUDA"
        return 0


def _pick_device(pref: str, compute: str) -> tuple[str, str]:
    pref = (pref or "auto").strip().lower()
    compute = (compute or "").strip().lower()
    if pref.startswith("cpu"):
        return "cpu", compute or "int8"
    if pref.startswith("cuda") or pref.startswith("gpu"):
        return "cuda", compute or "float16"
    # auto
    if _cuda_device_count() > 0:
        return "cuda", compute or "float16"
    return "cpu", compute or "int8"


def _instantiate(repo: str, device: str, compute: str):
    from faster_whisper import WhisperModel

    started = time.perf_counter()
    try:
        model = WhisperModel(repo, device=device, compute_type=compute, local_files_only=True)
        log(f"loaded {repo} from cache device={device} compute={compute} in {time.perf_counter() - started:.2f}s")
        return model
    except Exception as cache_exc:  # noqa: BLE001 - cache miss is expected on a fresh machine
        log(f"cache load failed for {repo} ({type(cache_exc).__name__}: {cache_exc}); retrying with download")
        model = WhisperModel(repo, device=device, compute_type=compute, local_files_only=False)
        log(f"downloaded {repo} device={device} compute={compute} in {time.perf_counter() - started:.2f}s")
        return model


def _silent_wav(seconds: float = 0.3, rate: int = 16000) -> Path:
    fd, name = tempfile.mkstemp(prefix="stt_warm_", suffix=".wav")
    os.close(fd)
    path = Path(name)
    frames = int(rate * max(seconds, 0.05))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * frames)
    return path


def _warmup(model) -> None:  # noqa: ANN001
    """Force one tiny inference: catches missing CUDA DLLs / OOM before the first request."""
    path = _silent_wav()
    try:
        segments, _info = model.transcribe(str(path), language="en", beam_size=1, vad_filter=False)
        for _segment in segments:
            pass
    finally:
        _safe_unlink(path)


def _load_model(model_name: str, device_pref: str, compute_pref: str) -> None:
    """(Re)load the model. Caller must hold _load_lock."""
    global _model, _model_repo, _model_label, _device, _compute

    repo = resolve_repo(model_name)
    label = model_label(model_name)
    device, compute = _pick_device(device_pref, compute_pref)
    log(f"loading model={label} repo={repo} device={device} compute={compute}")
    started = time.perf_counter()

    model = _instantiate(repo, device, compute)
    used_device, used_compute = device, compute

    if used_device == "cuda":
        try:
            log("CUDA warmup...")
            _warmup(model)
        except Exception as exc:  # noqa: BLE001 - missing cublas/cudnn or OOM -> CPU
            log(f"CUDA unusable ({type(exc).__name__}: {exc}); falling back to CPU int8")
            try:
                del model
            except Exception:  # noqa: BLE001
                pass
            model = _instantiate(repo, "cpu", "int8")
            used_device, used_compute = "cpu", "int8"

    _model = model
    _model_repo = repo
    _model_label = label
    _device = used_device
    _compute = used_compute
    log(
        f"ready model={label} device={used_device} compute={used_compute} "
        f"load={time.perf_counter() - started:.2f}s cuda_dirs={len(NVIDIA_DLL_DIRS)}"
    )


def _load_model_guarded(model_name: str, device_pref: str, compute_pref: str) -> None:
    global _loading, _load_error, _ready
    with _load_lock:
        _loading = True
        _ready = False
        _load_error = None
        try:
            _load_model(model_name, device_pref, compute_pref)
            _ready = True
        except BaseException as exc:  # noqa: BLE001 - keep the sidecar alive and report it
            _load_error = f"failed to load model '{model_name}' ({resolve_repo(model_name)}): {type(exc).__name__}: {exc}"
            log(_load_error)
            raise
        finally:
            _loading = False
            _load_done.set()


def _initial_load_thread() -> None:
    try:
        _load_model_guarded(_requested_model, _requested_device, _requested_compute)
    except BaseException:  # noqa: BLE001 - already logged
        pass


def _wait_ready(timeout: float | None = None) -> bool:
    if _ready:
        return True
    _load_done.wait(timeout if timeout is not None else DEFAULT_READY_TIMEOUT)
    return _ready


def _fallback_to_cpu(reason: str) -> bool:
    """Reload the current model on CPU int8 after a CUDA runtime failure."""
    global _model, _ready
    with _load_lock:
        if _device != "cuda":
            return False
        log(f"runtime CUDA failure ({reason}); switching to CPU int8")
        try:
            old = _model
            _model = None
            _ready = False
            try:
                del old
            except Exception:  # noqa: BLE001
                pass
            _load_model(_model_label or _requested_model, "cpu", "int8")
            _ready = True
            return True
        except Exception as exc:  # noqa: BLE001
            _ready = False
            log(f"CPU fallback failed: {type(exc).__name__}: {exc}")
            return False


# --------------------------------------------------------------------------- text helpers


def _clean_text(parts: list[str]) -> str:
    text = " ".join(part.strip() for part in parts if part and part.strip())
    text = _WS_RE.sub(" ", text).strip()
    if not text:
        return ""
    if _SILENCE_PHRASES.match(text) and len(text) <= 80:
        return ""
    if not re.search(r"[0-9A-Za-zА-Яа-яЁё]", text):
        return ""
    return text


def _normalize_language(value: object, default: str | None) -> str | None:
    """None means "auto-detect"."""
    if value is None:
        preferred = default
    elif isinstance(value, str):
        preferred = value
    else:
        raise ValueError("language must be a string")
    if preferred is None:
        return None
    preferred = preferred.strip()
    if not preferred or preferred.lower() in ("auto", "detect", "any"):
        return None
    return preferred


def _normalize_beam_override(value: object) -> int | None:
    """Per-request beam size for POST /stt; None keeps the process default.

    Streaming partials send beamSize=1 to run cheap while final dictation keeps
    the --beam-size default. Anything that is not an int in 1..8 is rejected
    with a field-named message (the HTTP layer turns it into a 400 JSON error).
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"beamSize must be an integer within {MIN_BEAM_SIZE}..{MAX_BEAM_SIZE}")
    if not MIN_BEAM_SIZE <= value <= MAX_BEAM_SIZE:
        raise ValueError(f"beamSize must be an integer within {MIN_BEAM_SIZE}..{MAX_BEAM_SIZE}")
    return value


def _normalize_vad_override(value: object) -> bool | None:
    """Per-request VAD switch for POST /stt; None keeps the process default."""
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError("vad must be a boolean (true/false)")
    return value


def _wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate() or 1
            return wf.getnframes() / float(rate)
    except (wave.Error, EOFError, OSError):
        return None


def _audio_duration(path: Path) -> float | None:
    """Duration in seconds: WAV via the stdlib, anything else via av when available."""
    duration = _wav_duration(path)
    if duration is not None:
        return duration
    try:
        import av  # a faster-whisper dependency, always present next to it

        with av.open(str(path)) as container:
            if container.duration is not None:
                return float(container.duration) / 1_000_000.0
            stream = container.streams.audio[0] if container.streams.audio else None
            if stream is not None and stream.duration is not None and stream.time_base is not None:
                return float(stream.duration * stream.time_base)
    except Exception:  # noqa: BLE001 - unknown container / broken file -> let the model try
        return None
    return None


def _safe_unlink(path: Path, attempts: int = 5) -> None:
    """Windows keeps files locked briefly after the decoder closes them; retry instead of raising."""
    for index in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if index == attempts - 1:
                return
            time.sleep(0.05 * (index + 1))
        except OSError:
            return


# --------------------------------------------------------------------------- request handling


class SttHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "windictate-stt/0.1"
    timeout = 300  # seconds; protects against a client that never finishes sending

    # -- plumbing -----------------------------------------------------------
    def log_message(self, fmt: str, *args: object) -> None:  # noqa: D401 - succinct access log
        log(f"{self.address_string()} {fmt % args}")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            # A body without Content-Length is not something our client sends; treat as empty.
            return b""
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Content-Length header") from exc
        if length < 0:
            raise ValueError("invalid Content-Length header")
        if length > MAX_BODY_BYTES:
            raise OverflowError(f"request body too large (>{MAX_BODY_BYTES} bytes)")
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                raise ValueError("truncated request body")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_json(self, allow_empty: bool = False) -> dict:
        raw = self._read_body()
        if not raw:
            if allow_empty:
                return {}
            raise ValueError("JSON body is required")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    @staticmethod
    def _path_only(raw: str) -> str:
        from urllib.parse import urlsplit

        return urlsplit(raw).path.rstrip("/") or "/"

    # -- routes -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        try:
            path = self._path_only(self.path)
            if path in ("/health", "/"):
                self._send_json(200, _health_payload())
                return
            self._send_error_json(404, f"unknown path {path}")
        except Exception as exc:  # noqa: BLE001 - never crash the server on a request
            self._send_error_json(500, f"internal error: {type(exc).__name__}: {exc}")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        try:
            path = self._path_only(self.path)
            if path == "/stt":
                self._handle_stt()
            elif path == "/warmup":
                self._handle_warmup()
            else:
                self._send_error_json(404, f"unknown path {path}")
        except ValueError as exc:
            self._send_error_json(400, str(exc))
        except OverflowError as exc:
            self._send_error_json(413, str(exc))
        except Exception as exc:  # noqa: BLE001 - never crash the server on a request
            log(f"unhandled error: {type(exc).__name__}: {exc}")
            self._send_error_json(500, f"internal error: {type(exc).__name__}: {exc}")

    def _handle_warmup(self) -> None:
        try:
            payload = self._read_json(allow_empty=True)
        except ValueError:
            payload = {}
        requested = payload.get("model")
        if requested is not None and not isinstance(requested, str):
            self._send_error_json(400, "model must be a string")
            return
        target = requested.strip() if isinstance(requested, str) and requested.strip() else None
        if target and model_label(target) != _model_label:
            try:
                _load_model_guarded(target, _requested_device, _requested_compute)
            except Exception as exc:  # noqa: BLE001
                self._send_error_json(500, f"failed to load model {target}: {type(exc).__name__}: {exc}")
                return
        if not _wait_ready(0.5):
            self._send_error_json(503, "model is still loading")
            return
        self._send_json(200, {"ok": True, "model": _model_label, "device": _device, "computeType": _compute})

    def _handle_stt(self) -> None:
        payload = self._read_json()
        audio_value = payload.get("audioBase64")
        if audio_value is None:
            audio_value = payload.get("audio_base64")
        if audio_value is None:
            raise ValueError("audioBase64 is required (base64-encoded WAV/PCM audio)")
        if not isinstance(audio_value, str):
            raise ValueError("audioBase64 must be a string")
        try:
            language = _normalize_language(payload.get("language"), _default_language)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        # Optional per-request recognizer overrides (streaming partials run cheap).
        beam_override = _normalize_beam_override(payload.get("beamSize"))
        vad_override = _normalize_vad_override(payload.get("vad"))
        effective_beam = _beam_size if beam_override is None else beam_override
        effective_vad = _vad_filter if vad_override is None else vad_override

        encoded = audio_value.strip()
        if not encoded:  # explicit empty audio (e.g. the user stopped before speaking)
            self._send_json(
                200,
                {
                    "text": "",
                    "language": language,
                    "languageProbability": 0.0,
                    "audioSeconds": 0.0,
                    "elapsedMs": 0,
                    "beamSize": effective_beam,
                    "vad": effective_vad,
                },
            )
            return
        if encoded.startswith("data:") and "," in encoded:  # tolerate data URLs
            encoded = encoded.split(",", 1)[1]
        try:
            audio_bytes = base64.b64decode(encoded, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("audioBase64 is not valid base64") from exc

        started = time.perf_counter()
        if len(audio_bytes) < 64:
            self._send_json(
                200,
                {
                    "text": "",
                    "language": language,
                    "languageProbability": 0.0,
                    "audioSeconds": 0.0,
                    "elapsedMs": int((time.perf_counter() - started) * 1000),
                    "beamSize": effective_beam,
                    "vad": effective_vad,
                },
            )
            return

        suffix = ".wav" if audio_bytes[:4] == b"RIFF" else ".bin"
        fd, name = tempfile.mkstemp(prefix="stt_", suffix=suffix)
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(audio_bytes)
            duration = _audio_duration(path)
            if duration is not None and duration < 0.2:  # empty / too short for speech
                self._send_json(
                    200,
                    {
                        "text": "",
                        "language": language,
                        "languageProbability": 0.0,
                        "audioSeconds": round(duration, 3),
                        "elapsedMs": int((time.perf_counter() - started) * 1000),
                        "beamSize": effective_beam,
                        "vad": effective_vad,
                    },
                )
                return

            if not _wait_ready():
                self._send_error_json(503, f"model not ready: {_load_error or 'still loading'}")
                return

            with _transcribe_lock:
                try:
                    text, info, audio_seconds = _transcribe(path, language, beam_override, vad_override)
                except Exception as exc:  # noqa: BLE001 - CUDA runtime failure -> CPU retry
                    if _device == "cuda" and _fallback_to_cpu(f"{type(exc).__name__}: {exc}"):
                        text, info, audio_seconds = _transcribe(path, language, beam_override, vad_override)
                    else:
                        raise
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self._send_json(
                200,
                {
                    "text": text,
                    "language": getattr(info, "language", None) or language,
                    "languageProbability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 4),
                    "audioSeconds": round(float(audio_seconds or duration or 0.0), 3),
                    "elapsedMs": elapsed_ms,
                    "beamSize": effective_beam,
                    "vad": effective_vad,
                },
            )
        finally:
            _safe_unlink(path)


def _transcribe(path: Path, language: str | None, beam_size: int | None = None, vad_filter: bool | None = None):
    """One transcription pass. Caller must hold _transcribe_lock and a loaded model.

    ``beam_size`` / ``vad_filter`` are per-request overrides used by streaming
    partials; ``None`` means "use the process default" (--beam-size / --vad).
    """
    if _model is None:
        raise RuntimeError("model is not loaded")
    effective_beam = _beam_size if beam_size is None else beam_size
    effective_vad = _vad_filter if vad_filter is None else vad_filter
    segments, info = _model.transcribe(
        str(path),
        language=language,
        vad_filter=effective_vad,
        beam_size=effective_beam,
        condition_on_previous_text=False,
    )
    parts: list[str] = []
    for segment in segments:
        text = (getattr(segment, "text", "") or "").strip()
        if not text:
            continue
        no_speech = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
        log_prob = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
        if no_speech > 0.75 and log_prob < -1.0:  # VAD slipped: likely silence
            continue
        parts.append(text)
    text = _clean_text(parts)
    audio_seconds = float(getattr(info, "duration", 0.0) or 0.0)
    return text, info, audio_seconds


def _health_payload() -> dict:
    # Read current values without blocking: /health must answer while a model is loading.
    payload = {
        "ok": _load_error is None,
        "engine": "faster-whisper",
        "model": _model_label or model_label(_requested_model),
        "device": _device,
        "computeType": _compute,
        "models": available_models(),
        "ready": _ready,
        "loading": _loading,
        "language": _default_language or "auto",
        "beamSize": _beam_size,
        "vad": _vad_filter,
        "pid": os.getpid(),
    }
    if _load_error:
        payload["error"] = _load_error
    return payload


# --------------------------------------------------------------------------- entrypoint


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), SttHandler)
    server.daemon_threads = True
    return server


def _env_flag(name: str, default: bool) -> bool:
    """Boolean env knob: unset/empty -> default, 0/false/no/off/disabled -> False, else True."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "disabled")


def _beam_size_arg(value: object) -> int:
    try:
        size = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("beam size must be an integer") from exc
    if not MIN_BEAM_SIZE <= size <= MAX_BEAM_SIZE:
        raise argparse.ArgumentTypeError(f"beam size must be within {MIN_BEAM_SIZE}..{MAX_BEAM_SIZE}")
    return size


def _env_beam_size() -> int:
    raw = os.environ.get("STT_BEAM_SIZE", "")
    if raw.strip():
        try:
            return _beam_size_arg(raw)
        except argparse.ArgumentTypeError:
            log(f"WARNING: ignoring invalid STT_BEAM_SIZE={raw!r}; using {DEFAULT_BEAM_SIZE}")
    return DEFAULT_BEAM_SIZE


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local faster-whisper STT sidecar (127.0.0.1 only)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("STT_PORT", "3210")))
    parser.add_argument("--host", default=os.environ.get("STT_HOST", "127.0.0.1"))
    parser.add_argument("--model", default=os.environ.get("STT_MODEL", "small"))
    parser.add_argument("--device", default=os.environ.get("STT_DEVICE", "auto"), help="auto | cuda | cpu")
    parser.add_argument("--compute-type", default=os.environ.get("STT_COMPUTE_TYPE", ""), help="float16 | int8 | int8_float16 ...")
    parser.add_argument("--language", default=os.environ.get("STT_LANGUAGE", DEFAULT_LANGUAGE), help="ru | en | auto")
    parser.add_argument(
        "--beam-size",
        type=_beam_size_arg,
        default=_env_beam_size(),
        help=f"decoder beam size, {MIN_BEAM_SIZE}..{MAX_BEAM_SIZE} (default {DEFAULT_BEAM_SIZE})",
    )
    parser.add_argument(
        "--vad",
        action=argparse.BooleanOptionalAction,
        default=_env_flag("STT_VAD", DEFAULT_VAD),
        help="Silero VAD filtering before decoding (default: on; use --no-vad to disable)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global _requested_model, _requested_device, _requested_compute, _default_language
    global _beam_size, _vad_filter
    args = parse_args(argv)
    _requested_model = args.model
    _requested_device = args.device
    _requested_compute = args.compute_type or ""
    _default_language = _normalize_language(args.language, DEFAULT_LANGUAGE)
    _beam_size = args.beam_size
    _vad_filter = bool(args.vad)
    host = args.host or "127.0.0.1"
    if host not in ("127.0.0.1", "localhost", "::1"):
        log(f"WARNING: binding to {host} (expected 127.0.0.1 for a local-only sidecar)")

    log(
        f"starting on http://{host}:{args.port} model={model_label(_requested_model)} "
        f"device={args.device} language={_default_language or 'auto'} "
        f"beam_size={_beam_size} vad={_vad_filter} "
        f"models_available={available_models()} pid={os.getpid()}"
    )
    threading.Thread(target=_initial_load_thread, name="stt-loader", daemon=True).start()

    try:
        server = build_server(host, args.port)
    except OSError as exc:
        log(f"cannot bind {host}:{args.port}: {exc}")
        return 1
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("interrupted, shutting down")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
