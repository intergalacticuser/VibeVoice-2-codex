#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONUNBUFFERED=1
export PYTORCH_ENABLE_MPS_FALLBACK="${PYTORCH_ENABLE_MPS_FALLBACK:-1}"
export TOKENIZERS_PARALLELISM="${TOKENIZERS_PARALLELISM:-false}"

PORT="${VIBEVOICE_PORT:-3000}"
MODEL="${VIBEVOICE_MODEL:-microsoft/VibeVoice-Realtime-0.5B}"
DEVICE="${VIBEVOICE_DEVICE:-mps}"

exec "$ROOT/.venv/bin/python" demo/vibevoice_realtime_demo.py \
  --port "$PORT" \
  --model_path "$MODEL" \
  --device "$DEVICE" \
  "$@"
