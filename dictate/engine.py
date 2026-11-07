"""Streaming dictation state machine for WinDictate.

One worker thread owns the whole recording pipeline: it drains PCM blocks produced
by ``PcmRecorder``, tracks speech/silence, sends cheap partial transcriptions while
the user speaks and synchronously finalises each phrase after a pause. Doing the
STT calls in a single thread guarantees final order and keeps the UI thread free.

Events are plain dicts delivered through ``on_event`` from worker threads; the UI
must marshal them into the Tk thread itself. No Tk import lives here on purpose.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable

import numpy as np

from .audio import SPEECH_RMS, PcmRecorder, encode_wav_pcm16, resample_linear, rms

# Streaming constants (documented in docs/ARCHITECTURE.md).
PARTIAL_MS = 900
MIN_PARTIAL_MS = 800
MAX_SEGMENT_MS = 20000
MAX_IN_FLIGHT_PARTIALS = 1

TARGET_RATE = 16000
TICK_S = 0.02  # worker poll interval
FINAL_TIMEOUT_S = 15.0  # stop() waits this long for the final transcription
ELAPSED_EMIT_MS = 500
SOFT_NOTICE = "Распознавание сейчас недоступно — запись продолжается."
SOFT_NOTICE_INTERVAL_S = 3.0
FINAL_TIMEOUT_NOTICE = "Распознавание не успело завершиться — текст может быть неполным."


def _clean(text: object) -> str:
    return str(text or "").strip()


def _join_text(*parts: str) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip())


def _single_line(text: str) -> str:
    return " ".join(text.split())


class DictationEngine:
    """Records audio, streams partial text and produces the final transcript."""

    def __init__(
        self,
        stt,
        settings: dict,
        on_event: Callable[[dict], None],
        recorder_factory: Callable | None = None,
    ) -> None:
        self._stt = stt
        self._settings = settings if isinstance(settings, dict) else {}
        self._on_event = on_event
        # Test hook: recorder factory with the PcmRecorder(device, on_block) signature.
        self._recorder_factory = recorder_factory
        self._lock = threading.RLock()
        self._state = "idle"
        self._worker: threading.Thread | None = None
        self._recorder = None
        self._queue: queue.Queue | None = None
        self._cfg: dict = {}
        self._generation = 0
        self._base_text = ""
        self._committed = ""
        self._partial = ""
        self._last_final = ""
        self._chunks: list[np.ndarray] = []
        self._saw_speech = False
        self._last_voice_at = 0.0
        self._last_partial_at = 0.0
        self._segment_started_at = 0.0
        self._started_at = 0.0
        self._last_elapsed_emit = 0.0
        self._stop_requested = False
        self._cancelled = False
        self._done_emitted = False
        self._last_notice_at = 0.0
        self._elapsed_ms = 0

    # -- public API --------------------------------------------------------
    def start(self, base_text: str = "") -> None:
        """Begin recording. Ignored unless the engine is idle."""
        with self._lock:
            if self._state != "idle":
                return
            self._cfg = dict(self._settings)
            self._generation += 1
            generation = self._generation
            self._base_text = base_text or ""
            self._committed = ""
            self._partial = ""
            self._last_final = ""
            self._chunks = []
            self._saw_speech = False
            now = time.monotonic()
            self._started_at = now
            self._last_voice_at = now
            self._last_partial_at = now - PARTIAL_MS / 1000.0
            self._segment_started_at = now
            self._last_elapsed_emit = now
            self._stop_requested = False
            self._cancelled = False
            self._done_emitted = False
            self._last_notice_at = 0.0
            self._elapsed_ms = 0
            self._queue = queue.Queue()
            factory = self._recorder_factory or PcmRecorder
            try:
                recorder = factory(self._cfg.get("device"), self._on_block)
            except Exception as exc:  # noqa: BLE001 - report the mic failure to the UI
                self._queue = None
                self._emit({"type": "error", "text": f"Не удалось открыть микрофон: {exc}"})
                return
            self._recorder = recorder
            self._state = "recording"
            self._emit({"type": "state", "state": "recording", "elapsed_ms": 0})
            try:
                recorder.start()
            except Exception as exc:  # noqa: BLE001
                self._state = "idle"
                self._recorder = None
                self._queue = None
                self._emit({"type": "error", "text": f"Не удалось открыть микрофон: {exc}"})
                return
            self._worker = threading.Thread(
                target=self._run, args=(generation,), name="dictation-engine", daemon=True
            )
            self._worker.start()

    def stop(self) -> None:
        """Stop capture and finalise the current phrase, emitting ``done``."""
        with self._lock:
            if self._state == "idle":
                return
            if self._state == "recording":
                self._elapsed_ms = int((time.monotonic() - self._started_at) * 1000)
                self._state = "finalizing"
                self._stop_requested = True
                self._emit({"type": "state", "state": "finalizing", "elapsed_ms": self._elapsed_ms})
            recorder = self._recorder
            worker = self._worker
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:
                pass
        if worker is not None and worker is not threading.current_thread():
            worker.join(FINAL_TIMEOUT_S)
            if worker.is_alive():
                self._force_done()
                worker.join(1.0)

    def cancel(self) -> None:
        """Discard everything and finish immediately, restoring the base text."""
        with self._lock:
            if self._state == "idle":
                return
            duration = self._elapsed_ms or int((time.monotonic() - self._started_at) * 1000)
            base = self._base_text
            self._cancelled = True
            self._generation += 1  # late STT answers are now ignored
            self._stop_requested = True
            self._state = "idle"
            self._elapsed_ms = duration
            recorder = self._recorder
            emit_done = not self._done_emitted
            self._done_emitted = True
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:
                pass
        if emit_done:
            self._emit({"type": "done", "text": base, "cancelled": True, "duration_ms": duration, "error": None})
            self._emit({"type": "state", "state": "idle", "elapsed_ms": duration})

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def elapsed_ms(self) -> int:
        with self._lock:
            if self._state == "recording":
                return int((time.monotonic() - self._started_at) * 1000)
            return self._elapsed_ms

    # -- capture callback --------------------------------------------------
    def _on_block(self, samples, sample_rate: int) -> None:  # noqa: ANN001 - numpy array
        with self._lock:
            q = self._queue
            if q is None or self._state != "recording":
                return
        try:
            q.put_nowait((samples, int(sample_rate)))
        except queue.Full:  # pragma: no cover - unbounded queue
            pass

    # -- worker ------------------------------------------------------------
    def _run(self, generation: int) -> None:
        try:
            while True:
                with self._lock:
                    if generation != self._generation or self._cancelled:
                        return
                    if self._stop_requested:
                        break
                try:
                    item = self._queue.get(timeout=TICK_S) if self._queue is not None else None
                except queue.Empty:
                    item = None
                now = time.monotonic()
                if item is not None:
                    self._ingest(generation, item, now)
                    with self._lock:
                        if generation != self._generation or self._cancelled:
                            return
                generation = self._tick(generation, now)
        finally:
            self._finalize(generation)

    def _ingest(self, generation: int, item: tuple, now: float) -> None:
        samples, rate = item
        block = np.asarray(samples, dtype=np.float32).reshape(-1)
        if block.size == 0:
            return
        level = rms(block)
        with self._lock:
            if generation != self._generation or self._state != "recording":
                return
            resampled = block if int(rate) == TARGET_RATE else resample_linear(block, int(rate), TARGET_RATE)
            self._chunks.append(resampled)
            if level >= SPEECH_RMS:
                self._saw_speech = True
                self._last_voice_at = now
        self._emit({"type": "level", "rms": round(min(1.0, level), 4)})

    def _tick(self, generation: int, now: float) -> int:
        """Run one housekeeping step; returns the (possibly bumped) generation."""
        with self._lock:
            if generation != self._generation or self._cancelled or self._stop_requested:
                return generation
            cfg = self._cfg
            elapsed_ms = int((now - self._started_at) * 1000)
            self._elapsed_ms = elapsed_ms
            max_record_ms = int(cfg.get("maxRecordMs") or 600000)
            pause_ms = int(cfg.get("pauseMs") or 800)
            segment_ms = self._segment_ms_locked()
            silence_ms = (now - self._last_voice_at) * 1000.0
            saw_speech = self._saw_speech

            # 1. hard cap: 10 minutes by default.
            if elapsed_ms >= max_record_ms:
                self._state = "finalizing"
                self._stop_requested = True
                self._emit({"type": "state", "state": "finalizing", "elapsed_ms": elapsed_ms})
                return generation

            # 2. phrase boundary -> commit the detached segment.
            if saw_speech and (silence_ms >= pause_ms or segment_ms >= MAX_SEGMENT_MS):
                audio = self._take_segment_locked(now)
                self._generation += 1
                new_generation = self._generation
            else:
                audio = None
                new_generation = generation

            # 3. cheap partial while the phrase is still growing.
            run_partial = (
                audio is None
                and saw_speech
                and segment_ms >= MIN_PARTIAL_MS
                and (now - self._last_partial_at) * 1000.0 >= PARTIAL_MS
            )
            partial_audio = self._segment_locked() if run_partial else None
            partial_generation = self._generation
            emit_elapsed = (now - self._last_elapsed_emit) * 1000.0 >= ELAPSED_EMIT_MS

        if audio is not None:
            self._run_final(new_generation, audio)
            return new_generation
        if partial_audio is not None:
            self._run_partial(partial_generation, partial_audio, now)
            return new_generation
        if emit_elapsed:
            with self._lock:
                self._last_elapsed_emit = now
            self._emit({"type": "elapsed", "elapsed_ms": elapsed_ms})
        return generation

    # -- transcription steps ----------------------------------------------
    def _run_partial(self, generation: int, audio: np.ndarray, sent_at: float) -> None:
        result = None
        failed = False
        try:
            result = self._stt.transcribe(encode_wav_pcm16(audio, TARGET_RATE), beam_size=1, vad=True)
        except Exception:  # noqa: BLE001 - soft failure: keep recording
            failed = True
        with self._lock:
            self._last_partial_at = max(self._last_partial_at, sent_at)
            if failed:
                self._notify_soft_locked(time.monotonic())
                return
            if generation != self._generation or self._cancelled or self._state != "recording":
                return  # generation guard: answer belongs to a stale segment
            self._partial = _clean((result or {}).get("text"))
            self._last_partial_at = time.monotonic()
            draft = self._draft_locked()
        self._emit({"type": "draft", "text": draft})

    def _run_final(self, generation: int, audio: np.ndarray) -> None:
        text = ""
        failed = False
        try:
            # Final quality: no beamSize/vad override (process defaults).
            result = self._stt.transcribe(encode_wav_pcm16(audio, TARGET_RATE))
            text = _clean((result or {}).get("text"))
        except Exception:  # noqa: BLE001 - soft failure: keep recording
            failed = True
        with self._lock:
            if generation != self._generation or self._cancelled or self._state == "idle":
                return
            if failed:
                self._notify_soft_locked(time.monotonic())
                return
            if text and text != self._last_final:
                self._committed = _join_text(self._committed, text)
                self._last_final = text
            self._partial = ""
            draft = self._draft_locked()
        self._emit({"type": "draft", "text": draft})

    def _finalize(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation or self._cancelled or self._done_emitted:
                return
            audio = self._segment_locked()
            saw_speech = self._saw_speech
            self._generation += 1
            duration_ms = int((time.monotonic() - self._started_at) * 1000)
            self._elapsed_ms = duration_ms
            state_changed = self._state != "finalizing"
            self._state = "finalizing"
            recorder = self._recorder
            single_line = bool(self._cfg.get("singleLine", True))
        if state_changed:
            self._emit({"type": "state", "state": "finalizing", "elapsed_ms": duration_ms})
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:
                pass
        text = ""
        failed = False
        if saw_speech and audio.size:
            try:
                result = self._stt.transcribe(encode_wav_pcm16(audio, TARGET_RATE))
                text = _clean((result or {}).get("text"))
            except Exception:  # noqa: BLE001 - final attempt may still fail softly
                failed = True
        with self._lock:
            if self._done_emitted or self._cancelled:
                return
            combined = _join_text(self._base_text, self._committed, text)
            final_text = _single_line(combined) if single_line else combined
            self._elapsed_ms = duration_ms
            self._state = "idle"
            self._done_emitted = True
        if failed:
            self._emit({"type": "notice", "text": SOFT_NOTICE})
        self._emit(
            {
                "type": "done",
                "text": final_text,
                "cancelled": False,
                "duration_ms": duration_ms,
                "error": None,
            }
        )
        self._emit({"type": "state", "state": "idle", "elapsed_ms": duration_ms})

    def _force_done(self) -> None:
        """Finalisation timed out: emit done with whatever text we already have."""
        with self._lock:
            if self._done_emitted:
                return
            duration_ms = self._elapsed_ms or int((time.monotonic() - self._started_at) * 1000)
            self._generation += 1
            combined = _join_text(self._base_text, self._committed, self._partial)
            final_text = _single_line(combined) if self._cfg.get("singleLine", True) else combined
            self._state = "idle"
            self._done_emitted = True
            self._elapsed_ms = duration_ms
        self._emit({"type": "notice", "text": FINAL_TIMEOUT_NOTICE})
        self._emit(
            {
                "type": "done",
                "text": final_text,
                "cancelled": False,
                "duration_ms": duration_ms,
                "error": None,
            }
        )
        self._emit({"type": "state", "state": "idle", "elapsed_ms": duration_ms})

    # -- segment helpers (caller must hold the lock) -----------------------
    def _segment_locked(self) -> np.ndarray:
        if not self._chunks:
            return np.zeros(0, dtype=np.float32)
        if len(self._chunks) == 1:
            return self._chunks[0]
        return np.concatenate(self._chunks)

    def _segment_ms_locked(self) -> int:
        return int(sum(chunk.size for chunk in self._chunks) * 1000 / TARGET_RATE)

    def _take_segment_locked(self, now: float) -> np.ndarray:
        audio = self._segment_locked()
        self._chunks = []
        self._saw_speech = False
        self._last_voice_at = now
        self._segment_started_at = now
        self._partial = ""
        return audio

    def _draft_locked(self) -> str:
        return _join_text(self._base_text, self._committed, self._partial)

    def _notify_soft_locked(self, now: float) -> None:
        if now - self._last_notice_at < SOFT_NOTICE_INTERVAL_S:
            return
        self._last_notice_at = now
        self._emit({"type": "notice", "text": SOFT_NOTICE})

    # -- events ------------------------------------------------------------
    def _emit(self, event: dict) -> None:
        callback = self._on_event
        if callback is None:
            return
        try:
            callback(event)
        except Exception:  # pragma: no cover - a broken UI handler must not kill capture
            pass
