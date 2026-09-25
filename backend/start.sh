#!/usr/bin/env sh
# Production entrypoint: apply DB migrations + seed badges (idempotent), heal any
# trips stuck in "processing", start the background worker, then start the API.
# A single Railway service runs both the API and the Celery worker, so trips are
# actually processed (geocoding + distance) instead of queuing forever.
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

# Seeding and healing are non-fatal and can be slow (healing geocodes each stuck
# trip via Nominatim at ~1 req/s), so run them in the background — the API must
# bind its port quickly or a cold start looks like an outage.
(
  echo "Seeding badges..."
  python -m scripts.seed_badges || echo "Badge seed skipped/failed (non-fatal)"
  echo "Healing any trips stuck in 'processing'..."
  python -m scripts.reprocess_stuck || echo "Reprocess skipped/failed (non-fatal)"
) &

# One solo-pool worker: prefork children double memory on a 512 MB free
# instance, and the prefork pool also crashes on macOS in local dev.
echo "Starting Celery worker (background)..."
celery -A app.workers worker -l info --pool=solo &

echo "Starting API on port ${PORT:-8000}..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
