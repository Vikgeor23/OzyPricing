"""In-memory catalog index for batch matching.

Batch matching used to hit the database (or scan the entire catalog with
fuzzy scoring) once per listing, which stops scaling past a few thousand
products. The index is built once per batch run and answers candidate
lookups from dictionaries:

- exact EAN
- exact manufacturer code (also matched against listing sku)
- brand bucket (capped)
- shared rare name-tokens (inverted index) for the fuzzy tier
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CompetitorProduct, Product

_TOKEN_RE = re.compile(r"[a-z0-9а-я]{3,}")
# Tokens present in more than this many products carry no signal.
_MAX_TOKEN_DF = 2500
_BRAND_BUCKET_CAP = 800
_TOKEN_CANDIDATES = 60


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _norm_code(value: str | None) -> str:
    return re.sub(r"[\s\-_./]", "", (value or "").strip().lower())


def _tokens(value: str | None) -> list[str]:
    return _TOKEN_RE.findall(_norm(value))


class CatalogIndex:
    def __init__(self, products: list[Product]) -> None:
        self.products = products
        self.by_ean: dict[str, list[Product]] = defaultdict(list)
        self.by_mfr: dict[str, list[Product]] = defaultdict(list)
        self.by_brand: dict[str, list[Product]] = defaultdict(list)
        token_index: dict[str, list[Product]] = defaultdict(list)

        for p in products:
            ean = _norm(p.ean)
            if ean:
                self.by_ean[ean].append(p)
            mfr = _norm_code(p.manufacturer_code)
            if mfr:
                self.by_mfr[mfr].append(p)
            brand = _norm(p.brand)
            if brand:
                self.by_brand[brand].append(p)
            for token in set(_tokens(p.name)):
                token_index[token].append(p)

        self.token_index = {
            token: rows for token, rows in token_index.items() if len(rows) <= _MAX_TOKEN_DF
        }

    @classmethod
    def load(cls, db: Session) -> "CatalogIndex":
        products = list(db.scalars(select(Product)).all())
        return cls(products)

    def candidates_for(self, cp: CompetitorProduct) -> list[Product]:
        out: list[Product] = []
        seen: set = set()

        def add(rows: list[Product]) -> None:
            for p in rows:
                if p.id not in seen:
                    seen.add(p.id)
                    out.append(p)

        ean = _norm(cp.ean)
        if ean:
            add(self.by_ean.get(ean, []))

        for code in (_norm_code(cp.manufacturer_code), _norm_code(cp.sku), _norm_code(cp.model)):
            if code:
                add(self.by_mfr.get(code, []))

        brand = _norm(cp.brand)
        if brand:
            add(self.by_brand.get(brand, [])[:_BRAND_BUCKET_CAP])

        # Fuzzy tier: products sharing the rarest title tokens.
        counter: Counter = Counter()
        for token in set(_tokens(cp.title)):
            for p in self.token_index.get(token, []):
                counter[p] += 1
        if counter:
            add([p for p, _ in counter.most_common(_TOKEN_CANDIDATES)])

        return out
