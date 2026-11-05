"""Celery application (Redis broker + result backend).

Run with:  celery -A app.workers worker -l info -c 4
Set CELERY_TASK_ALWAYS_EAGER=1 to run tasks inline (tests / no-worker dev).
"""
import os

from celery import Celery

from app.config import settings

celery = Celery(
    "trekrank",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_always_eager=os.getenv("CELERY_TASK_ALWAYS_EAGER", "0") == "1",
    task_eager_propagates=True,
    timezone="UTC",
)

# Ensure task modules are imported & registered.
celery.autodiscover_tasks(
    ["app.workers.trip_processor", "app.workers.badge_worker"]
)
