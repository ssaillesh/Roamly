#!/usr/bin/env sh
# Production entrypoint: apply DB migrations, then start the API. (The old
# TrekRank background worker, badge seeding and trip healing are gone — nothing
# in Roamly needs a worker; the planner caches in Redis directly.)
set -e

# Log which database host this deploy will use (host only — never the password)
# and where the value came from, so a stale DATABASE_URL is obvious in the logs.
python - <<'EOF' || true
import os
from urllib.parse import urlparse
from app.config import settings
src = "DATABASE_URL env var" if os.environ.get("DATABASE_URL") else ".env file or built-in default (DATABASE_URL env var NOT set)"
print(f"Database host: {urlparse(settings.database_url).hostname} (from {src})")
EOF

echo "Running database migrations..."
alembic upgrade head

echo "Starting API on port ${PORT:-8000}..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
