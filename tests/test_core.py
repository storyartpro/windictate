"""Headless tests for the WinDictate core (audio/stt/engine).

No real microphone, no real sidecar, no network: ``PcmRecorder`` is replaced by a
fake recorder factory injected into the engine and ``SttService`` by a fake that
records the calls and returns canned text.

Run:  <venv-python> tests\\test_core.py   (prints PASS n/n, exit code 0)
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave
from contextlib import contextmanager
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # Russian event texts in failure dumps should not crash a legacy Windows console.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from dictate import audio as audio_mod  # noqa: E402
from dictate import engine as engine_mod  # noqa: E402
from dictate.audio import PcmRecorder, encode_wav_pcm16, resample_linear, rms  # noqa: E402
from dictate.engine import DictationEngine  # noqa: E402
from dictate.stt import PORT_FIRST, PORT_LAST, SttService  # noqa: E402

TESTS: list = []


def test(func):
    TESTS.append(func)
    return func


# --------------------------------------------------------------------------- doubles


class Events:
    """Thread-safe event sink with a wait-for-predicate helper."""

    def __init__(self) -> None:
        self._items: list[dict] = []
        self._cond = threading.Condition()

    def __call__(self, event: dict) -> None:
        with self._cond:
            self._items.append(event)
            self._cond.notify_all()

    def all(self) -> list[dict]:
        with self._cond:
            return list(self._items)

    def of(self, kind: str) -> list[dict]:
        return [event for event in self.all() if event.get("type") == kind]

    def wait(self, predicate, timeout: float = 2.0):
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for event in self._items:
                    if predicate(event):
                        return event
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)


class FakeRecorder:
    """Records what the engine asks of it; tests feed blocks by hand."""

    def __init__(self, device, on_block, blocksize_ms: int = 100) -> None:
        self.device = device
        self.on_block = on_block
        self.blocksize_ms = blocksize_ms
        self.started = False
        self.stopped = False
        self.stop_count = 0
        self.sample_rate = 16000

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stop_count += 1
        self.stopped = True

    def feed(self, samples, rate: int = 16000) -> None:
        if not self.started or self.stopped:
            return
        self.on_block(np.asarray(samples, dtype=np.float32), rate)


class FakeSTT:
    """Canned STT: partials (beam_size == 1) and finals (no overrides)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[dict] = []
        self.partial_text = "черновик"
        self.finals = ["финал"]
        self.final_index = 0
        self.fail_partial = False
        self.fail_final = False
        self.delay = 0.0
        self.final_delay = 0.0

    def transcribe(self, wav_bytes, language=None, beam_size=None, vad=None, timeout_s=30.0):
        is_partial = beam_size == 1
        with self.lock:
            self.calls.append({"wav": wav_bytes, "beam_size": beam_size, "vad": vad})
        delay = self.delay if is_partial else self.final_delay
        if delay:
            time.sleep(delay)
        if is_partial and self.fail_partial:
            raise RuntimeError("stt down")
        if not is_partial and self.fail_final:
            raise RuntimeError("stt down")
        if is_partial:
            return {"text": self.partial_text}
        with self.lock:
            if not self.finals:
                return {"text": ""}
            index = min(self.final_index, len(self.finals) - 1)
            text = self.finals[index]
            self.final_index += 1
        return {"text": text}

    def final_calls(self) -> list[dict]:
        return [call for call in self.calls if call["beam_size"] is None]

    def partial_calls(self) -> list[dict]:
        return [call for call in self.calls if call["beam_size"] == 1]


# --------------------------------------------------------------------------- helpers

BLOCK_MS = 100


def make_speech(seconds: float, amplitude: float = 0.2, rate: int = 16000) -> list[np.ndarray]:
    frames = int(rate * BLOCK_MS / 1000)
    count = max(1, int(round(seconds * 1000 / BLOCK_MS)))
    return [np.full(frames, amplitude, dtype=np.float32) for _ in range(count)]


def make_silence(seconds: float, rate: int = 16000) -> list[np.ndarray]:
    frames = int(rate * BLOCK_MS / 1000)
    count = max(1, int(round(seconds * 1000 / BLOCK_MS)))
    return [np.zeros(frames, dtype=np.float32) for _ in range(count)]


def feed(recorder: FakeRecorder, blocks: list[np.ndarray], rate: int = 16000, pace: float = 0.01) -> None:
    for block in blocks:
        recorder.feed(block, rate)
        if pace:
            time.sleep(pace)


def make_settings(**overrides) -> dict:
    base = {
        "device": None,
        "language": "ru",
        "model": "small",
        "beamSize": 5,
        "pauseMs": 300,
        "maxRecordMs": 600000,
        "singleLine": True,
    }
    base.update(overrides)
    return base


@contextmanager
def fast_timing(partial_ms: int = 120, min_partial_ms: int = 100, max_segment_ms: int = 20000):
    """Shrink streaming intervals so tests stay well under the 15 s budget."""
    saved = (engine_mod.PARTIAL_MS, engine_mod.MIN_PARTIAL_MS, engine_mod.MAX_SEGMENT_MS)
    engine_mod.PARTIAL_MS = partial_ms
    engine_mod.MIN_PARTIAL_MS = min_partial_ms
    engine_mod.MAX_SEGMENT_MS = max_segment_ms
    try:
        yield
    finally:
        engine_mod.PARTIAL_MS, engine_mod.MIN_PARTIAL_MS, engine_mod.MAX_SEGMENT_MS = saved


def make_engine(settings: dict, stt: FakeSTT, events: Events):
    recorders: list[FakeRecorder] = []

    def factory(device, on_block, blocksize_ms: int = 100):
        recorder = FakeRecorder(device, on_block, blocksize_ms)
        recorders.append(recorder)
        return recorder

    engine = DictationEngine(stt, settings, events, recorder_factory=factory)
    return engine, recorders


def finish(engine: DictationEngine) -> None:
    """Never leave a recording running after a failed assertion."""
    try:
        if engine.state != "idle":
            engine.cancel()
    except Exception:
        pass


def decoded_wav(data: bytes) -> tuple[int, int, np.ndarray]:
    with wave.open(io.BytesIO(data), "rb") as reader:
        rate = reader.getframerate()
        frames = reader.getnframes()
        channels = reader.getnchannels()
        width = reader.getsampwidth()
        raw = reader.readframes(frames)
    assert channels == 1 and width == 2, (channels, width)
    return rate, frames, np.frombuffer(raw, dtype=np.int16)


# --------------------------------------------------------------------------- tests: constants / audio


@test
def test_contract_constants() -> None:
    assert engine_mod.PARTIAL_MS == 900, engine_mod.PARTIAL_MS
    assert engine_mod.MIN_PARTIAL_MS == 800, engine_mod.MIN_PARTIAL_MS
    assert engine_mod.MAX_SEGMENT_MS == 20000, engine_mod.MAX_SEGMENT_MS
    assert engine_mod.MAX_IN_FLIGHT_PARTIALS == 1
    assert engine_mod.SPEECH_RMS == 0.018, engine_mod.SPEECH_RMS
    assert audio_mod.SPEECH_RMS == 0.018, audio_mod.SPEECH_RMS
    assert engine_mod.FINAL_TIMEOUT_S == 15.0, engine_mod.FINAL_TIMEOUT_S


@test
def test_resample_linear() -> None:
    source = np.full(4800, 0.5, dtype=np.float32)
    output = resample_linear(source, 48000, 16000)
    assert output.size == 1600, output.size
    assert np.allclose(output, 0.5, atol=1e-5)

    ramp = np.linspace(0.0, 1.0, 100, dtype=np.float32)
    assert np.array_equal(resample_linear(ramp, 16000, 16000), ramp)
    assert resample_linear(np.zeros(0, dtype=np.float32), 48000, 16000).size == 0

    up = resample_linear(np.arange(10, dtype=np.float32), 16000, 32000)
    assert up.size == 20, up.size


@test
def test_encode_wav_pcm16() -> None:
    samples = np.full(16000, 0.5, dtype=np.float32)
    data = encode_wav_pcm16(samples, 16000)
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    rate, frames, pcm = decoded_wav(data)
    assert rate == 16000 and frames == 16000, (rate, frames)
    assert pcm.dtype == np.int16 and pcm.size == 16000
    assert abs(int(pcm[0]) - 16384) <= 2, int(pcm[0])

    clipped = encode_wav_pcm16(np.array([2.0, -2.0], dtype=np.float32), 8000)
    _, frames, pcm = decoded_wav(clipped)
    assert frames == 2 and int(pcm.max()) == 32767 and int(pcm.min()) == -32767


@test
def test_rms_helper() -> None:
    assert rms(np.zeros(1600, dtype=np.float32)) == 0.0
    assert abs(rms(np.full(1600, 0.5, dtype=np.float32)) - 0.5) < 1e-6
    assert rms(np.zeros(0, dtype=np.float32)) == 0.0
    assert audio_mod.SPEECH_RMS == 0.018


@test
def test_pcm_recorder_stop_idempotent() -> None:
    recorder = PcmRecorder(device=None, on_block=lambda samples, rate: None)
    assert recorder.sample_rate == 16000
    recorder.stop()  # never started: must not raise
    recorder.stop()  # second call: must not raise


# --------------------------------------------------------------------------- tests: engine streaming


@test
def test_start_state_and_level_events() -> None:
    events, stt = Events(), FakeSTT()
    engine, recorders = make_engine(make_settings(pauseMs=60000), stt, events)
    try:
        engine.start()
        assert engine.state == "recording"
        assert events.wait(lambda e: e["type"] == "state" and e["state"] == "recording", 1.0)
        assert len(recorders) == 1
        feed(recorders[0], make_speech(0.5))
        level = events.wait(lambda e: e["type"] == "level" and e["rms"] > 0.018, 1.0)
        assert level is not None, events.all()
    finally:
        finish(engine)
    assert engine.state == "idle"


@test
def test_at_least_two_drafts_during_speech() -> None:
    events, stt = Events(), FakeSTT()
    engine, recorders = make_engine(make_settings(pauseMs=60000), stt, events)
    try:
        with fast_timing():
            engine.start()
            feed(recorders[0], make_speech(2.5), pace=0.01)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and len(events.of("draft")) < 2:
                time.sleep(0.01)
        drafts = events.of("draft")
        assert len(drafts) >= 2, [e["text"] for e in drafts]
        assert all(isinstance(draft["text"], str) for draft in drafts)
    finally:
        finish(engine)


@test
def test_final_replaces_partial() -> None:
    events, stt = Events(), FakeSTT()
    stt.partial_text = "черно"
    stt.finals = ["финал"]
    engine, recorders = make_engine(make_settings(pauseMs=120), stt, events)
    try:
        with fast_timing():
            engine.start()
            feed(recorders[0], make_speech(1.0), pace=0.01)
            partial = events.wait(lambda e: e["type"] == "draft" and "черно" in e["text"], 1.5)
            assert partial is not None, events.all()
            feed(recorders[0], make_silence(0.5), pace=0.01)
            final = events.wait(lambda e: e["type"] == "draft" and e["text"] == "финал", 2.0)
            assert final is not None, [e["text"] for e in events.of("draft")]
            drafts = [e["text"] for e in events.of("draft")]
            assert "черно" not in drafts[-1]
    finally:
        finish(engine)


@test
def test_pause_commits_phrase_with_final_quality() -> None:
    events, stt = Events(), FakeSTT()
    engine, recorders = make_engine(make_settings(pauseMs=120), stt, events)
    try:
        with fast_timing():
            engine.start()
            feed(recorders[0], make_speech(1.0), pace=0.01)
            feed(recorders[0], make_silence(0.5), pace=0.01)
            committed = events.wait(lambda e: e["type"] == "draft" and e["text"] == "финал", 2.0)
            assert committed is not None, events.all()
        final_calls = stt.final_calls()
        assert len(final_calls) == 1, final_calls
        assert final_calls[0]["beam_size"] is None and final_calls[0]["vad"] is None
        partial_calls = stt.partial_calls()
        assert partial_calls and all(c["vad"] is True and c["beam_size"] == 1 for c in partial_calls)
    finally:
        finish(engine)


@test
def test_final_order_and_done_text() -> None:
    events, stt = Events(), FakeSTT()
    stt.finals = ["один", "два"]
    engine, recorders = make_engine(make_settings(pauseMs=120), stt, events)
    try:
        with fast_timing():
            engine.start()
            feed(recorders[0], make_speech(1.0), pace=0.01)
            assert events.wait(lambda e: e["type"] == "draft" and "один" in e["text"], 2.0), events.all()
            feed(recorders[0], make_silence(0.5), pace=0.01)
            feed(recorders[0], make_speech(1.0), pace=0.01)
            assert events.wait(lambda e: e["type"] == "draft" and "два" in e["text"], 3.0), events.all()
            engine.stop()
        done = events.wait(lambda e: e["type"] == "done", 12.0)
        assert done is not None, events.all()
        assert done["text"] == "один два", done["text"]
        assert done["cancelled"] is False and done["error"] is None
    finally:
        finish(engine)


@test
def test_cap_stops_recording() -> None:
    events, stt = Events(), FakeSTT()
    engine, recorders = make_engine(make_settings(pauseMs=60000, maxRecordMs=250), stt, events)
    started = time.monotonic()
    try:
        with fast_timing():
            engine.start()
            feed(recorders[0], make_speech(3.0), pace=0.01)
            done = events.wait(lambda e: e["type"] == "done", 3.0)
        assert done is not None, events.all()
        assert done["cancelled"] is False
        assert engine.state == "idle"
        assert time.monotonic() - started < 3.0
        assert recorders[0].stop_count >= 1
    finally:
        finish(engine)


@test
def test_cancel_restores_base_and_ignores_late_answers() -> None:
    events, stt = Events(), FakeSTT()
    stt.partial_text = "мусор"
    stt.delay = 0.2  # partial answer arrives after cancel
    engine, recorders = make_engine(make_settings(pauseMs=60000), stt, events)
    try:
        with fast_timing():
            engine.start("база")
            feed(recorders[0], make_speech(1.0), pace=0.01)
            time.sleep(0.05)
            engine.cancel()
        done = events.wait(lambda e: e["type"] == "done", 1.0)
        assert done is not None, events.all()
        assert done["cancelled"] is True and done["text"] == "база", done
        drafts_after = len(events.of("draft"))
        time.sleep(0.4)  # let the stale STT answer come back
        assert len(events.of("draft")) == drafts_after
        assert len(events.of("done")) == 1
        assert "мусор" not in " ".join(e["text"] for e in events.of("draft"))
    finally:
        finish(engine)


@test
def test_soft_partial_error_does_not_kill_recording() -> None:
    events, stt = Events(), FakeSTT()
    stt.fail_partial = True
    stt.finals = ["итог"]
    engine, recorders = make_engine(make_settings(pauseMs=120), stt, events)
    try:
        with fast_timing():
            engine.start()
            feed(recorders[0], make_speech(1.0), pace=0.01)
            notice = events.wait(lambda e: e["type"] == "notice", 2.0)
            assert notice is not None, events.all()
            assert engine.state == "recording"
            feed(recorders[0], make_silence(0.5), pace=0.01)
            committed = events.wait(lambda e: e["type"] == "draft" and e["text"] == "итог", 2.0)
            assert committed is not None, events.all()
            engine.stop()
        done = events.wait(lambda e: e["type"] == "done", 12.0)
        assert done["text"] == "итог", done
    finally:
        finish(engine)


@test
def test_soft_final_error_still_produces_done() -> None:
    events, stt = Events(), FakeSTT()
    stt.fail_final = True
    engine, recorders = make_engine(make_settings(pauseMs=60000), stt, events)
    try:
        engine.start()
        feed(recorders[0], make_speech(1.0), pace=0.01)
        engine.stop()
        done = events.wait(lambda e: e["type"] == "done", 12.0)
        assert done is not None, events.all()
        assert done["text"] == "" and done["cancelled"] is False and done["error"] is None
        assert events.of("notice"), events.all()
    finally:
        finish(engine)


@test
def test_silence_yields_empty_done_without_stt_calls() -> None:
    events, stt = Events(), FakeSTT()
    engine, recorders = make_engine(make_settings(pauseMs=120), stt, events)
    try:
        engine.start()
        feed(recorders[0], make_silence(1.0), pace=0.01)
        engine.stop()
        done = events.wait(lambda e: e["type"] == "done", 12.0)
        assert done is not None, events.all()
        assert done["text"] == "" and done["error"] is None
        assert stt.calls == [], stt.calls
    finally:
        finish(engine)


@test
def test_single_line_joining() -> None:
    events, stt = Events(), FakeSTT()
    stt.finals = ["привет\n   мир"]
    engine, recorders = make_engine(make_settings(pauseMs=120, singleLine=True), stt, events)
    try:
        with fast_timing():
            engine.start()
            feed(recorders[0], make_speech(1.0), pace=0.01)
            feed(recorders[0], make_silence(0.5), pace=0.01)
            engine.stop()
        done = events.wait(lambda e: e["type"] == "done", 12.0)
        assert done["text"] == "привет мир", repr(done["text"])
    finally:
        finish(engine)


@test
def test_audio_always_resampled_to_16k() -> None:
    events, stt = Events(), FakeSTT()
    engine, recorders = make_engine(make_settings(pauseMs=60000), stt, events)
    try:
        with fast_timing():
            engine.start()
            # 100 ms at 48 kHz -> must reach STT as 16 kHz mono PCM16.
            block = np.full(4800, 0.2, dtype=np.float32)
            recorders[0].feed(block, 48000)
            call = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and call is None:
                calls = stt.partial_calls()
                call = calls[0] if calls else None
                if call is None:
                    time.sleep(0.01)
        assert call is not None, stt.calls
        rate, frames, _pcm = decoded_wav(call["wav"])
        assert rate == 16000, rate
        assert frames == 1600, frames
    finally:
        finish(engine)


@test
def test_state_sequence_and_elapsed_events() -> None:
    events, stt = Events(), FakeSTT()
    engine, recorders = make_engine(make_settings(pauseMs=60000), stt, events)
    try:
        engine.start()
        feed(recorders[0], make_speech(1.0), pace=0.02)
        assert events.wait(lambda e: e["type"] == "elapsed", 2.0), events.all()
        engine.stop()
        done = events.wait(lambda e: e["type"] == "done", 12.0)
        assert done is not None
        states = [e["state"] for e in events.of("state")]
        assert states[0] == "recording", states
        assert "finalizing" in states, states
        assert states[-1] == "idle", states
        assert done["duration_ms"] >= 0
    finally:
        finish(engine)


@test
def test_second_start_ignored_while_recording() -> None:
    events, stt = Events(), FakeSTT()
    engine, recorders = make_engine(make_settings(pauseMs=60000), stt, events)
    try:
        engine.start()
        engine.start()  # must be ignored
        assert len(recorders) == 1
        assert engine.state == "recording"
        assert len(events.of("state")) == 1, events.all()
    finally:
        finish(engine)


@test
def test_engine_reusable_after_done() -> None:
    events, stt = Events(), FakeSTT()
    stt.finals = ["первый", "второй"]
    engine, recorders = make_engine(make_settings(pauseMs=60000), stt, events)
    try:
        engine.start()
        feed(recorders[0], make_speech(0.4), pace=0.01)
        engine.stop()
        assert events.wait(lambda e: e["type"] == "done", 12.0) is not None
        engine.start("ещё")
        assert engine.state == "recording"
        feed(recorders[1], make_speech(0.4), pace=0.01)
        engine.stop()
        done = events.wait(lambda e: e["type"] == "done" and e["text"].startswith("ещё"), 12.0)
        assert done is not None, events.all()
    finally:
        finish(engine)


@test
def test_stop_is_idempotent_and_final_timeout_emits_done() -> None:
    events, stt = Events(), FakeSTT()
    stt.final_delay = 1.0
    saved_timeout = engine_mod.FINAL_TIMEOUT_S
    engine_mod.FINAL_TIMEOUT_S = 0.3
    engine, recorders = make_engine(make_settings(pauseMs=60000), stt, events)
    started = time.monotonic()
    try:
        engine.start()
        feed(recorders[0], make_speech(1.0), pace=0.01)
        engine.stop()
        elapsed = time.monotonic() - started
        first = events.wait(lambda e: e["type"] == "done", 2.0)
        assert first is not None, events.all()
        assert elapsed < 1.5, elapsed
        engine.stop()  # second stop must be a no-op
        assert len(events.of("done")) == 1
        assert events.wait(lambda e: e["type"] == "notice", 1.0) is not None
        assert engine.state == "idle"
    finally:
        engine_mod.FINAL_TIMEOUT_S = saved_timeout
        finish(engine)


@test
def test_stt_service_has_no_process_and_is_safe_when_stopped() -> None:
    service = SttService(make_settings(), python_exe=sys.executable, server_script=str(ROOT / "server" / "stt_server.py"))
    assert service.port is None
    assert service.health()["ok"] is False
    service.stop()  # never started: must not raise
    service.stop()
    service.restart()  # must not raise either


@test
def test_recorder_failure_emits_fatal_error() -> None:
    events, stt = Events(), FakeSTT()

    def broken_factory(device, on_block, blocksize_ms: int = 100):
        raise RuntimeError("нет микрофона")

    engine = DictationEngine(stt, make_settings(), events, recorder_factory=broken_factory)
    engine.start()
    error = events.wait(lambda e: e["type"] == "error", 1.0)
    assert error is not None, events.all()
    assert "микрофон" in error["text"]
    assert engine.state == "idle"


@test
def test_max_segment_commits_continuous_speech() -> None:
    events, stt = Events(), FakeSTT()
    stt.finals = ["длинно"]
    engine, recorders = make_engine(make_settings(pauseMs=60000), stt, events)
    try:
        with fast_timing(max_segment_ms=300):
            engine.start()
            feed(recorders[0], make_speech(1.5), pace=0.02)
            committed = events.wait(lambda e: e["type"] == "draft" and "длинно" in e["text"], 2.0)
            assert committed is not None, events.all()
        assert stt.final_calls(), stt.calls
    finally:
        finish(engine)


@test
def test_free_port_range_excludes_plugin_port() -> None:
    port = SttService._free_port()
    assert PORT_FIRST <= port <= PORT_LAST, port
    assert port != 3210, port


def _process_alive(pid: int) -> bool:
    """Windows: True while the process exists (used to prove Job Object cleanup)."""
    if os.name != "nt" or pid <= 0:
        return False
    import ctypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    api.OpenProcess.restype = ctypes.c_void_p
    api.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    api.GetExitCodeProcess.restype = ctypes.c_int
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.CloseHandle.restype = ctypes.c_int
    handle = api.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        api.CloseHandle(handle)


def _make_stt_service(**overrides) -> SttService:
    log_path = Path(tempfile.gettempdir()) / "windictate-test-stt.log"
    return SttService(
        make_settings(**overrides),
        python_exe=sys.executable,
        server_script=str(ROOT / "server" / "stt_server.py"),
        log_path=str(log_path),
    )


class FakePopen:
    """Minimal subprocess.Popen stand-in: records the command, never spawns anything."""

    instances: list["FakePopen"] = []
    _next_pid = 20000

    def __init__(self, command, **kwargs) -> None:
        self.command = list(command)
        self.kwargs = kwargs
        FakePopen._next_pid += 1
        self.pid = FakePopen._next_pid
        self._handle = 0
        self.returncode = None
        self.terminated = False
        self.killed = False
        FakePopen.instances.append(self)

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 1

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 1
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@contextmanager
def fake_sidecar_service(**settings_overrides):
    """SttService with faked Popen/port/health — no real sidecar process is started."""
    import dictate.stt as stt_mod

    service = _make_stt_service(**settings_overrides)
    saved_popen = stt_mod.subprocess.Popen
    ports = iter(range(3211, 3298))
    FakePopen.instances = []

    def fake_popen(command, **kwargs):
        return FakePopen(command, **kwargs)

    def fake_free_port():
        return next(ports)

    def fake_health_payload() -> dict:
        proc = service._proc
        return {
            "ok": True,
            "ready": True,
            "model": service._model(),
            "language": service._language(),
            "device": "cuda",
            "computeType": "float16",
            "pid": proc.pid if proc is not None else None,
            "beamSize": 5,
            "vad": True,
        }

    def fake_wait_health(timeout_s: float = 8.0):
        return fake_health_payload()

    def fake_get_health(timeout_s: float):
        return fake_health_payload()

    stt_mod.subprocess.Popen = fake_popen
    service._free_port = fake_free_port
    service._wait_health_locked = fake_wait_health
    service._get_health_locked = fake_get_health
    # Never touch real pids/jobs from a fake process table.
    service._assign_pid_to_job_locked = lambda pid: False
    service._terminate_pid = lambda pid: None
    try:
        yield service
    finally:
        stt_mod.subprocess.Popen = saved_popen
        try:
            service.stop()
        except Exception:  # noqa: BLE001
            pass


@test
def test_job_object_lifecycle() -> None:
    service = _make_stt_service()
    assert service._job_handle is None
    if os.name != "nt":
        # Non-Windows: the whole Job Object path is a soft no-op.
        assert service._create_job_locked() is False
        assert service._assign_to_job_locked(None) is False
        service._close_job_locked()
        return
    assert service._create_job_locked(), "CreateJobObjectW/SetInformationJobObject failed"
    first = service._job_handle
    assert first, "job handle not stored"
    assert service._assign_to_job_locked(None) is False
    # Recreating closes the previous handle and makes a fresh job (Windows may reuse
    # the numeric handle value immediately, so only the success/None state is checked).
    assert service._create_job_locked()
    assert service._job_handle
    # stop() without a spawned sidecar still closes the job handle.
    service._stop_locked()
    assert service._job_handle is None
    service._close_job_locked()  # idempotent
    assert service._job_handle is None


@test
def test_job_object_kills_assigned_processes() -> None:
    """KILL_ON_JOB_CLOSE must take down both the venv launcher shim and the real interpreter."""
    if os.name != "nt":
        return
    service = _make_stt_service()
    assert service._create_job_locked()
    real_pid = 0
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import os, time; print(os.getpid(), flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        real_pid = int(sleeper.stdout.readline().strip())
        assert real_pid > 0
        # The real interpreter (behind the launcher shim) is assigned by pid, the shim by handle.
        assert service._assign_pid_to_job_locked(real_pid), "AssignProcessToJobObject by pid failed"
        assert service._assign_to_job_locked(sleeper), "AssignProcessToJobObject by handle failed"
        service._close_job_locked()
        assert service._job_handle is None
        sleeper.wait(timeout=5)
        assert sleeper.poll() is not None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and _process_alive(real_pid):
            time.sleep(0.02)
        assert not _process_alive(real_pid), f"real interpreter pid {real_pid} survived job close"
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)
        if real_pid and _process_alive(real_pid):
            service._terminate_pid(real_pid)
        try:
            sleeper.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        service._close_job_locked()


@test
def test_stt_command_uses_stt_device_and_vad() -> None:
    with fake_sidecar_service(sttDevice="cuda", vad=False) as service:
        assert service.ensure_ready(timeout_s=1.0)
        command = FakePopen.instances[-1].command
        assert command[command.index("--device") + 1] == "cuda"
        assert "--no-vad" in command and "--vad" not in command
        assert command[command.index("--model") + 1] == "small"
        assert command[command.index("--language") + 1] == "ru"
        assert command[command.index("--beam-size") + 1] == "5"
        assert service.state == "ready"
    with fake_sidecar_service(sttDevice="tpu", vad=True) as service:
        assert service.ensure_ready(timeout_s=1.0)
        command = FakePopen.instances[-1].command
        assert command[command.index("--device") + 1] == "auto"  # unknown -> auto
        assert "--vad" in command and "--no-vad" not in command
    with fake_sidecar_service(vad="false") as service:
        assert service.ensure_ready(timeout_s=1.0)
        assert "--no-vad" in FakePopen.instances[-1].command


@test
def test_stt_health_fields_and_state() -> None:
    with fake_sidecar_service() as service:
        assert service.state == "idle" and service.last_error is None
        idle = service.health()  # must not raise when the sidecar is down
        assert idle["ok"] is False and idle["state"] == "idle"
        assert idle["port"] is None and idle["model"] == "small"
        assert "last_error" in idle

        assert service.ensure_ready(timeout_s=1.0)
        health = service.health()
        for key in ("ok", "ready", "model", "device", "computeType", "port", "lang", "pid", "state"):
            assert key in health, key
        assert health["ok"] is True and health["ready"] is True
        assert health["state"] == "ready" and health["last_error"] is None
        assert health["model"] == "small"
        assert health["device"] == "cuda" and health["computeType"] == "float16"
        assert health["port"] == service.port and isinstance(health["port"], int)
        assert health["lang"] == "ru"
        assert isinstance(health["pid"], int)

        service.stop()
        assert service.state == "idle" and service.last_error is None
        stopped = service.health()
        assert stopped["ok"] is False and stopped["state"] == "idle"
        assert service._job_handle is None


@test
def test_stt_reload_restarts_sidecar_with_new_settings() -> None:
    with fake_sidecar_service(sttDevice="auto", vad=True) as service:
        assert service.ensure_ready(timeout_s=1.0)
        first_proc = FakePopen.instances[-1]
        first_port = service.port
        assert service._job_handle is not None

        changed = {
            "model": "base",
            "language": "en",
            "beamSize": 3,
            "sttDevice": "cpu",
            "vad": False,
        }
        assert service.reload(changed) is True

        second_proc = FakePopen.instances[-1]
        assert second_proc is not first_proc
        assert first_proc.terminated, "old sidecar was not stopped"
        assert service.port != first_port
        command = second_proc.command
        assert command[command.index("--model") + 1] == "base"
        assert command[command.index("--device") + 1] == "cpu"
        assert command[command.index("--language") + 1] == "en"
        assert command[command.index("--beam-size") + 1] == "3"
        assert "--no-vad" in command
        assert service.state == "ready"
        assert service._job_handle is not None  # Job Object was recreated for the new process
        assert service.health()["state"] == "ready"


@test
def test_stt_reload_failure_sets_last_error_and_never_raises() -> None:
    with fake_sidecar_service() as service:
        # The sidecar never reports health: reload must fail softly, not raise.
        service._wait_health_locked = lambda timeout_s=8.0: None
        service._get_health_locked = lambda timeout_s: None

        assert service.reload({"model": "medium"}) is False
        assert service.last_error, "last_error must explain the failure"
        assert service.state == "error"
        health = service.health()
        assert health["ok"] is False and health["state"] == "error"
        assert health["last_error"] == service.last_error
        assert service.port is None
        assert service._job_handle is None

        # A later good reload recovers the service and clears the error.
        import dictate.stt as stt_mod  # noqa: F401 - the context already faked Popen

        def good_wait(timeout_s: float = 8.0):
            proc = service._proc
            return {
                "ready": True,
                "model": service._model(),
                "language": service._language(),
                "pid": proc.pid if proc is not None else None,
            }

        service._wait_health_locked = good_wait
        service._get_health_locked = lambda timeout_s: good_wait()
        assert service.reload() is True
        assert service.state == "ready" and service.last_error is None


# --------------------------------------------------------------------------- runner


def main() -> int:
    passed = 0
    failures: list[tuple[str, BaseException]] = []
    for func in TESTS:
        name = func.__name__
        try:
            func()
        except BaseException as exc:  # noqa: BLE001 - report every failure, keep going
            failures.append((name, exc))
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"ok   {name}")
    total = len(TESTS)
    print(f"PASS {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
