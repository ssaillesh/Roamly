"""Worker package. Exposes the Celery app as `celery` for `celery -A app.workers`."""
from app.workers.celery_app import celery

# Import task modules so they register with the app.
from app.workers import trip_processor, badge_worker  # noqa: E402,F401

__all__ = ["celery"]
