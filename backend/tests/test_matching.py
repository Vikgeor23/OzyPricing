"""Tests for deterministic product matching."""

import unittest
from decimal import Decimal
from uuid import uuid4

from app.models import Competitor, CompetitorProduct, Product
from app.services.matching import score_product_against_listing


def _product(**kwargs) -> Product:
    p = Product(
        id=uuid4(),
        sku=kwargs.get("sku", "SKU-1"),
        name=kwargs.get("name", "Apple iPhone 16 Black 128GB"),
        ean=kwargs.get("ean"),
        brand=kwargs.get("brand"),
        manufacturer_code=kwargs.get("manufacturer_code"),
        model=kwargs.get("model"),
        color=kwargs.get("color"),
        storage=kwargs.get("storage"),
        memory=kwargs.get("memory"),
    )
    return p


def _listing(**kwargs) -> CompetitorProduct:
    cp = CompetitorProduct(
        id=uuid4(),
        competitor_id=uuid4(),
        url="https://www.technopolis.bg/bg/test/p/1",
        title=kwargs.get("title", "Apple iPhone 16 Black 128GB"),
        ean=kwargs.get("ean"),
        brand=kwargs.get("brand"),
        manufacturer_code=kwargs.get("manufacturer_code"),
        model=kwargs.get("model"),
        specs_json=kwargs.get("specs_json"),
    )
    return cp


class MatchingScoreTests(unittest.TestCase):
    def test_ean_exact_scores_100(self) -> None:
        p = _product(ean="5901234123457")
        cp = _listing(ean="5901234123457", title="Some phone")
        ev = score_product_against_listing(p, cp)
        self.assertEqual(ev.score, Decimal("100"))
        self.assertEqual(ev.method, "ean_exact")
        self.assertIn("EAN exact match", ev.reasons)

    def test_manufacturer_code_exact_scores_95(self) -> None:
        p = _product(manufacturer_code="MQ6K3", brand="Apple")
        cp = _listing(manufacturer_code="MQ6K3", title="iPhone")
        ev = score_product_against_listing(p, cp)
        self.assertEqual(ev.score, Decimal("95"))
        self.assertEqual(ev.method, "manufacturer_code_exact")

    def test_brand_and_title_similarity_without_ean(self) -> None:
        p = _product(brand="Apple", name="Apple iPhone 16 Black 128GB", ean=None)
        cp = _listing(
            brand="Apple",
            title="Apple iPhone 16 Black 128GB",
            ean=None,
        )
        ev = score_product_against_listing(p, cp)
        self.assertGreaterEqual(ev.score, Decimal("80"))
        self.assertIn("No EAN available", ev.warnings)

    def test_storage_conflict_reduces_score(self) -> None:
        p = _product(
            brand="Apple",
            name="Apple iPhone 16 Pink 256GB",
            storage="256GB",
            color="pink",
            ean="1111111111111",
        )
        cp = _listing(
            brand="Apple",
            title="Apple iPhone 16 Black 128GB",
            ean="1111111111111",
            specs_json={"storage": "128GB", "color": "black"},
        )
        ev = score_product_against_listing(p, cp)
        self.assertEqual(ev.method, "ean_exact")
        self.assertTrue(any("Storage differs" in w for w in ev.warnings))
        self.assertLess(ev.score, Decimal("100"))

    def test_missing_ean_falls_back_to_title(self) -> None:
        p = _product(name="Samsung Galaxy S24 Ultra", brand="Samsung")
        cp = _listing(title="Samsung Galaxy S24 Ultra 256GB Titanium", brand="Samsung")
        ev = score_product_against_listing(p, cp)
        self.assertGreaterEqual(ev.score, Decimal("60"))
        self.assertTrue(
            "title" in ev.method or "brand" in ev.method or "token" in ev.method,
            ev.method,
        )


if __name__ == "__main__":
    unittest.main()
