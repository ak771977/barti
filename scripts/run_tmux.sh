#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SESSION_NAME="${TMUX_SESSION:-gridbot}"
RUN_CMD="source .venv/bin/activate && python -m src.runner $* || { echo 'runner exited with code $?'; sleep 999999; }"
TMUX_TMPDIR="${TMUX_TMPDIR:-$ROOT_DIR/.tmux-tmp}"
mkdir -p "$TMUX_TMPDIR"

if [ ! -d ".venv" ]; then
  echo ".venv not found. Run ./scripts/setup_venv.sh first." >&2
  exit 1
fi

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  echo "Session '$SESSION_NAME' already running. Attaching..."
  tmux attach -t "$SESSION_NAME"
  exit 0
fi

if TMUX_TMPDIR="$TMUX_TMPDIR" tmux new-session -d -s "$SESSION_NAME" "$SHELL -lc '$RUN_CMD'"; then
  echo "Started tmux session '$SESSION_NAME'. Attach with: TMUX_TMPDIR=\"$TMUX_TMPDIR\" tmux attach -t $SESSION_NAME"
  exit 0
fi

echo "tmux unavailable here; running in foreground. Press Ctrl+C to stop." >&2
exec bash -lc "$RUN_CMD"
