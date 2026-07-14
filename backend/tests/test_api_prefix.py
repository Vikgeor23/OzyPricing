"""Tests for /api route aliases (Cloudflare path prefix)."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import API_MOUNT_PREFIX, create_app


class ApiPrefixRouteTests(unittest.TestCase):
    def tearDown(self) -> None:
        get_settings.cache_clear()

    def test_health_and_api_health(self) -> None:
        client = TestClient(create_app())
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.get("/health").json(), {"status": "ok"})
        self.assertEqual(client.get("/api/health").status_code, 200)
        self.assertEqual(client.get("/api/health").json(), {"status": "ok"})

    def test_api_competitors_tree_not_404(self) -> None:
        client = TestClient(create_app())
        tree = client.get("/api/competitors/tree")
        self.assertNotEqual(tree.status_code, 404)

    def test_unprefixed_competitors_tree_not_404(self) -> None:
        client = TestClient(create_app())
        tree = client.get("/competitors/tree")
        self.assertNotEqual(tree.status_code, 404)

    def test_api_debug_route_not_404(self) -> None:
        client = TestClient(create_app())
        resp = client.get("/api/debug/scrape-runtime")
        self.assertNotEqual(resp.status_code, 404)

    def test_mount_prefix_constant(self) -> None:
        self.assertEqual(API_MOUNT_PREFIX, "/api")


if __name__ == "__main__":
    unittest.main()
