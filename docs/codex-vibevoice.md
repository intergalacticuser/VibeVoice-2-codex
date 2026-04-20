# Codex + VibeVoice Realtime Bridge

This workspace now includes a launcher-based Codex voice bridge with a macOS menu bar control panel and a fallback floating panel.

## What is included

- `scripts/prefetch_vibevoice_model.sh`
  Downloads and warms the selected backend model.
- `scripts/run_vibevoice_server.sh`
  Starts the official Microsoft websocket demo server on port `3000`.
- `scripts/vibevoice_speak.sh`
  Standalone text-to-speech client for arbitrary text, files, or stdin.
- `scripts/codex_speak.sh`
  Compatibility launcher for the new Codex voice bridge.
- `scripts/codex_voice_bridge.sh`
  Primary bridge launcher for `codex exec --json`.
- `scripts/codex_voice_everything.sh`
  Launcher preset that speaks visible answer deltas, completed answers, safe reasoning summaries, and status updates.
- `scripts/codex_desktop_voice.sh`
  Watches the current live Codex Desktop session JSONL and voices new commentary/final messages automatically.
- `scripts/start_desktop_voice.sh`
  Starts the desktop watcher in the background and opens the macOS menu bar helper.
- `scripts/stop_desktop_voice.sh`
  Stops the active desktop watcher and unloads the speech backend.
- `scripts/codex_voice_panel.sh`
  Attaches the macOS menu bar panel to the active bridge session.

## Backends

- `official`
  Uses Microsoft's upstream websocket demo with `microsoft/VibeVoice-Realtime-0.5B`.
- `apple`
  Uses MLX on Apple Silicon with `mlx-community/VibeVoice-Realtime-0.5B-4bit`.
- `auto`
  Picks `apple` on macOS and `official` elsewhere.

## Voice channels

The bridge exposes four independent speech channels:

- `assistant_deltas`
  Speaks visible text while Codex is still generating it.
- `assistant_completed`
  Speaks the final completed item if delta mode is off or no delta speech was emitted.
- `reasoning_summary`
  Speaks only safe `summaryTextDelta` reasoning summaries.
- `status_announcements`
  Speaks short state changes such as planning, running tool, writing answer, or turn completed.

Raw `item/reasoning/textDelta` is not spoken.

## Important behavior

- Default interrupt policy is `finish_current`.
- By default, if a long plan is being read and a new Codex action begins, the current speech keeps going until the current item finishes or you stop it manually from the panel.
- If you switch the panel to `interrupt_latest`, the current speech is interrupted automatically and old buffered plan text is dropped so it does not come back later from `flush_all` or `item/completed`.
- `read_through` temporarily disables automatic interruptions while `interrupt_latest` is selected.
- The bridge does not try to extract private chain-of-thought.

## One-time setup

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -e '.[streamingtts]' websockets fastapi uvicorn
python3.11 -m venv .venv-mlx
.venv-mlx/bin/python -m pip install --upgrade pip setuptools wheel
.venv-mlx/bin/python -m pip install mlx-audio
scripts/prefetch_vibevoice_model.sh --backend official
scripts/prefetch_vibevoice_model.sh --backend apple
```

## Standalone speech

On Apple Silicon, this now defaults to the MLX backend:

```bash
scripts/vibevoice_speak.sh --text "Hello from Apple MLX VibeVoice."
```

Force the official Microsoft server path:

```bash
scripts/vibevoice_speak.sh --backend official --auto-start-server --text "Hello from the upstream server."
```

Save to a wav file:

```bash
scripts/vibevoice_speak.sh --text "Save this to wav." --output outputs/test.wav --no-play
```

## Bridge usage

Default launcher:

```bash
scripts/codex_voice_bridge.sh --prompt "Explain the architecture of this repository."
```

Everything audible preset:

```bash
scripts/codex_voice_everything.sh --panel --prompt "Explain the architecture of this repository."
```

Compatibility launcher:

```bash
scripts/codex_speak.sh --prompt "Explain the architecture of this repository."
```

Live desktop-thread auto-voice:

```bash
scripts/codex_desktop_voice.sh
```

Convenience start/stop helpers:

```bash
scripts/start_desktop_voice.sh
scripts/stop_desktop_voice.sh
```

This watches the active `Codex Desktop` chat in `~/.codex/sessions/...jsonl` and automatically voices new assistant commentary/final messages from the thread that currently owns the desktop focus. That is the path to use when you want normal Codex desktop replies to speak by default without manually forwarding each message to TTS.

The desktop watcher now starts headless by default for lower system overhead. If you want the menu bar controls too, launch them separately:

```bash
scripts/codex_voice_panel.sh
```

The desktop watcher now follows the active `Codex Desktop` workspace/thread automatically instead of staying pinned to the launch thread. It resolves the active workspace from `~/.codex/.codex-global-state.json`, maps that workspace to the newest matching thread in `~/.codex/state_5.sqlite`, and then tails that thread's rollout JSONL with a reactive file-change wakeup on macOS.

For debugging and inspection, the desktop watcher also writes an English-normalized mirror stream under `.codex-home/voice-bridge/desktop-mirror/<thread-id>.jsonl`.

Launch the macOS menu bar panel together with Codex:

```bash
scripts/codex_voice_bridge.sh --panel --prompt "Summarize this repository."
```

Attach the menu bar panel to an already running bridge session:

```bash
scripts/codex_voice_panel.sh
```

Force Apple backend explicitly:

```bash
scripts/codex_voice_bridge.sh --backend apple --prompt "Summarize the repository in plain English."
```

Speak safe reasoning summaries too:

```bash
scripts/codex_voice_bridge.sh \
  --speak-reasoning-summary \
  --prompt "Refactor the logging setup and explain the changes."
```

If you want "everything" spoken, this preset enables:

- visible assistant deltas
- completed/final assistant text
- safe reasoning summaries
- status announcements

It still does not speak raw private chain-of-thought.

For the live desktop watcher, "thinking process" means:

- safe reasoning summaries when they are present in the session log
- status announcements such as `Working.`, `Planning.`, and `Running tool.`

Raw private chain-of-thought is still not spoken.

Speak only completed items and disable streaming deltas:

```bash
scripts/codex_voice_bridge.sh \
  --no-speak-assistant-deltas \
  --speak-assistant-completed \
  --prompt "Give me the final answer only."
```

Pass a fully explicit Codex command after `--`:

```bash
scripts/codex_voice_bridge.sh -- \
  codex exec --json --color never --skip-git-repo-check "Summarize this repo."
```

## Panel controls

On macOS, the panel now lives in the menu bar as a status-item dropdown. Outside macOS, the old floating panel remains the fallback.

The panel currently supports:

- Channel toggles for deltas, completed, reasoning summary, and status
- `Mute`
- `Read through`
- Voice selection
- Speed control
- Interrupt policy selection
- Desktop mode:
  - `live_fast`
  - `english_full`
  - `status_only`
- `Set Voice…`
- `Stop`
- `Stop All Speech`
- `Next`
- `Clear`
- `Quit Watcher and Stop All Processes`
- `Quit Menu Only`
- Current item preview and queued items

Settings are stored in `.codex-home/voice-bridge/settings.json`, and the active session is published in `.codex-home/voice-bridge/session.json`.

## Notes and limitations

- Microsoft notes that code, formulas, and uncommon symbols are not well supported by this model.
- The bridge therefore normalizes markdown and removes most code-like chunks before speech by default.
- For Apple Silicon, `auto` routes to MLX by default instead of the slower PyTorch `mps` path.
- The Apple worker is persistent, so the MLX model is loaded once and reused across speech items.
- `desktop_speech_mode=live_fast` keeps the real desktop text path and uses the most aggressive chunking/merge settings for lower latency.
- `desktop_speech_mode=english_full` keeps the full desktop-text path but translates non-English commentary, reasoning summaries, and final answers to English before they reach TTS.
- `desktop_speech_mode=status_only` keeps the desktop watcher in the lightweight status path. That mode intentionally says only condensed progress/status lines instead of full reply text.
- Active-chat following is workspace-aware and much better than the old pinned watcher, but it still resolves the focused chat heuristically from Desktop state files rather than a documented public API.
- The official websocket server can still be used, but its realtime behavior on Apple Silicon is much slower than the MLX fallback in this workspace.
- `microsoft/VibeVoice-1.5B` is a larger long-form TTS model, not a drop-in realtime replacement for Codex token speech.
