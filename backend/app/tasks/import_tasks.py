"""Async XLSX catalog import with live progress."""

from __future__ import annotations

import base64

from app.celery_app import celery_app
from app.database import SessionLocal
from app.services.product_import import import_products_from_xlsx


@celery_app.task(bind=True, name="app.tasks.import_tasks.import_catalog_xlsx")
def import_catalog_xlsx(self, file_b64: str, filename: str) -> dict:
    file_bytes = base64.b64decode(file_b64)

    def on_progress(current: int, total: int, phase: str) -> None:
        self.update_state(
            state="PROGRESS",
            meta={"current": current, "total": total, "phase": phase},
        )

    db = SessionLocal()
    try:
        summary = import_products_from_xlsx(
            db,
            file_bytes,
            filename=filename,
            progress_callback=on_progress,
        )
        return summary.model_dump()
    finally:
        db.close()
