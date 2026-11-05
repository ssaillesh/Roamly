"""Helper to enqueue Celery tasks, with a synchronous fallback.

If the broker is unreachable (no worker / no Redis), we run the job inline so the
app still works end-to-end in a minimal dev setup.
"""
from app.workers.trip_processor import process_trip, process_trip_sync


def enqueue_trip(trip_id: str) -> None:
    try:
        process_trip.delay(trip_id)
    except Exception:
        process_trip_sync(trip_id)

