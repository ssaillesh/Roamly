#!/usr/bin/env bash
# Native (no-Docker) launcher for the TrekRank backend on macOS.
# Requires: Homebrew postgresql + redis running, Python 3.10+.
#
#   brew services start postgresql@18
#   brew services start redis
#   createdb trekrank   # once
#
# Usage:
#   ./run_local.sh setup     # create venv, install deps, migrate, seed
#   ./run_local.sh api       # run the API on :8001
#   ./run_local.sh worker    # run the Celery worker
#   ./run_local.sh test      # run pytest
#   ./run_local.sh smoke     # run the live end-to-end smoke test
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
    "$VENV/bin/python" -m scripts.seed_badges
    echo "✅ Setup complete. Run './run_local.sh worker' and './run_local.sh api' in two terminals."
    ;;
  api)
    PUBLIC_BASE_URL="http://127.0.0.1:${PORT}" "$VENV/bin/uvicorn" app.main:app --host 127.0.0.1 --port "$PORT" --reload
    ;;
  worker)
    "$VENV/bin/celery" -A app.workers worker -l info --pool=solo
    ;;
  test)
    CELERY_TASK_ALWAYS_EAGER=1 "$VENV/bin/python" -m pytest -q
    ;;
  smoke)
    "$VENV/bin/python" -m scripts.smoke_test "http://127.0.0.1:${PORT}/api/v1"
    ;;
  *)
    echo "Unknown command: $1"; exit 1;;
esac
