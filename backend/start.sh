#!/usr/bin/env sh
# Production entrypoint: apply DB migrations, then start the API.
# The planner runs entirely in-request (external venue APIs + Redis cache), so
# there is no background worker and no broker to keep alive.
set -e

echo "Running database migrations..."
alembic upgrade head

echo "Starting API on port ${PORT:-8000}..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
