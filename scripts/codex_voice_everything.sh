#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

exec "$ROOT/.venv/bin/python" -m tools.codex_vibevoice \
  --speak-assistant-deltas \
  --speak-assistant-completed \
  --speak-reasoning-summary \
  --speak-status-announcements \
  --interrupt-policy finish_current \
  "$@"
