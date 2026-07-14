"""In-page JS extraction for Technopolis PDPs (lightweight Playwright path)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from app.scrapers.base import ScrapeResult
from app.scrapers.bg_price import parse_bg_leva_amount

_JS_EXTRACT = """
() => {
  const pickText = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const t = (el.getAttribute('content') || el.textContent || '').trim();
    return t || null;
  };
  const priceSelectors = [
    '[itemprop="price"]',
    '.current-price',
    '.price',
    '[data-testid*="price"]',
  ];
  let priceText = null;
  let priceSelector = null;
  for (const sel of priceSelectors) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const t = (el.getAttribute('content') || el.textContent || '').trim();
    if (t && /\\d/.test(t)) {
      priceText = t;
      priceSelector = sel;
      break;
    }
  }
  const title =
    pickText('h1') ||
    pickText('meta[property="og:title"]') ||
    pickText('.product-title') ||
    pickText('[itemprop="name"]');
  let availability = null;
  const body = document.body ? document.body.innerText : '';
  const m = body.match(/(в\\s+наличност|изчерпан|заявка|очакваме|не\\s+е\\s+наличен)/i);
  if (m) availability = m[1].toLowerCase().replace(/\\s+/g, '_');
  let ean = null;
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const data = JSON.parse(s.textContent || '');
      const nodes = Array.isArray(data) ? data : [data];
      for (const n of nodes) {
        if (!n || typeof n !== 'object') continue;
        const t = n['@type'];
        const isProduct = (Array.isArray(t) ? t : [t]).some((x) => String(x).includes('Product'));
        if (!isProduct) continue;
        if (n.gtin13 || n.gtin) { ean = String(n.gtin13 || n.gtin); break; }
      }
    } catch (e) {}
    if (ean) break;
  }
  return JSON.stringify({
    title,
    priceText,
    priceSelector,
    availability,
    ean,
  });
}
"""


def js_extract_script() -> str:
    return _JS_EXTRACT


def parse_js_extract_payload(raw: Any, *, url: str, captured_at: datetime) -> ScrapeResult | None:
    """Build ScrapeResult from evaluate() JSON when price is present."""
    if not raw or not isinstance(raw, dict):
        return None
    price_text = raw.get("priceText")
    if not price_text:
        return None
    price = parse_bg_leva_amount(str(price_text)[:64])
    if price is None:
        for part in str(price_text).split():
            price = parse_bg_leva_amount(part)
            if price is not None:
                break
    if price is None:
        return None

    title = raw.get("title")
    if title:
        title = str(title)[:512]

    ean = raw.get("ean")
    product_ids: dict[str, Any] = {}
    if ean:
        product_ids["ean"] = str(ean)

    return ScrapeResult(
        title=title,
        price=price,
        old_price=None,
        promo_price=None,
        currency="BGN",
        availability=raw.get("availability"),
        captured_at=captured_at,
        image_url=None,
        raw_data={
            "scrape_layer": "playwright",
            "parse_mode": "js_evaluate",
            "price_selector": raw.get("priceSelector"),
            "product_identifiers": product_ids,
            "url": url,
        },
    )
