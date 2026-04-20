#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SESSION_PATH="$ROOT/.codex-home/voice-bridge/session.json"
if [[ ! -f "$SESSION_PATH" ]]; then
  echo "No active voice session found."
  exit 0
fi

CONTROL_URL="$(
  python3 - <<'PY'
import json
from pathlib import Path

session = json.loads(Path(".codex-home/voice-bridge/session.json").read_text(encoding="utf-8"))
print(str(session.get("control_url") or "").strip())
PY
)"

if [[ -z "$CONTROL_URL" ]]; then
  echo "Could not resolve control URL from $SESSION_PATH" >&2
  exit 1
fi

python3 - <<'PY'
import json
import urllib.request
from pathlib import Path

session = json.loads(Path(".codex-home/voice-bridge/session.json").read_text(encoding="utf-8"))
req = urllib.request.Request(
    session["control_url"] + "/actions/shutdown",
    data=b"{}",
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=3):
    pass
PY

echo "Stopped the active desktop voice watcher."
