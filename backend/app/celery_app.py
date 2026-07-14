"""Celery application instance."""

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "price_monitor",
    broker=settings.effective_celery_broker,
    backend=settings.effective_celery_backend,
    include=[
        "app.tasks.scrape_tasks",
        "app.tasks.scrape_batch_tasks",
        "app.tasks.discovery_tasks",
        "app.tasks.match_tasks",
        "app.tasks.import_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)
