#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

has_control_url=0
for arg in "$@"; do
  if [[ "$arg" == "--control-url" || "$arg" == --control-url=* ]]; then
    has_control_url=1
    break
  fi
done

if [[ "$has_control_url" -eq 0 ]]; then
  SESSION_PATH="$ROOT/.codex-home/voice-bridge/session.json"
  if [[ ! -f "$SESSION_PATH" ]]; then
    echo "No active voice bridge session found at $SESSION_PATH" >&2
    exit 1
  fi
  CONTROL_URL="$(
    python3 - <<'PY'
import json
from pathlib import Path

session_path = Path(".codex-home/voice-bridge/session.json")
try:
    payload = json.loads(session_path.read_text(encoding="utf-8"))
except Exception:
    print("")
else:
    print(str(payload.get("control_url") or "").strip())
PY
  )"
  if [[ -z "$CONTROL_URL" ]]; then
    echo "Could not resolve control_url from $SESSION_PATH" >&2
    exit 1
  fi
  set -- --control-url "$CONTROL_URL" "$@"
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
  APP="$("$ROOT/scripts/build_macos_voice_menu.sh")"
  exec open -na "$APP" --args "$@"
fi

exec "$ROOT/.venv/bin/python" -m tools.voice_bridge_panel "$@"
