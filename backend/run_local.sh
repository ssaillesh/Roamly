#!/usr/bin/env bash
# Native (no-Docker) launcher for the Sway planner backend on macOS.
# Requires: Homebrew postgresql + redis running, Python 3.10+.
#
#   brew services start postgresql@18
#   brew services start redis
#   createdb sway   # once
#
# Usage:
#   ./run_local.sh setup     # create venv, install deps, migrate
#   ./run_local.sh api       # run the API on :8001
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
VENV=".venv"
PORT="${PORT:-8001}"

ensure_venv() {
  [ -d "$VENV" ] || "$PY" -m venv "$VENV"
}

case "${1:-setup}" in
  setup)
    ensure_venv
    "$VENV/bin/pip" install --upgrade pip -q
    "$VENV/bin/pip" install -r requirements.txt
    [ -f .env ] || cp .env.example .env
    "$VENV/bin/alembic" upgrade head
    echo "✅ Setup complete. Run './run_local.sh api'."
    ;;
  api)
    "$VENV/bin/uvicorn" app.main:app --host 127.0.0.1 --port "$PORT" --reload
    ;;
  *)
    echo "Unknown command: $1"; exit 1;;
esac
