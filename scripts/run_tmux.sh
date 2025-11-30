#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SESSION_NAME="${TMUX_SESSION:-gridbot}"
RUN_CMD="source .venv/bin/activate && python -m src.runner $* || { echo 'runner exited with code $?'; sleep 999999; }"

if [ ! -d ".venv" ]; then
  echo ".venv not found. Run ./scripts/setup_venv.sh first." >&2
  exit 1
fi

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  echo "Session '$SESSION_NAME' already running. Attach with: tmux attach -t $SESSION_NAME"
  tmux attach -t "$SESSION_NAME"
  exit 0
fi

tmux new-session -d -s "$SESSION_NAME" "$SHELL -lc '$RUN_CMD'"
echo "Started tmux session '$SESSION_NAME'. Attach with: tmux attach -t $SESSION_NAME"
