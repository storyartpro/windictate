# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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
