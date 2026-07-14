"""Tests for scrape task status API exposing OCC metrics."""

import unittest

from app.schemas.scrape_batch import scrape_task_status_from_meta


class ScrapeTaskStatusApiTests(unittest.TestCase):
    def test_scrape_task_status_from_meta_includes_occ_fields(self) -> None:
        meta = {
            "current": 10,
            "total": 100,
            "scraped": 8,
            "failed": 2,
            "skipped": 0,
            "occ_api_success": 7,
            "occ_api_failed": 3,
            "avg_occ_ms": 450,
            "js_extract_success": 1,
            "adaptive_fast_success": 7,
            "adaptive_playwright_success": 1,
            "playwright_fallback": 1,
            "http_skipped": 10,
            "current_concurrency": 12,
        }
        status = scrape_task_status_from_meta("task-1", "PROGRESS", False, meta)
        self.assertEqual(status.occ_api_success, 7)
        self.assertEqual(status.occ_api_failed, 3)
        self.assertEqual(status.avg_occ_ms, 450)
        self.assertEqual(status.js_extract_success, 1)
        self.assertEqual(status.adaptive_fast_success, 7)
        self.assertEqual(status.adaptive_playwright_success, 1)
        self.assertEqual(status.current_concurrency, 12)


if __name__ == "__main__":
    unittest.main()
