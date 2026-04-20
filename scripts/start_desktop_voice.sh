#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p logs

nohup "$ROOT/scripts/codex_desktop_voice.sh" >> "$ROOT/logs/codex-desktop-voice.log" 2>&1 < /dev/null &
WATCHER_PID=$!

for _ in $(seq 1 80); do
  if [[ -f "$ROOT/.codex-home/voice-bridge/session.json" ]]; then
    break
  fi
  sleep 0.25
done

if [[ "$(uname -s)" == "Darwin" ]]; then
  "$ROOT/scripts/codex_voice_panel.sh" || true
fi

echo "Started Codex desktop voice watcher (pid $WATCHER_PID)."
