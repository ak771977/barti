#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d ".venv" ]; then
  echo ".venv not found. Run ./scripts/setup_venv.sh first." >&2
  exit 1
fi

source .venv/bin/activate
python -m src.runner --config config/live.json --live "$@"
