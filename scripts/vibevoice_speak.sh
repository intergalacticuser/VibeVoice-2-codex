#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BACKEND="${VIBEVOICE_BACKEND:-auto}"
for ((i=1; i<=$#; i++)); do
  arg="${!i}"
  if [[ "$arg" == --backend=* ]]; then
    BACKEND="${arg#*=}"
  elif [[ "$arg" == "--backend" ]]; then
    next_index=$((i + 1))
    if (( next_index <= $# )); then
      BACKEND="${!next_index}"
    fi
  fi
done

if [[ "$BACKEND" == "auto" ]]; then
  if [[ "$(uname -s)" == "Darwin" ]]; then
    BACKEND="apple"
  else
    BACKEND="official"
  fi
fi

if [[ "$BACKEND" == "apple" ]]; then
  PYTHON="$ROOT/.venv-mlx/bin/python"
else
  PYTHON="$ROOT/.venv/bin/python"
fi

exec "$PYTHON" -m tools.vibevoice_speak "$@"
