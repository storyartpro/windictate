# Architecture

```
┌───────────────────────────────────────────────────────────────────────────┐
│ WinDictate.exe (C# launcher, portable-aware)                              │
│   --start/--stop/--restart/--status/--settings/--open/--autostart          │
└───────────────┬───────────────────────────────────────────────────────────┘
                │ pythonw app.py --autostart
                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ app.py — DictateApp                                                       │
│  Tk root (hidden) · hotkeys · tray · settings · history · clipboard        │
│  ├─ dictate/ui.py        DictationWindow + SettingsWindow                  │
│  ├─ dictate/engine.py    streaming recording state machine                 │
│  ├─ dictate/audio.py     sounddevice PCM 16 kHz capture                    │
│  ├─ dictate/stt.py       STT sidecar manager (HTTP client + Job Object)    │
│  ├─ dictate/theme.py     design tokens, fonts, ttk styles                  │
│  ├─ dictate/hotkeys.py   global RegisterHotKey manager                     │
│  └─ dictate/config.py    settings.json schema / atomic save                │
└───────────────┬───────────────────────────────────────────────────────────┘
                │ POST /stt (base64 WAV, loopback)
                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ server/stt_server.py — faster-whisper sidecar (stdlib HTTPServer)          │
│  GET /health · POST /stt · POST /warmup — 127.0.0.1:3211…3299              │
│  CUDA float16 when available, CPU int8 fallback; Silero VAD optional      │
└───────────────────────────────────────────────────────────────────────────┘
```

## Streaming pipeline

`DictateEngine` owns one worker thread. `PcmRecorder` delivers mono float32 blocks from
the PortAudio callback; the engine resamples to 16 kHz and splits the stream into phrases
on pauses (`pauseMs`, default 800 ms):

- **partial passes** every ~900 ms of an active phrase (`beamSize: 1`, `vad: true`) render
  the "growing" draft;
- **final pass** after a pause (`beamSize` from settings) fixes the phrase and appends it,
  so committed phrases stay in order;
- **10-minute cap** (`maxRecordMs`), never auto-stops on silence;
- a generation counter drops stale responses after stop/cancel;
- transcription failures degrade to a soft notice — recording continues.

Constants: `PARTIAL_MS=900`, `MIN_PARTIAL_MS=800`, `MAX_SEGMENT_MS=20000`,
`SPEECH_RMS=0.018`.

## Sidecar lifecycle

- Lazy start on the first dictation; a free port from 3211–3299 is chosen by binding
  a socket (no `SO_REUSEADDR`), then the child is spawned with `CREATE_NO_WINDOW`.
- The launcher child (in Windows venv `python.exe` is a shim) and the real interpreter
  reported by `/health` are both assigned to a **Job Object** with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; closing the app kills the model process too.
- `/health` reports model, device, compute type, port, beam size and VAD state.

## Data locations

| Mode | Data |
|---|---|
| Installed (`scripts/install.ps1`) | `%LOCALAPPDATA%\WinDictate\{settings.json, history.json, logs\}` |
| Portable (ZIP) | `<folder>\data\{settings.json, history.json, logs\}`; Python env in `<folder>\runtime` |
| Models | HuggingFace cache (`%USERPROFILE%\.cache\huggingface`) |

The app sets `WIN_DICTATE_DATA` for portable mode; `dictate/config.py` honours it.

## Privacy

Audio never leaves the machine. The sidecar listens on loopback only; the app makes no
outbound network requests. Models are downloaded once by the user's HuggingFace cache
when they are not present.
