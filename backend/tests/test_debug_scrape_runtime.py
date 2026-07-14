"""Tests for GET /debug/scrape-runtime."""

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app


class DebugScrapeRuntimeTests(unittest.TestCase):
    @patch("app.routers.debug.fetch_occ_product", new_callable=AsyncMock)
    def test_scrape_runtime_endpoint(self, mock_fetch: AsyncMock) -> None:
        mock_fetch.return_value = (
            200,
            {"name": "Test", "price": {"value": 30.1, "currencyIso": "EUR"}},
            None,
        )
        client = TestClient(app)
        resp = client.get("/debug/scrape-runtime")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["scrape_occ_enabled"])
        self.assertEqual(body["occ_test_status"], 200)
        self.assertEqual(body["occ_test_product_code"], "14251")
        self.assertGreater(body["occ_test_duration_ms"], 0)
        self.assertEqual(body["occ_test_price"], "30.1")


if __name__ == "__main__":
    unittest.main()
