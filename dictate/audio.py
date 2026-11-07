"""Audio capture helpers for WinDictate.

PcmRecorder wraps ``sounddevice.InputStream`` and delivers mono float32 blocks to
``on_block(samples, sample_rate)`` from the PortAudio callback thread. The rest of
the pipeline (STT) always works at ``TARGET_RATE`` = 16 kHz mono, so the recorder
keeps the device's native rate and the caller resamples via ``resample_linear``.
"""

from __future__ import annotations

import io
import threading
import wave
from typing import Callable

import numpy as np

# A block is treated as speech when its RMS is at or above this threshold.
SPEECH_RMS = 0.018
TARGET_RATE = 16000


# --------------------------------------------------------------------------- recorder


class PcmRecorder:
    """Microphone recorder producing float32 mono blocks.

    ``start()`` first tries 16 kHz (what the model wants); if the device rejects that
    rate (``PortAudioError``) it falls back to the device default samplerate. The
    ``sample_rate`` property always reports the rate the callback really receives.
    """

    def __init__(
        self,
        device: int | None,
        on_block: Callable[[np.ndarray, int], None],
        blocksize_ms: int = 100,
    ) -> None:
        self._device = device
        self._on_block = on_block
        self._blocksize_ms = max(10, int(blocksize_ms))
        self._lock = threading.Lock()
        self._stream = None
        self._rate = TARGET_RATE

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Open the input stream; raises RuntimeError if no rate/device works."""
        import sounddevice as sd  # lazy: importing PortAudio is not free

        with self._lock:
            if self._stream is not None:
                return
            last_error: Exception | None = None
            candidates: list[int] = [TARGET_RATE]
            default_rate = self._device_default_rate(sd)
            if default_rate and default_rate != TARGET_RATE:
                candidates.append(default_rate)
            for rate in candidates:
                stream = None
                try:
                    # Set the rate before start(): the PortAudio callback may fire immediately.
                    self._rate = int(rate)
                    stream = sd.InputStream(
                        device=self._device,
                        channels=1,
                        dtype="float32",
                        samplerate=int(rate),
                        blocksize=max(1, int(rate * self._blocksize_ms / 1000)),
                        callback=self._callback,
                    )
                    stream.start()
                except Exception as exc:  # PortAudioError / device busy / invalid rate
                    last_error = exc
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                    continue
                self._stream = stream
                return
            self._rate = TARGET_RATE
            raise RuntimeError(f"Не удалось открыть микрофон: {last_error}")

    def stop(self) -> None:
        """Stop and close the stream. Idempotent, never raises."""
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    # -- helpers -----------------------------------------------------------
    def _device_default_rate(self, sd) -> int | None:  # noqa: ANN001 - sounddevice module
        try:
            if self._device is not None:
                info = sd.query_devices(self._device, "input")
            else:
                info = sd.query_devices(kind="input")
            return int(info.get("default_samplerate") or 0) or None
        except Exception:
            return None

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001, ARG002
        # Runs on the PortAudio thread: never let an exception escape into C code.
        try:
            block = np.asarray(indata, dtype=np.float32).reshape(-1).copy()
            callback = self._on_block
            if callback is not None and block.size:
                callback(block, self._rate)
        except Exception:
            pass

    @property
    def sample_rate(self) -> int:
        return self._rate


# --------------------------------------------------------------------------- pure helpers


def resample_linear(samples: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Resample mono float32 audio with linear interpolation (not FFT)."""
    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    if from_rate <= 0 or to_rate <= 0:
        raise ValueError("sample rates must be positive")
    if from_rate == to_rate or data.size == 0:
        return data.copy()
    out_len = max(1, int(round(data.size * float(to_rate) / float(from_rate))))
    positions = np.arange(out_len, dtype=np.float64) * (float(from_rate) / float(to_rate))
    positions = np.clip(positions, 0.0, float(data.size - 1))
    source = np.arange(data.size, dtype=np.float64)
    return np.interp(positions, source, data.astype(np.float64)).astype(np.float32)


def encode_wav_pcm16(samples: np.ndarray, rate: int = TARGET_RATE) -> bytes:
    """Encode mono float32 audio as a standard 16-bit PCM RIFF/WAVE file."""
    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    pcm = np.clip(data, -1.0, 1.0)
    pcm = np.rint(pcm * 32767.0).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(int(rate))
        writer.writeframes(pcm.tobytes())
    return buffer.getvalue()


def rms(samples: np.ndarray) -> float:
    """Root-mean-square level of the block (0.0 for empty input)."""
    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    if data.size == 0:
        return 0.0
    values = data.astype(np.float64)
    return float(np.sqrt(np.mean(values * values)))
