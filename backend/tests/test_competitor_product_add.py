"""Tests for single competitor product URL add/upsert."""

import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import Competitor, CompetitorCategory, CompetitorProduct
from app.services.competitor_product_service import upsert_competitor_product_url

SAMPLE_URL = (
    "https://www.technopolis.bg/bg/Smartfoni-i-mobilni-telefoni/"
    "Smartfon-GSM--APPLE-IPHONE-16-BLACK/p/505144?utm_source=x"
)


class CompetitorProductUpsertTests(unittest.TestCase):
    def setUp(self) -> None:
        for col in CompetitorProduct.__table__.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()

        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(
            self.engine,
            tables=[Competitor.__table__, CompetitorCategory.__table__, CompetitorProduct.__table__],
        )
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.db = self.Session()
        self.competitor = Competitor(name="Technopolis", domain="technopolis.bg", currency="BGN")
        self.db.add(self.competitor)
        self.db.commit()
        self.db.refresh(self.competitor)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_upsert_creates_then_returns_existing(self) -> None:
        cp1, created1 = upsert_competitor_product_url(
            self.db,
            competitor_id=self.competitor.id,
            url=SAMPLE_URL,
        )
        self.assertTrue(created1)
        self.assertNotIn("utm_source", cp1.url)

        cp2, created2 = upsert_competitor_product_url(
            self.db,
            competitor_id=self.competitor.id,
            url=SAMPLE_URL + "&fbclid=1",
        )
        self.assertFalse(created2)
        self.assertEqual(cp1.id, cp2.id)

    @patch("app.routers.competitor_products.scrape_competitor_product")
    def test_api_post_scrape_after_create(self, mock_scrape: MagicMock) -> None:
        mock_scrape.delay.return_value = MagicMock(id="task-123")
        session_factory = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)

        def override_get_db():
            db = session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        client = TestClient(app)
        try:
            payload = {
                "competitor_id": str(self.competitor.id),
                "url": SAMPLE_URL,
                "scrape_after_create": True,
            }
            res = client.post("/competitor-products", json=payload)
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertTrue(body["created"])
            self.assertEqual(body["scrape_task_id"], "task-123")
            mock_scrape.delay.assert_called_once()

            res2 = client.post("/competitor-products", json={**payload, "scrape_after_create": False})
            self.assertEqual(res2.status_code, 200)
            body2 = res2.json()
            self.assertFalse(body2["created"])
            self.assertEqual(body2["id"], body["id"])
        finally:
            app.dependency_overrides.clear()
            client.close()


if __name__ == "__main__":
    unittest.main()
