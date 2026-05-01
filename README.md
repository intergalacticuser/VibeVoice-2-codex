# VibeVoice Codex

Open-source voice control layer for Codex built on top of Microsoft's MIT-licensed VibeVoice runtime.

> We wanted Codex to talk.
>  
> So we wired it into VibeVoice, taught it how to follow the active chat, gave it a menu bar, queue controls, shutdown controls, Apple Silicon support, and enough runtime discipline to survive real coding sessions.
>  
> During testing we also discovered a cursed bonus behavior: on some long outputs the model occasionally ends an otherwise normal sentence by transforming into a full infernal shriek from the underworld. If you have seen that too, please let us know, because it is objectively hilarious and deeply unsettling.

This project turns Codex into a spoken coding assistant:

- live voice for `Codex Desktop` replies via an active-chat watcher
- direct voice mode for `codex exec --json`
- Apple Silicon MLX fallback for low-latency local playback
- menu bar controls on macOS
- English-only desktop speech mode
- queue controls for stopping, skipping, muting, and shutting the watcher down

## What This Repo Contains

This repository packages the VibeVoice runtime together with an open-source Codex voice bridge we built around it.

Main entry points:

- `scripts/bootstrap.sh`
  Creates the Python environments and installs the required dependencies.
- `scripts/start_desktop_voice.sh`
  Starts the desktop watcher and opens the macOS menu bar helper.
- `scripts/stop_desktop_voice.sh`
  Stops the watcher, unloads the model, and closes the menu bar helper.
- `scripts/codex_desktop_voice.sh`
  Watches the active Codex Desktop session and speaks new output.
- `scripts/codex_voice_bridge.sh`
  Voices `codex exec --json` directly, without a desktop watcher.
- `scripts/codex_voice_panel.sh`
  Attaches the menu bar helper to the current live bridge session.
- `scripts/vibevoice_speak.sh`
  Standalone text-to-speech.

## Best Supported Setup

The most polished path today is:

- macOS
- Apple Silicon
- Codex Desktop
- `mlx-community/VibeVoice-Realtime-0.5B-4bit`

Linux and the official Microsoft websocket demo path are still available, but the Apple MLX backend is the fastest and most reliable path for desktop speech in this repo.

## Quick Start

From the repo root:

```bash
./scripts/bootstrap.sh --backend apple
```

Start the desktop watcher stack:

```bash
./scripts/start_desktop_voice.sh
```

Stop everything and unload the model:

```bash
./scripts/stop_desktop_voice.sh
```

## Direct Codex CLI Mode

If you want speech directly from `codex exec --json` instead of the desktop watcher:

```bash
./scripts/codex_voice_bridge.sh --prompt "Explain the repository architecture."
```

Enable the menu bar helper for that run:

```bash
./scripts/codex_voice_bridge.sh --panel --prompt "Summarize the current task."
```

## Standalone Speech

```bash
./scripts/vibevoice_speak.sh --text "Hello from VibeVoice Codex."
```

## How It Works

### 1. Desktop watcher

`tools/codex_desktop_voice.py` follows the active Codex Desktop workspace and tails the live rollout JSONL for the active thread. It converts visible assistant output, safe reasoning summaries, and status updates into speech items.

### 2. Voice bridge

`tools/codex_vibevoice.py` runs the queue, control API, runtime settings, and speech lifecycle. It is responsible for queue control, interruption policy, backend selection, and worker shutdown.

### 3. Speech backends

- `official`: Microsoft's websocket demo server with `microsoft/VibeVoice-Realtime-0.5B`
- `apple`: MLX backend with `mlx-community/VibeVoice-Realtime-0.5B-4bit`

### 4. Menu bar controls

On macOS, `macos/CodexVoiceMenuBar.swift` exposes:

- channel toggles
- desktop speech mode selection
- speed control
- mute / read-through
- stop current
- stop all speech
- next item
- clear queue
- quit watcher and stop all processes
- quit menu only

## Desktop Speech Modes

- `live_fast`
  Lowest-latency path. Speaks visible desktop text directly.
- `english_full`
  Translates non-English desktop output to English before speech.
- `status_only`
  Lightweight status announcements instead of full message text.

## Safety and Privacy Notes

- Raw private chain-of-thought is not spoken.
- Only safe reasoning summaries are eligible for speech.
- Local runtime state is stored under `.codex-home/voice-bridge/`, which is ignored by git.
- Generated logs, cached models, menu bar builds, and local sessions are excluded from the open-source repo.

## Development

Run the local test suite:

```bash
./.venv/bin/python -m unittest discover -s tests
```

## Contributors

- [intergalacticuser](https://github.com/intergalacticuser) - project creator and maintainer

## Attribution

This project is built on top of [Microsoft VibeVoice](https://github.com/microsoft/VibeVoice), which is MIT licensed. This repository keeps the upstream license and adds the Codex voice bridge, desktop watcher, Apple MLX worker flow, and macOS menu bar controls as an open-source integration layer.

More background: [NOTICE.md](NOTICE.md), [docs/codex-vibevoice.md](docs/codex-vibevoice.md)
