# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-11-07

### Added

- **Session history** — every dictation is stored in `sessions.json` (time, duration, model,
  edited text). The new History window lists, searches, opens, edits, copies and deletes
  sessions; the store keeps the last 300 sessions (configurable 20–1000) and migrates the old
  `history.json` format automatically.
- **Local post-processing** — after dictation the app fixes spaces, dashes (`—`), sentence
  capitalisation, terminal punctuation and safe commas (conjunctions, introductory words,
  «который…»), plus a set of common grammar slip-ups. Everything is undoable with `Ctrl+Z`.
- **Contested words** — the post-processor flags suspicious fragments (`-ться/-тся`, merged
  «не», Latin letters inside Cyrillic words, frequent typos, very short words) and offers
  correction variants in a review dialog; nothing is changed without confirmation.
- **«Improve text» via DeepSeek API** — one click sends the current (edited) transcript to
  DeepSeek and replaces it with the improved version. Presets (Мягко / Стандарт / Сильно) and
  sliders for strength and quality; API key, model (`deepseek-chat` / `deepseek-reasoner`) and
  a «Проверить ключ» button live in the new settings section. Requests are made only when the
  button is pressed; the key is kept in `settings.json` and never logged.
- Settings sections `07 / Постобработка` and `08 / Улучшение текста`; new keys
  `autoPostprocess`, `suggestContested`, `sessionsLimit`, `llmApiKey`, `llmModel`, `llmPreset`,
  `llmStrength`, `llmQuality`.
- `tests/test_llm.py` (30 checks) and CI step for the LLM client; post-processing and history
  covered by new core tests (49 total).

### Changed

- Version bump to **0.2.0** (app, launcher, install entry).
- README/README.ru document history, post-processing and the optional DeepSeek integration.

## [0.1.0] — 2026-11-07

First public release.

### Added

- Global hotkey dictation: `Ctrl+Alt+Space` starts/stops recording from any application.
- Live transcript: text streams into the floating window while you speak (partial
  faster-whisper passes every ~0.9 s, accurate final pass after a pause).
- Editable transcript: fix the text in the window and copy/paste exactly the edited version.
- "New" button: clear the text and start a new dictation (also while recording).
- Copy on `Enter`, paste into the previously active window on `Ctrl+Enter`, cancel on `Esc`.
- Floating window: drag it anywhere (position remembered), pin it as an always-on-top widget,
  switch between compact / normal / wide layouts.
- Settings window with sections for recognition (model, device, beam size, VAD, language,
  model reload), microphone, appearance (light/dark theme, accent, scale), hotkeys,
  behaviour (auto-copy, single line, history, autostart) and About.
- Model control: choose tiny/base/small/medium/large-v3-turbo, force CUDA or CPU,
  reload the model without restarting the app.
- Safe settings: a busy hotkey never breaks the previous working hotkey; the settings
  window explains the conflict and offers a free combination. One-click reset.
- Portable mode: unpack the ZIP anywhere, the launcher bootstraps its own Python
  environment (`uv`) and keeps settings/history/logs inside the folder.
- Native `WinDictate.exe` manager: `--start`, `--stop`, `--restart`, `--status`,
  `--settings`, `--open`, `--autostart`, `--version`, `--help`.
- Word download free: models are loaded from the local HuggingFace cache; no telemetry.

### Technical

- Local STT sidecar (`server/stt_server.py`, stdlib HTTP + faster-whisper) on a free
  127.0.0.1 port (3211–3299); child process is placed in a Windows Job Object with
  `KILL_ON_JOB_CLOSE`, so it never survives a crash of the main app.
- Single-instance application, autostart via `HKCU\...\Run`, uninstall entry in
  "Programs and Features".
- UI follows the СТОРИАРТ design system (canon 27.10.2026): warm paper / graphite,
  terracotta accent, 1 px hairlines, square corners, Onest typeface.
