"""Live end-to-end smoke test for WinDictate (Lead integration).

Feeds a real speech WAV into the real DictationEngine + real SttService sidecar in
real time (no microphone), and verifies the streaming contract: live partial drafts,
final phrase replacement, non-empty transcript, no fatal errors.

Run:
    python tests/live_e2e.py [wav]
Exit code 0 when all checks pass.
"""

from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dictate import config  # noqa: E402
from dictate.audio import resample_linear  # noqa: E402
from dictate.engine import DictationEngine  # noqa: E402
from dictate.stt import SttService  # noqa: E402

TARGET_RATE = 16000
BLOCK = 1600  # 100 ms at 16 kHz


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    if width != 2:
        raise SystemExit(f"unsupported sample width: {width}")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != TARGET_RATE:
        samples = resample_linear(samples, rate, TARGET_RATE)
    return samples.astype(np.float32)


class FileRecorder:
    """Replays a WAV into the engine in real time; ignores the device argument."""

    def __init__(self, samples: np.ndarray, realtime: bool = True) -> None:
        self.samples = samples
        self.realtime = realtime
        self._stop = False
        self._thread = None
        self.blocks = 0

    def start(self) -> None:
        import threading

        def run() -> None:
            pos = 0
            while not self._stop and pos < self.samples.size:
                block = self.samples[pos:pos + BLOCK]
                pos += BLOCK
                self._on_block(block, TARGET_RATE)
                self.blocks += 1
                if self.realtime:
                    time.sleep(BLOCK / TARGET_RATE)

        self._thread = threading.Thread(target=run, daemon=True, name="file-recorder")
        self._thread.start()

    def _on_block(self, block: np.ndarray, rate: int) -> None:
        self.on_block(block, rate)

    def stop(self) -> None:
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python tests/live_e2e.py <speech.wav>  # any 3-30 s Russian speech file")
        return 2
    wav_path = Path(sys.argv[1])
    if not wav_path.is_file():
        print(f"FAIL: wav not found: {wav_path}")
        return 1
    samples = load_wav(wav_path)
    duration_s = samples.size / TARGET_RATE
    print(f"audio: {wav_path.name}, {duration_s:.1f} s")

    settings = dict(config.DEFAULTS)
    settings.update({"model": "small", "language": "ru", "beamSize": 5, "pauseMs": 800})
    stt = SttService(settings)
    if not stt.ensure_ready(timeout_s=120):
        print(f"FAIL: STT sidecar not ready: {stt._last_error}")
        return 1
    print(f"stt: ready on port {stt.port}")

    events: list[dict] = []
    drafts: list[str] = []
    notices: list[str] = []

    def on_event(event: dict) -> None:
        events.append(event)
        if event.get("type") == "draft":
            drafts.append(str(event.get("text", "")))
        elif event.get("type") == "notice":
            notices.append(str(event.get("text", "")))

    holder: dict = {}

    def recorder_factory(device, on_block):
        recorder = FileRecorder(samples)
        recorder.on_block = on_block
        holder["recorder"] = recorder
        return recorder

    engine = DictationEngine(stt, settings, on_event, recorder_factory=recorder_factory)
    started = time.monotonic()
    engine.start()
    deadline = time.monotonic() + duration_s + 8.0
    while time.monotonic() < deadline:
        done = any(e.get("type") == "done" for e in events)
        recorder = holder.get("recorder")
        if done:
            break
        if recorder is not None and recorder.blocks * BLOCK >= samples.size:
            time.sleep(1.2)  # let the last phrase commit / partial land
            break
        time.sleep(0.1)
    engine.stop()
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and not any(e.get("type") == "done" for e in events):
        time.sleep(0.1)

    done = next((e for e in events if e.get("type") == "done"), None)
    text = str((done or {}).get("text", "")).strip()
    errors = [e for e in events if e.get("type") == "error"]
    states = [e.get("state") for e in events if e.get("type") == "state"]

    checks: list[tuple[str, bool, str]] = []
    checks.append(("done received", done is not None, str(done)))
    checks.append(("no fatal errors", not errors, str(errors)))
    checks.append(("recording state emitted", states[:1] == ["recording"], str(states[:3])))
    checks.append(("live drafts >= 2", len([d for d in drafts if d]) >= 2, f"{len(drafts)} drafts"))
    checks.append(("draft grew before stop", len({d for d in drafts if d}) >= 2, str(drafts[:3])))
    checks.append(("final text non-empty", len(text) >= 20, text[:120]))
    checks.append(("final is single line", "\n" not in text, repr(text[:80])))
    checks.append(("duration sane", (done or {}).get("duration_ms", 0) > 3000, str((done or {}).get("duration_ms"))))
    checks.append(("soft notices only", all("недоступно" in n or "не успело" in n for n in notices), str(notices)))

    failed = 0
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
        if not ok:
            failed += 1

    print(f"\nfinal text:\n{text}\n")
    print(f"elapsed: {time.monotonic() - started:.1f} s; drafts={len(drafts)}; events={len(events)}")
    stt.stop()
    if failed:
        print(f"FAIL {failed}/{len(checks)}")
        return 1
    print(f"PASS {len(checks)}/{len(checks)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
