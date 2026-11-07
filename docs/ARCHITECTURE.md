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
│  ├─ dictate/ui.py        DictationWindow + SettingsWindow + HistoryWindow   │
│  ├─ dictate/engine.py    streaming recording state machine                 │
│  ├─ dictate/audio.py     sounddevice PCM 16 kHz capture                    │
│  ├─ dictate/stt.py       STT sidecar manager (HTTP client + Job Object)    │
│  ├─ dictate/history.py   SessionStore (sessions.json, atomic writes)       │
│  ├─ dictate/postprocess.py  punctuation / grammar / contested words       │
│  ├─ dictate/llm.py       optional DeepSeek client («Улучшить»)             │
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
| Installed (`scripts/install.ps1`) | `%LOCALAPPDATA%\WinDictate\{settings.json, sessions.json, logs\}` |
| Portable (ZIP) | `<folder>\data\{settings.json, sessions.json, logs\}`; Python env in `<folder>\runtime` |
| Models | HuggingFace cache (`%USERPROFILE%\.cache\huggingface`) |

The app sets `WIN_DICTATE_DATA` for portable mode; `dictate/config.py` honours it.

## Session history

`dictate/history.py` stores completed dictations in `sessions.json` (atomic `.tmp` +
`os.replace`, threaded access): id, ISO timestamp, duration, chars, model, source, edited flag.
The store keeps the newest `sessionsLimit` entries (default 300) and imports the legacy
`history.json` list on first use. The UI writes the **post-processed** text after each session.

## Post-processing

`dictate/postprocess.py` is a local rule engine: whitespace/ellipsis normalisation, dashes,
sentence capitalisation, terminal punctuation, safe comma rules (conjunctions, introductory
words, «который…»), and a set of common grammar substitutions. It never touches numbers, URLs
or quoted fragments, and is idempotent. "Contested" fragments (`-ться/-тся`, merged «не»,
Latin letters inside Cyrillic words, frequent typos, very short words) are returned as
suggestions with variants; the UI shows them in a review dialog and applies only what the user
confirms.

## Optional DeepSeek improvement

`dictate/llm.py` talks to `https://api.deepseek.com/chat/completions` only when the user presses
«Улучшить» and an API key is configured. Presets (`light` / `standard` / `strong`) and strength /
quality sliders shape the system prompt and temperature; `deepseek-reasoner` is supported
(temperature omitted). Errors are surfaced as Russian messages; the key is stored in
`settings.json` and never logged.

## Privacy

Audio never leaves the machine. The sidecar listens on loopback only. The app makes no outbound
requests on its own: the one-time model download happens in the user's HuggingFace cache, and the
transcript is sent to the DeepSeek API only when the user explicitly presses «Улучшить» with a
configured key.
