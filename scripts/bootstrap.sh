#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BACKEND="auto"
PREFETCH_MODELS=1

while (($#)); do
  case "$1" in
    --backend)
      BACKEND="${2:-auto}"
      shift 2
      ;;
    --backend=*)
      BACKEND="${1#*=}"
      shift
      ;;
    --skip-prefetch)
      PREFETCH_MODELS=0
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

pick_python() {
  local candidate
  for candidate in python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

PYTHON_BIN="$(pick_python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "No supported Python interpreter found. Install python3.10+ first." >&2
  exit 1
fi

echo "Using Python: $PYTHON_BIN"

"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -e '.[streamingtts]' websockets fastapi uvicorn

if [[ "$(uname -s)" == "Darwin" ]]; then
  "$PYTHON_BIN" -m venv .venv-mlx
  .venv-mlx/bin/python -m pip install --upgrade pip setuptools wheel
  .venv-mlx/bin/python -m pip install mlx-audio
fi

if [[ "$PREFETCH_MODELS" -eq 1 ]]; then
  if [[ "$BACKEND" == "auto" || "$BACKEND" == "official" ]]; then
    ./scripts/prefetch_vibevoice_model.sh --backend official
  fi
  if [[ "$(uname -s)" == "Darwin" && ( "$BACKEND" == "auto" || "$BACKEND" == "apple" ) ]]; then
    ./scripts/prefetch_vibevoice_model.sh --backend apple
  fi
fi

echo
echo "Bootstrap complete."
echo "Start desktop voice: ./scripts/start_desktop_voice.sh"
echo "Stop everything:     ./scripts/stop_desktop_voice.sh"
