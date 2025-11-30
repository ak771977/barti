#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f ".env" ]; then
  echo "Warning: .env not found. Create it with BINANCE_LIVE_API_KEY/BINANCE_LIVE_API_SECRET and BINANCE_TESTNET_API_KEY/BINANCE_TESTNET_API_SECRET before running." >&2
fi

./scripts/setup_venv.sh
source .venv/bin/activate

exec ./scripts/run_tmux.sh "$@"
