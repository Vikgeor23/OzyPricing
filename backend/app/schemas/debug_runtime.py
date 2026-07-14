"""Debug runtime verification schemas."""

from pydantic import BaseModel


class ScrapeRuntimeDebug(BaseModel):
    scrape_occ_enabled: bool
    scrape_http_enabled: bool
    playwright_enabled: bool
    worker_version: str
    occ_test_product_code: str
    occ_test_status: int
    occ_test_duration_ms: int
    occ_test_error: str | None = None
    occ_test_price: str | None = None
