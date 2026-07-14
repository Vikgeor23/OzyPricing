"""Generic sitemap-based product URL discovery for unknown ecommerce domains."""

from __future__ import annotations

import logging
import asyncio
import gzip
import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from app.config import get_settings
from app.utils.url_utils import normalize_domain, normalize_url

logger = logging.getLogger(__name__)

DEFAULT_MAX_PRODUCTS: int | None = None
_HTTP_TIMEOUT = 30.0
_EXTERNAL_SEARCH_TIMEOUT = 8.0
_EXTERNAL_SEARCH_BATCH_SIZE = 2
_EXTERNAL_SEARCH_PATIENT_BATCH_DELAY = 1.5
_EXTERNAL_SEARCH_RATE_LIMIT_DELAY = 20.0
_MAX_SITEMAP_DEPTH = 8
_MAX_PUBLIC_CRAWL_PAGES = 500
_MAX_EXTERNAL_INDEX_RESULTS = 5000
_MAX_DYNAMIC_JSON_BYTES = 2_000_000
_MAX_DYNAMIC_ENDPOINT_PAGES = 3
_MAX_PLAYWRIGHT_RESPONSES = 60
_MAX_PAGES_PER_CATEGORY = 40
_MAX_SITE_SEARCH_TERMS = 30
_MAX_SITE_SEARCH_RESULT_PAGES = 3
_MAGENTO_GRAPHQL_PAGE_SIZE = 500
_MAGENTO_GRAPHQL_QUERY = """
query MagentoProducts($pageSize: Int!, $currentPage: Int!) {
  products(filter: {}, pageSize: $pageSize, currentPage: $currentPage) {
    total_count
    page_info {
      current_page
      page_size
      total_pages
    }
    items {
      sku
      name
      url_key
      canonical_url
      ... on ConfigurableProduct {
        variants {
          attributes { code label value_index }
          product {
            sku
            name
            url_key
            canonical_url
          }
        }
      }
    }
  }
}
"""
_TRACKING_QUERY_PREFIXES = ("utm_", "mc_")
_TRACKING_QUERY_EXACT = frozenset({"fbclid", "gclid", "ref", "source", "campaign", "affiliate"})
# Listing-state params picked up when a product link is harvested from a
# paginated/sorted category page; they never identify a product, and keeping
# them multiplies one product into many "unique" URLs (?page=2, ?page=3, …).
_LISTING_QUERY_EXACT = frozenset({
    "page",
    "p",
    "per_page",
    "limit",
    "offset",
    "start",
    "sort",
    "sort_by",
    "sortby",
    "orderby",
    "order",
    "dir",
    "view",
})
_STATIC_EXTENSIONS = (
    ".css",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".pdf",
    ".png",
    ".svg",
    ".webp",
    ".xml",
    ".zip",
)
_EXCLUDED_SEGMENTS = frozenset(
    {
        "account",
        "beautyblog",
        "blog",
        "brand",
        "brands",
        "campaign",
        "cart",
        "categories",
        "category",
        "checkout",
        "compare",
        "contact",
        "contacts",
        "deals",
        "delivery",
        "dostavka",
        "faq",
        "garancia",
        "garantsiya",
        "help",
        "info",
        "kampanii",
        "kontakti",
        "label",
        "label-campaign",
        "labels",
        "landing",
        "login",
        "logout",
        "lps",
        "magazini",
        "marka",
        "marki",
        "nav",
        "news",
        "novini",
        "pomosht",
        "pravila",
        "privacy",
        "proizvoditeli",
        "promocii",
        "promotsii",
        "register",
        "salon",
        "saloni",
        "salons",
        "search",
        "services",
        "store-locator",
        "stores",
        "tag",
        "tags",
        "terms",
        "uslugi",
        "usloviya",
        "wishlist",
        "za-nas",
        "категории",
        "марка",
        "марки",
    },
)
# Tokens inside a final path segment that mark legal/marketing pages rather
# than products (e.g. "obschi-uslovija-klientska-karta-12-2025").
_NON_PRODUCT_SLUG_TOKENS = frozenset(
    {
        "campaign",
        "cookies",
        "gdpr",
        "kampanija",
        "politika",
        "pravila",
        "promo",
        "uslovija",
        "usloviya",
    },
)
_PRODUCT_HINT_SEGMENTS = frozenset({"p", "pd", "dp", "product", "products", "prod", "item", "items", "offer", "offers"})
_CATEGORY_HINT_SEGMENTS = frozenset(
    {
        "c",
        "cat",
        "cats",
        "catalog",
        "categories",
        "category",
        "collections",
        "kategorii",
        "shop",
        "store",
        "продукти",
        "категории",
    },
)
_EXTERNAL_NON_PRODUCT_TERMS = frozenset(
    {
        "black-friday",
        "campaign",
        "clearance",
        "discount",
        "offer",
        "offers",
        "promo",
        "promos",
        "promotsii",
        "sale",
        "sales",
    },
)
_EXTERNAL_NON_PRODUCT_SEGMENTS = frozenset(
    {
        "beautyblog",
        "category",
        "collection",
        "collections",
        "help",
        "kozmetika",
        "majka-i-dete",
        "nay-prodavani-parfyumi",
        "oferti",
        "parfyumi",
        "pregledi",
        "reviews",
        "shopping-days",
        "summer-black-friday",
        "sbf",
    },
)
_SEARCH_SEED_TERMS = (
    "perfume",
    "serum",
    "shampoo",
    "cream",
    "makeup",
    "cosmetics",
    "product",
    "buy",
    "shop",
    "price",
    "brand",
    "sale",
    "Код",
    "Купете",
    "цена",
    "SKU",
    "EAN",
    "add to cart",
)
_DYNAMIC_ENDPOINT_PATHS = (
    "/products.json?limit=250&page={page}",
    "/collections/all/products.json?limit=250&page={page}",
    "/wp-json/wc/store/products?per_page=100&page={page}",
    "/wp-json/wc/store/v1/products?per_page=100&page={page}",
    "/wp-json/wp/v2/product?per_page=100&page={page}",
)
_MERCHANT_FEED_PATHS = (
    "/feed/google",
    "/feed/google.xml",
    "/feeds/google.xml",
    "/google_feed.xml",
    "/google-feed.xml",
    "/googlemerchant.xml",
    "/google_merchant.xml",
    "/product_feed.xml",
    "/products_feed.xml",
    "/product-feed.xml",
    "/facebook_feed.xml",
    "/facebook-feed.xml",
    "/feed.xml",
    "/index.php?route=extension/feed/google_base",
)
_AUTOCOMPLETE_ENDPOINT_TEMPLATES = (
    "/search/suggest.json?q={q}&resources[type]=product&resources[limit]=20",
    "/search/suggest?q={q}",
    "/search/autocomplete?q={q}",
    "/autocomplete?q={q}",
    "/api/search?q={q}",
    "/api/autocomplete?q={q}",
    "/searchanise/result?q={q}",
    "/?wc-ajax=flatsome_ajax_search_products&query={q}",
)
_AUTOCOMPLETE_PREFIXES = (
    *"abcdefghijklmnopqrstuvwxyz",
    *"абвгдежзиклмнопрстуфхцчшщя",
)
_SEARCH_INPUT_SELECTORS = (
    "input[type='search']",
    "input[name='q']",
    "input[name='query']",
    "input[name='search']",
    "input[placeholder*='Search']",
    "input[placeholder*='search']",
    "input[placeholder*='Търс']",
    "input[placeholder*='търс']",
)
_NUMERIC_TOKEN = re.compile(r"\d{3,}")
_SLUG_WITH_DIGITS = re.compile(r"[a-zа-я][a-zа-я0-9-]*\d|\d[a-zа-я0-9-]*[a-zа-я]", re.I)
_TEXTY_SLUG = re.compile(r"^[a-zа-я0-9]+(?:-[a-zа-я0-9]+){2,}$", re.I)

ProgressCallback = Callable[[dict[str, Any]], None]


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _same_domain(url: str, domain: str) -> bool:
    return normalize_domain(url) == normalize_domain(domain)


def _strip_tracking_query(query: str) -> str:
    if not query:
        return ""
    params = parse_qs(query, keep_blank_values=False)
    kept = {
        k: v
        for k, v in params.items()
        if k.lower() not in _TRACKING_QUERY_EXACT
        and k.lower() not in _LISTING_QUERY_EXACT
        and not any(k.lower().startswith(p) for p in _TRACKING_QUERY_PREFIXES)
    }
    return urlencode(kept, doseq=True)


def normalize_generic_product_url(url: str, *, domain: str) -> str | None:
    parsed = urlparse(normalize_url(url))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if not _same_domain(url, domain):
        return None
    path = parsed.path.rstrip("/") or "/"
    if path.lower().endswith(_STATIC_EXTENSIONS):
        return None
    return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", _strip_tracking_query(parsed.query), ""))


def _looks_like_sitemap_url(url: str) -> bool:
    low = url.lower().split("?", 1)[0]
    return low.endswith((".xml", ".xml.gz")) or "sitemap" in low


def _build_parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    parent_map: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parent_map[child] = parent
    return parent_map


def _parse_text_sitemap(body: bytes) -> list[str]:
    """Plain-text sitemap (sitemaps.org): one absolute URL per line."""
    urls: list[str] = []
    for line in body.decode("utf-8", errors="ignore").splitlines():
        candidate = line.strip()
        if candidate.startswith(("http://", "https://")):
            urls.append(candidate)
    return urls


def _parse_feed_locs(root: ET.Element) -> list[str]:
    """RSS 2.0 (<item><link>text</link>) and Atom (<entry><link href=…/>) feeds."""
    urls: list[str] = []
    for elem in root.iter():
        name = _local_tag(elem.tag)
        if name not in ("item", "entry"):
            continue
        for child in elem:
            if _local_tag(child.tag) != "link":
                continue
            href = (child.get("href") or "").strip() or (child.text or "").strip()
            if href.startswith(("http://", "https://")):
                urls.append(href)
                break
    return urls


def parse_sitemap_locs(xml_bytes: bytes) -> tuple[list[str], list[str]]:
    page_locs: list[str] = []
    nested: list[str] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        # Not XML — the protocol also allows plain-text sitemaps.
        for url in _parse_text_sitemap(xml_bytes):
            (nested if _looks_like_sitemap_url(url) else page_locs).append(url)
        return page_locs, nested

    root_name = _local_tag(root.tag)
    if root_name in ("rss", "feed"):
        return _parse_feed_locs(root), nested
    parent_map = _build_parent_map(root)
    for elem in root.iter():
        if _local_tag(elem.tag) != "loc" or not elem.text:
            continue
        loc = elem.text.strip()
        parent = parent_map.get(elem)
        parent_name = _local_tag(parent.tag) if parent is not None else ""
        if root_name == "sitemapindex" or parent_name == "sitemap" or _looks_like_sitemap_url(loc):
            nested.append(loc)
        else:
            page_locs.append(loc)
    return page_locs, nested


def is_probable_product_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return False
    segments = [s.lower() for s in path.split("/") if s]
    if any(seg in _EXCLUDED_SEGMENTS for seg in segments):
        return False
    if parsed.path.lower().endswith(_STATIC_EXTENSIONS):
        return False
    last = segments[-1]
    if last.endswith("-brand") or set(last.split("-")) & _NON_PRODUCT_SLUG_TOKENS:
        return False
    if any(seg in _PRODUCT_HINT_SEGMENTS for seg in segments) and _NUMERIC_TOKEN.search(path):
        return True
    # Generic slug heuristic: category-tree leaves tend to be short
    # ("intel-b660", "1-8-tb") while product slugs are long and hyphen-rich.
    if _SLUG_WITH_DIGITS.search(last) and last.count("-") >= 2 and len(last) >= 12:
        return True
    query = parse_qs(parsed.query)
    return any(k.lower() in {"id", "product_id", "sku", "pid"} for k in query)


def _is_probable_external_product_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return False
    segments = [s.lower() for s in path.split("/") if s]
    if any(seg in _EXCLUDED_SEGMENTS for seg in segments):
        return False
    if segments[0] in _EXTERNAL_NON_PRODUCT_SEGMENTS:
        return False
    last = segments[-1]
    last_tokens = set(last.split("-"))
    if last in _EXTERNAL_NON_PRODUCT_TERMS or last_tokens & _EXTERNAL_NON_PRODUCT_TERMS:
        return False
    if is_probable_product_url(url):
        return True
    if parsed.query:
        return False
    if len(segments) < 2 or len(segments) > 4:
        return False
    return bool(_TEXTY_SLUG.match(last)) and len(last) >= 16 and last.count("-") >= 3


def _is_probable_dynamic_product_url(url: str) -> bool:
    parsed = urlparse(url)
    segments = [s.lower() for s in parsed.path.strip("/").split("/") if s]
    if not segments or any(seg in _EXCLUDED_SEGMENTS for seg in segments):
        return False
    if (
        segments[0] in _EXTERNAL_NON_PRODUCT_SEGMENTS
        or segments[-1] in _EXTERNAL_NON_PRODUCT_TERMS
        or segments[-1].startswith("sbf-")
        or "uslovija" in segments[-1]
    ):
        return False
    if is_probable_product_url(url):
        return True
    last = segments[-1]
    if last.endswith("-brand") or set(last.split("-")) & _NON_PRODUCT_SLUG_TOKENS:
        return False
    if any(seg in _PRODUCT_HINT_SEGMENTS for seg in segments):
        return len(last) >= 6 and bool(_TEXTY_SLUG.match(last) or _SLUG_WITH_DIGITS.search(last))
    query = parse_qs(parsed.query)
    if any(k.lower() in {"id", "product_id", "sku", "pid"} for k in query):
        return True
    # Same strictness as is_probable_product_url: short digit-bearing slugs
    # are category leaves, not products. Likewise digit-bearing slugs under an
    # explicit category namespace (/cats/…) — those are subcategory leaves
    # (age/size filters like "pazeli-nad-3-godini"), not products.
    if any(seg in _CATEGORY_HINT_SEGMENTS for seg in segments[:-1]):
        return False
    return bool(_SLUG_WITH_DIGITS.search(last) and last.count("-") >= 2 and len(last) >= 12)


def _is_probable_listing_product_url(url: str, *, domain: str) -> bool:
    normalized = normalize_generic_product_url(url, domain=domain)
    if normalized is None:
        return False
    parsed = urlparse(normalized)
    segments = [s.lower() for s in parsed.path.strip("/").split("/") if s]
    if len(segments) < 3 or any(seg in _EXCLUDED_SEGMENTS for seg in segments):
        return False
    if segments[0] in _EXTERNAL_NON_PRODUCT_SEGMENTS:
        return False
    last = segments[-1]
    if last in _EXTERNAL_NON_PRODUCT_TERMS or last.startswith("productsegment_"):
        return False
    return last.count("-") >= 2 and bool(_NUMERIC_TOKEN.search(last))


def _prefer_hinted_product_urls(found: list[str]) -> tuple[list[str], int]:
    """Domain-adaptive cleanup: when most URLs sit under a product hint segment
    (/product/, /pd/ …), the site clearly namespaces products there — anything
    outside the namespace is a category/series page, not a product.
    Returns (filtered_urls, dropped_count)."""
    if len(found) < 100:
        return found, 0

    def has_hint(url: str) -> bool:
        segments = [s.lower() for s in urlparse(url).path.strip("/").split("/") if s]
        return any(seg in _PRODUCT_HINT_SEGMENTS for seg in segments)

    hinted = [u for u in found if has_hint(u)]
    if len(hinted) * 2 >= len(found):
        return hinted, len(found) - len(hinted)
    return found, 0


def _is_static_or_excluded_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.path.lower().endswith(_STATIC_EXTENSIONS):
        return True
    segments = [s.lower() for s in parsed.path.strip("/").split("/") if s]
    return any(seg in _EXCLUDED_SEGMENTS for seg in segments)


_BRAND_INDEX_SEGMENTS = frozenset({"brand", "brands", "marka", "marki", "марка", "марки"})


def _is_brand_listing_url(url: str) -> bool:
    """/brands/<name> style pages list that brand's products — crawlable even
    though bare brand-index segments are excluded as product URLs."""
    segments = [s.lower() for s in urlparse(url).path.strip("/").split("/") if s]
    return len(segments) == 2 and segments[0] in _BRAND_INDEX_SEGMENTS


def _is_probable_listing_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.path.lower().endswith(_STATIC_EXTENSIONS):
        return False
    segments = [s.lower() for s in parsed.path.strip("/").split("/") if s]
    if not segments:
        return True
    if any(seg in _EXCLUDED_SEGMENTS for seg in segments):
        return False
    return any(seg in _CATEGORY_HINT_SEGMENTS for seg in segments) or len(segments) <= 2


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_CHALLENGE_MARKERS = (
    "just a moment",
    "един момент",
    "cf-browser-verification",
    "cf-challenge",
    "challenge-platform",
    "checking your browser",
)


def _is_blocked_error(error: str | None) -> bool:
    """True when httpx failed in a way a real browser might get past
    (Cloudflare 403 / anti-bot challenge), as opposed to 404/timeout/etc."""
    if not error:
        return False
    return error.startswith("status_403:") or error.startswith("blocked_challenge:")


# Domains known to drop plain HTTP clients at the transport level while
# serving a real browser (e.g. altex.ro behind Akamai). Each collector creates
# its own _BrowserFetcher, so without this process-level cache every method
# would re-pay two 30s timeouts before re-learning to prefer the browser.
_HTTP_BLOCKED_DOMAINS: set[str] = set()

_virtual_display_proc: subprocess.Popen | None = None
_virtual_display_started = False


def _ensure_virtual_display() -> bool:
    """Lazily provide an X display for headful Chromium.

    If a DISPLAY is already exported (e.g. the worker runs under xvfb-run) it is
    reused. Otherwise a private Xvfb is spawned once per process. Xvfb is
    launched directly (not via xvfb-run) so no xauth binary is required.
    """
    global _virtual_display_proc, _virtual_display_started
    if os.environ.get("DISPLAY"):
        return True
    if _virtual_display_started:
        return _virtual_display_proc is not None and _virtual_display_proc.poll() is None
    _virtual_display_started = True
    if not shutil.which("Xvfb"):
        logger.warning("headful discovery fallback unavailable: Xvfb not installed")
        return False
    # Offset the display number by PID so prefork workers do not collide.
    for candidate in (99 + os.getpid() % 100, 128, 129, 130, 131):
        display = f":{candidate}"
        try:
            proc = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1366x768x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:  # noqa: PERF203
            logger.warning("failed to start Xvfb on %s: %s", display, exc)
            continue
        time.sleep(1.2)
        if proc.poll() is not None:  # died immediately -> display in use
            continue
        os.environ["DISPLAY"] = display
        _virtual_display_proc = proc
        logger.info("started virtual display %s for headful discovery", display)
        return True
    logger.warning("headful discovery fallback unavailable: no free Xvfb display")
    return False


class _BrowserFetcher:
    """Shared, lazily-launched Chromium used to retry URLs that httpx cannot
    reach because of Cloudflare / managed-challenge protection.

    One warmed context is reused for the whole discovery run. Navigation-based:
    in-page ``fetch()`` is blocked by Cloudflare (Sec-Fetch-Mode: cors) while a
    top-level navigation passes, so every fetch goes through ``page.goto`` and
    XML sitemaps (served as downloads) are captured from the download event.
    """

    def __init__(self, origin: str):
        self._origin = origin
        self._pw = None
        self._browser = None
        self._context = None
        self._warmed = False
        self._unavailable = False
        self._challenge_wait = max(4.0, float(get_settings().discovery_browser_challenge_wait_sec))
        self._budget = max(1, int(get_settings().discovery_browser_max_pages))
        self._navigations = 0
        # Some sites (e.g. altex.ro behind Akamai) drop plain HTTP clients at
        # the transport level while serving a real browser. After two httpx
        # transport failures (not necessarily consecutive — Akamai blocks are
        # often intermittent) _fetch_text stops paying the 30s timeout per URL
        # and goes straight to the browser.
        self.http_transport_failures = 0
        self.prefer_browser = normalize_domain(origin) in _HTTP_BLOCKED_DOMAINS

    @property
    def budget_exhausted(self) -> bool:
        return self._navigations >= self._budget

    async def _ensure(self) -> bool:
        if self._context is not None:
            return True
        if self._unavailable:
            return False
        headful = bool(get_settings().discovery_browser_headful)
        if headful and not _ensure_virtual_display():
            headful = False  # fall back to headless rather than failing outright
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=not headful,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            self._context = await self._browser.new_context(
                locale="bg-BG",
                user_agent=_BROWSER_UA,
                viewport={"width": 1366, "height": 768},
                accept_downloads=True,
            )

            async def _route(route) -> None:  # type: ignore[no-untyped-def]
                if route.request.resource_type in {"image", "media", "font"}:
                    await route.abort()
                else:
                    await route.continue_()

            await self._context.route("**/*", _route)
        except Exception as exc:  # noqa: BLE001
            logger.warning("browser fallback failed to launch: %s", exc)
            self._unavailable = True
            await self.close()
            return False
        return True

    async def _clear_challenge(self, page: Any) -> bool:
        deadline = time.monotonic() + self._challenge_wait
        while True:
            try:
                content = (await page.content()).lower()
            except Exception:  # noqa: BLE001
                content = ""
            if not any(m in content[:4000] for m in _CHALLENGE_MARKERS):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(2)

    async def _warmup(self) -> None:
        if self._warmed:
            return
        self._warmed = True
        page = await self._context.new_page()
        try:
            await page.goto(self._origin, wait_until="domcontentloaded", timeout=45_000)
            await self._clear_challenge(page)
        except Exception:  # noqa: BLE001
            pass
        finally:
            await page.close()

    async def fetch_text(self, url: str) -> tuple[bytes | None, str | None]:
        if self._navigations >= self._budget:
            return None, f"browser_budget_exhausted:{url}"
        if not await self._ensure():
            return None, f"browser_unavailable:{url}"
        self._navigations += 1
        await self._warmup()
        page = await self._context.new_page()
        download_holder: dict[str, Any] = {}
        page.on("download", lambda d: download_holder.setdefault("d", d))
        try:
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            except Exception as exc:  # noqa: BLE001
                if "Download is starting" not in str(exc):
                    return None, f"browser_error:{type(exc).__name__}:{url}"
                response = None
            # An XML sitemap comes back as a download rather than a rendered page.
            if response is None or "d" in download_holder:
                for _ in range(20):
                    if "d" in download_holder:
                        break
                    await asyncio.sleep(0.5)
                dl = download_holder.get("d")
                if dl is None:
                    return None, f"browser_no_content:{url}"
                path = await dl.path()
                if path is None:
                    return None, f"browser_download_failed:{url}"
                with open(path, "rb") as fh:
                    data = fh.read()
                if data[:2] == b"\x1f\x8b":
                    try:
                        data = gzip.decompress(data)
                    except OSError:
                        pass
                return (data, None) if data.strip() else (None, f"browser_empty:{url}")
            if not await self._clear_challenge(page):
                return None, f"browser_blocked_challenge:{url}"
            status = response.status if response else 0
            if status >= 400:
                return None, f"browser_status_{status}:{url}"
            html = await page.content()
            return html.encode("utf-8"), None
        except Exception as exc:  # noqa: BLE001
            return None, f"browser_error:{type(exc).__name__}:{url}"
        finally:
            await page.close()

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._browser = None
        self._context = None
        self._pw = None


async def check_site_reachability(site_url_or_domain: str) -> dict[str, Any]:
    """Fast connectivity precheck before a full discovery run.

    Returns ``{"reachable": bool, "via": "http" | "browser" | None, "errors": [...]}``.

    ``via="browser"`` means the site drops plain HTTP clients at the transport
    level (Akamai/Cloudflare IP heuristics) but still serves a real browser —
    discovery should skip httpx-only steps and lean on the browser fallback.
    ``reachable=False`` means even Chromium gets nothing (IP-level block or
    dead host), so discovery can fail fast instead of burning a 30s timeout
    per request across every method. Any HTTP response — including 403 or a
    challenge page — counts as reachable.
    """
    start = normalize_url(site_url_or_domain)
    parsed = urlparse(start)
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    headers = {"User-Agent": _BROWSER_UA, "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8"}
    errors: list[str] = []
    timeout = httpx.Timeout(8.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for path in ("/robots.txt", "/"):
            try:
                await client.get(f"{origin}{path}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}:{origin}{path}")
            else:
                return {"reachable": True, "via": "http", "errors": errors}
    # Some sites drop plain HTTP clients on TLS fingerprint but still serve a
    # real browser — give Chromium one attempt before declaring the site dead.
    if get_settings().discovery_browser_fallback_enabled:
        browser = _BrowserFetcher(origin)
        browser._warmed = True  # noqa: SLF001 — we navigate the origin itself; warmup would duplicate it
        try:
            body, error = await browser.fetch_text(origin)
        finally:
            await browser.close()
        if body is not None or (
            error is not None
            and (error.startswith("browser_status_") or error.startswith("browser_blocked_challenge:"))
        ):
            _HTTP_BLOCKED_DOMAINS.add(normalize_domain(origin))
            return {"reachable": True, "via": "browser", "errors": errors}
        if error:
            errors.append(error)
    return {"reachable": False, "via": None, "errors": errors}


async def _fetch_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    browser: _BrowserFetcher | None = None,
) -> tuple[bytes | None, str | None]:
    if browser is not None and browser.prefer_browser and not browser.budget_exhausted:
        return await browser.fetch_text(url)
    try:
        resp = await client.get(url, follow_redirects=True)
        if resp.status_code != 200:
            error = f"status_{resp.status_code}:{url}"
            if browser is not None and _is_blocked_error(error):
                return await browser.fetch_text(url)
            return None, error
        body = resp.content
        # Compressed sitemaps (e.g. sitemap.xml.gz) arrive as gzip *content*,
        # which httpx does not decode — only transport-level encoding is.
        if body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(body)
            except OSError as exc:
                return None, f"gzip_error:{url}:{exc}"
        if not body.strip():
            return None, f"empty:{url}"
        return body, None
    except Exception as exc:  # noqa: BLE001
        if browser is not None and isinstance(exc, httpx.TransportError):
            browser.http_transport_failures += 1
            if browser.http_transport_failures >= 2:
                browser.prefer_browser = True
                _HTTP_BLOCKED_DOMAINS.add(normalize_domain(browser._origin))  # noqa: SLF001
            return await browser.fetch_text(url)
        return None, f"{type(exc).__name__}:{url}:{exc}"


async def _fetch_html(
    client: httpx.AsyncClient,
    url: str,
    *,
    browser: _BrowserFetcher | None = None,
) -> tuple[str | None, str | None]:
    body, error = await _fetch_text(client, url, browser=browser)
    if body is None:
        return None, error
    text = body.decode("utf-8", errors="ignore")
    low = text[:2000].lower()
    if "cf-browser-verification" in low or "just a moment" in low or "captcha" in low:
        if browser is not None:
            retry, retry_error = await browser.fetch_text(url)
            if retry is not None:
                return retry.decode("utf-8", errors="ignore"), None
            return None, retry_error
        return None, f"blocked_challenge:{url}"
    return text, None


async def _sitemap_urls_from_robots(
    client: httpx.AsyncClient,
    origin: str,
    *,
    browser: _BrowserFetcher | None = None,
) -> list[str]:
    body, _ = await _fetch_text(client, f"{origin}/robots.txt", browser=browser)
    if body is None:
        return []
    urls: list[str] = []
    for line in body.decode("utf-8", errors="ignore").splitlines():
        if line.lower().startswith("sitemap:"):
            raw = line.split(":", 1)[1].strip()
            if raw:
                urls.append(raw)
    return urls


def _extract_search_result_url(raw_href: str) -> str | None:
    if not raw_href:
        return None
    parsed = urlparse(raw_href)
    query = parse_qs(parsed.query)
    for key in ("uddg", "u", "url"):
        value = query.get(key, [None])[0]
        if value and value.startswith(("http://", "https://")):
            return unquote(value)
    if raw_href.startswith(("http://", "https://")):
        return raw_href
    return None


def _extract_search_urls_from_text(text: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"https?://[^\s)\]]+", text):
        raw = match.group(0).rstrip(".,;\"'")
        extracted = _extract_search_result_url(raw)
        if extracted is not None:
            urls.append(extracted)
    return urls


def _search_seed_term_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    segments = [s.lower() for s in parsed.path.strip("/").split("/") if s]
    if not segments:
        return None
    first = segments[0]
    if (
        first in _EXCLUDED_SEGMENTS
        or first in _CATEGORY_HINT_SEGMENTS
        or first in _EXTERNAL_NON_PRODUCT_TERMS
        or first.startswith("cdn-")
        or len(first) < 3
        or len(first) > 40
    ):
        return None
    if "." in first or first.isdigit():
        return None
    return first


def _add_found_url(
    found: list[str],
    found_set: set[str],
    raw_url: str,
    *,
    domain: str,
    external: bool = True,
    dynamic: bool = False,
    max_products: int | None = None,
) -> bool:
    normalized = normalize_generic_product_url(raw_url, domain=domain)
    if normalized is None:
        return False
    if dynamic:
        if not _is_probable_dynamic_product_url(normalized):
            return False
    elif external:
        if not _is_probable_external_product_url(normalized):
            return False
    elif not is_probable_product_url(normalized):
        return False
    if normalized in found_set:
        return False
    found_set.add(normalized)
    found.append(normalized)
    return max_products is not None and len(found) >= max_products


def _iter_json_url_candidates(payload: Any, *, origin: str) -> Iterable[str]:
    if isinstance(payload, dict):
        handle = payload.get("handle")
        if isinstance(handle, str) and handle.strip():
            yield f"{origin}/products/{handle.strip('/')}"
        for key, value in payload.items():
            key_low = str(key).lower()
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    continue
                if key_low in {
                    "url",
                    "href",
                    "link",
                    "permalink",
                    "producturl",
                    "product_url",
                    "canonicalurl",
                    "canonical_url",
                }:
                    yield urljoin(origin, value)
                elif value.startswith(("http://", "https://", "/")) and (
                    "product" in value.lower() or _SLUG_WITH_DIGITS.search(value)
                ):
                    yield urljoin(origin, value)
            elif isinstance(value, (dict, list)):
                yield from _iter_json_url_candidates(value, origin=origin)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_json_url_candidates(item, origin=origin)


def _json_payloads_from_html(html: str) -> Iterable[Any]:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.select("script[type='application/ld+json'], script[type='application/json']"):
        raw = script.string or script.get_text()
        if not raw or len(raw) > _MAX_DYNAMIC_JSON_BYTES:
            continue
        try:
            yield json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue


def _is_douglas_domain(domain: str) -> bool:
    return normalize_domain(domain).removeprefix("www.") == "douglas.bg"


async def probe_magento_graphql(client: httpx.AsyncClient, origin: str) -> bool:
    """Cheap check whether the site exposes a Magento 2 GraphQL endpoint."""
    try:
        resp = await client.post(
            f"{origin}/graphql",
            json={"query": "{storeConfig{store_code}}"},
            headers={"content-type": "application/json"},
        )
    except Exception:  # noqa: BLE001
        return False
    if resp.status_code != 200:
        return False
    try:
        body = resp.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(body, dict) and isinstance(body.get("data"), dict) and "storeConfig" in body["data"]


def _magento_product_url_from_item(item: Any, *, origin: str) -> str | None:
    if not isinstance(item, dict):
        return None
    for key in ("canonical_url", "url_key"):
        value = item.get(key)
        if not isinstance(value, str):
            continue
        slug = value.strip().strip("/")
        if not slug:
            continue
        if slug.startswith(("http://", "https://")):
            return slug
        return urljoin(origin, f"/{slug}")
    return None


def _magento_product_urls_from_item(item: Any, *, origin: str) -> list[str]:
    urls: list[str] = []
    parent_url = _magento_product_url_from_item(item, origin=origin)
    if parent_url:
        urls.append(parent_url)
    if not isinstance(item, dict):
        return urls
    variants = item.get("variants")
    if not isinstance(variants, list):
        return urls
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        product = variant.get("product")
        raw_url = _magento_product_url_from_item(product, origin=origin)
        if raw_url:
            urls.append(raw_url)
    return urls


async def _collect_magento_product_urls_from_graphql(
    origin: str,
    *,
    domain: str,
    max_products: int | None,
    progress_callback: ProgressCallback | None,
) -> tuple[list[str], dict[str, Any]]:
    t0 = time.perf_counter()
    found: list[str] = []
    found_set: set[str] = set()
    errors: list[str] = []
    pages_checked = 0
    total_count = 0
    total_pages = 0

    def emit() -> None:
        if progress_callback is None:
            return
        progress_callback(
            {
                "current_phase": "magento_graphql_products",
                "product_urls_found": len(found),
                "pages_scanned": pages_checked,
            },
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    }

    emit()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                context = await browser.new_context(locale="bg-BG", user_agent=headers["User-Agent"])

                async def route_light_assets(route) -> None:  # type: ignore[no-untyped-def]
                    if route.request.resource_type in {"image", "media", "font", "stylesheet"}:
                        await route.abort()
                    else:
                        await route.continue_()

                await context.route("**/*", route_light_assets)
                page = await context.new_page()
                response = await page.goto(origin, wait_until="domcontentloaded", timeout=30_000)
                if response is None or response.status >= 400:
                    errors.append(f"magento_graphql_home_status_{response.status if response else 'none'}:{origin}")
                await page.wait_for_timeout(1_200)

                current_page = 1
                while max_products is None or len(found) < max_products:
                    payload = {
                        "query": _MAGENTO_GRAPHQL_QUERY,
                        "variables": {
                            "pageSize": _MAGENTO_GRAPHQL_PAGE_SIZE,
                            "currentPage": current_page,
                        },
                    }
                    result = await page.evaluate(
                        """async (payload) => {
                            const response = await fetch('/graphql', {
                                method: 'POST',
                                headers: {'content-type': 'application/json'},
                                body: JSON.stringify(payload),
                            });
                            return {status: response.status, text: await response.text()};
                        }""",
                        payload,
                    )
                    status = int(result.get("status") or 0)
                    if status != 200:
                        errors.append(f"magento_graphql_status_{status}:page_{current_page}")
                        break
                    try:
                        body = json.loads(str(result.get("text") or "{}"))
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        errors.append(f"magento_graphql_json_error:page_{current_page}:{exc}")
                        break

                    products = (body.get("data") or {}).get("products") if isinstance(body, dict) else None
                    if not isinstance(products, dict):
                        errors.append(f"magento_graphql_missing_products:page_{current_page}")
                        break
                    page_info = products.get("page_info") if isinstance(products.get("page_info"), dict) else {}
                    total_count = int(products.get("total_count") or total_count or 0)
                    total_pages = int(page_info.get("total_pages") or total_pages or 0)
                    items = products.get("items") if isinstance(products.get("items"), list) else []
                    if not items:
                        break

                    pages_checked += 1
                    for item in items:
                        for raw_url in _magento_product_urls_from_item(item, origin=origin):
                            done = _add_found_url(
                                found,
                                found_set,
                                raw_url,
                                domain=domain,
                                dynamic=True,
                                max_products=max_products,
                            )
                            if done:
                                break
                        if max_products is not None and len(found) >= max_products:
                            break
                    emit()

                    if total_pages and current_page >= total_pages:
                        break
                    current_page += 1
            finally:
                await browser.close()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"magento_graphql_error:{type(exc).__name__}:{exc}")

    return found, {
        "source": "magento_graphql_products",
        "domain": domain,
        "pages_scanned": pages_checked,
        "graphql_total_count": total_count,
        "graphql_total_pages": total_pages,
        "limit_reached": max_products is not None and len(found) >= max_products,
        "max_products": max_products,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "sample_product_urls": found[:10],
        "errors": errors[:50],
    }


async def _collect_product_links_from_page(
    page: Any,
    found: list[str],
    found_set: set[str],
    *,
    domain: str,
    max_products: int | None,
    search_term: str | None = None,
) -> None:
    page_matches_search = bool(search_term and _url_matches_site_search_term(page.url, search_term))
    try:
        hrefs = await page.locator("a[href]").evaluate_all("(nodes) => nodes.slice(0, 1000).map((n) => n.href)")
    except Exception:
        return
    for href in hrefs:
        if not isinstance(href, str):
            continue
        if search_term:
            href_matches_search = _url_matches_site_search_term(href, search_term)
            if not page_matches_search and not href_matches_search:
                continue
            if page_matches_search and not href_matches_search and not _is_probable_listing_product_url(href, domain=domain):
                continue
        done = _add_found_url(
            found,
            found_set,
            href,
            domain=domain,
            dynamic=True,
            max_products=max_products,
        )
        if done:
            break


async def _common_crawl_indexes(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    try:
        resp = await client.get("https://index.commoncrawl.org/collinfo.json", follow_redirects=True)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


async def collect_generic_product_urls_from_sitemaps(
    site_url_or_domain: str,
    *,
    max_products: int | None = DEFAULT_MAX_PRODUCTS,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Collect likely product URLs from standard sitemap locations for any domain."""
    t0 = time.perf_counter()
    start = normalize_url(site_url_or_domain)
    parsed_start = urlparse(start)
    origin = f"{parsed_start.scheme or 'https'}://{parsed_start.netloc}"
    domain = normalize_domain(start)
    found: list[str] = []
    found_set: set[str] = set()
    checked: list[str] = []
    errors: list[str] = []

    def emit(phase: str) -> None:
        if progress_callback is None:
            return
        progress_callback(
            {
                "current_phase": phase,
                "sitemap_files_checked": len(checked),
                "product_urls_found": len(found),
            },
        )

    # Browser-like headers: some CDNs (e.g. Cloudflare on ardes.bg) return
    # 403 for the default httpx user agent even on robots.txt/sitemaps.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    }
    browser = _BrowserFetcher(origin) if get_settings().discovery_browser_fallback_enabled else None
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=headers, follow_redirects=True) as client:
        robots_sitemaps = await _sitemap_urls_from_robots(client, origin, browser=browser)
        seed_urls = [
            *robots_sitemaps,
            f"{origin}/sitemap.xml",
            f"{origin}/sitemap_index.xml",
            f"{origin}/sitemap-index.xml",
            f"{origin}/sitemap.xml.gz",
            f"{origin}/sitemap.txt",
            # WordPress core (5.5+) serves its own index here even when no SEO
            # plugin provides /sitemap.xml.
            f"{origin}/wp-sitemap.xml",
            # Shopify shards product sitemaps directly.
            f"{origin}/sitemap_products_1.xml",
            # Magento defaults.
            f"{origin}/sitemap/sitemap.xml",
            f"{origin}/pub/sitemap.xml",
            # PrestaShop multi-shop index.
            f"{origin}/1_index_sitemap.xml",
            # OpenCart shops expose their sitemap as a feed route; /sitemap.xml
            # is often missing or broken on them (e.g. bghlapeta.com).
            f"{origin}/index.php?route=extension/feed/google_sitemap",
        ]
        queue: deque[tuple[str, int]] = deque((u, 0) for u in seed_urls)
        seen_sitemaps: set[str] = set()
        emit("reading_sitemap_index")

        while queue and (max_products is None or len(found) < max_products):
            sitemap_url, depth = queue.popleft()
            if sitemap_url in seen_sitemaps or depth > _MAX_SITEMAP_DEPTH:
                continue
            seen_sitemaps.add(sitemap_url)
            checked.append(sitemap_url)

            body, error = await _fetch_text(client, sitemap_url, browser=browser)
            if body is None:
                if error:
                    errors.append(error)
                continue

            page_locs, nested = parse_sitemap_locs(body)
            for nested_url in nested:
                resolved = urljoin(sitemap_url, nested_url)
                if not _same_domain(resolved, domain):
                    continue
                low = resolved.lower()
                if "image" in low or "video" in low or "unavailable" in low:
                    continue
                # Review/rating sitemaps (e.g. Notino's /pregledi/ "reviews")
                # list per-review pages, not products — skip them entirely.
                if "review" in low or "pregled" in low or "rating" in low:
                    continue
                if any(k in low for k in ("product", "prod", "offer")):
                    queue.appendleft((resolved, depth + 1))
                else:
                    queue.append((resolved, depth + 1))

            emit("parsing_product_sitemaps")
            low_sitemap = sitemap_url.lower()
            # A dedicated product sitemap lists product pages directly, so we
            # trust its entries (minus static/excluded noise) instead of running
            # the strict per-URL product heuristic — that heuristic assumes a
            # digit/SKU in the slug and would drop word-only product slugs like
            # Notino's /lancome/o-cool-hair-body-mist... (~88% false negatives).
            # "detail" is how Notino (and other shops) name their PDP sitemaps.
            product_sitemap = any(
                k in low_sitemap for k in ("product", "prod", "offer", "detail", "/pdp")
            )
            for loc in page_locs:
                normalized = normalize_generic_product_url(urljoin(sitemap_url, loc), domain=domain)
                if normalized is None:
                    continue
                if product_sitemap:
                    if _is_static_or_excluded_url(normalized):
                        continue
                elif not is_probable_product_url(normalized):
                    continue
                if normalized in found_set:
                    continue
                found_set.add(normalized)
                found.append(normalized)
                if max_products is not None and len(found) >= max_products:
                    break

    if browser is not None:
        await browser.close()
    found, dropped_outside_hint = _prefer_hinted_product_urls(found)

    return found, {
        "source": "generic_sitemap",
        "domain": domain,
        "limit_reached": max_products is not None and len(found) >= max_products,
        "max_products": max_products,
        "sitemap_urls_checked": len(checked),
        "dropped_outside_product_namespace": dropped_outside_hint,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "sample_product_urls": found[:10],
        "errors": errors[:50],
    }


async def collect_generic_product_urls_from_public_pages(
    site_url_or_domain: str,
    *,
    max_products: int | None = DEFAULT_MAX_PRODUCTS,
    max_pages: int = _MAX_PUBLIC_CRAWL_PAGES,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Fallback discovery by following public same-domain links from the homepage."""
    t0 = time.perf_counter()
    start = normalize_url(site_url_or_domain)
    parsed_start = urlparse(start)
    origin = f"{parsed_start.scheme or 'https'}://{parsed_start.netloc}"
    domain = normalize_domain(start)
    found: list[str] = []
    found_set: set[str] = set()
    visited: set[str] = set()
    errors: list[str] = []
    queue: deque[str] = deque([origin, f"{origin}/"])

    def emit(phase: str) -> None:
        if progress_callback is None:
            return
        progress_callback(
            {
                "current_phase": phase,
                "pages_scanned": len(visited),
                "product_urls_found": len(found),
            },
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=headers, follow_redirects=True) as client:
        emit("crawling_public_pages")
        while queue and len(visited) < max_pages and (max_products is None or len(found) < max_products):
            page_url = queue.popleft()
            normalized_page = normalize_generic_product_url(page_url, domain=domain)
            if normalized_page is None or normalized_page in visited:
                continue
            visited.add(normalized_page)

            html, error = await _fetch_html(client, normalized_page)
            if html is None:
                if error:
                    errors.append(error)
                continue

            soup = BeautifulSoup(html, "html.parser")
            for node in soup.select("a[href]"):
                href = node.get("href") or ""
                absolute = urljoin(normalized_page, href)
                normalized = normalize_generic_product_url(absolute, domain=domain)
                if normalized is None:
                    continue
                if is_probable_product_url(normalized):
                    if normalized not in found_set:
                        found_set.add(normalized)
                        found.append(normalized)
                        if max_products is not None and len(found) >= max_products:
                            break
                    continue
                if _is_probable_listing_url(normalized) and normalized not in visited:
                    queue.append(normalized)
            emit("crawling_public_pages")

    return found, {
        "source": "generic_public_crawl",
        "domain": domain,
        "pages_scanned": len(visited),
        "limit_reached": max_products is not None and len(found) >= max_products,
        "max_products": max_products,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "sample_product_urls": found[:10],
        "errors": errors[:50],
    }


def _pagination_candidates(category_url: str, page_num: int) -> list[tuple[str, str]]:
    """Return (pattern_name, url) probes for one page number of a category."""
    parsed = urlparse(category_url)
    path = parsed.path.rstrip("/")
    query = parse_qs(parsed.query, keep_blank_values=True)
    out: list[tuple[str, str]] = []
    for key in ("page", "p"):
        q = {**query, key: [str(page_num)]}
        out.append((f"query_{key}", urlunparse((parsed.scheme, parsed.netloc, path or "/", "", urlencode(q, doseq=True), ""))))
    out.append(("path_page", urlunparse((parsed.scheme, parsed.netloc, f"{path}/page/{page_num}", "", parsed.query, ""))))
    out.append(("path_num", urlunparse((parsed.scheme, parsed.netloc, f"{path}/{page_num}", "", parsed.query, ""))))
    return out


async def _settle_listing_page(page: Any) -> None:
    for _ in range(3):
        try:
            await page.mouse.wheel(0, 1400)
            await page.wait_for_timeout(700)
        except Exception:
            break


def _site_search_slug(term: str) -> str:
    tokens = [token for token in re.split(r"[^a-zа-я0-9]+", term.lower()) if token]
    return quote("-".join(tokens))


def _site_search_url_candidates(origin: str, term: str) -> list[str]:
    encoded = urlencode({"q": term})
    encoded_search = urlencode({"search": term})
    encoded_query = urlencode({"query": term})
    slug = _site_search_slug(term)
    return [
        f"{origin}/bg/ALL/{slug}/?title={quote(term)}&specialprice=0",
        f"{origin}/bg/{slug}/?title={quote(term)}",
        f"{origin}/ALL/{slug}/?title={quote(term)}&specialprice=0",
        f"{origin}/{slug}/?title={quote(term)}",
        f"{origin}/catalogsearch/result/?{encoded}",
        f"{origin}/catalogsearch/result/index/?{encoded}",
        f"{origin}/search?{encoded}",
        f"{origin}/search?{encoded_query}",
        f"{origin}/search?{encoded_search}",
        f"{origin}/catalogsearch/result/?{encoded}&p=2",
    ]


def _url_matches_site_search_term(url: str, term: str) -> bool:
    term_tokens = [t for t in re.split(r"[^a-zа-я0-9]+", term.lower()) if len(t) >= 3]
    if not term_tokens:
        return True
    haystack = unquote(url).lower()
    return any(token in haystack for token in term_tokens)


async def collect_generic_product_urls_from_category_pagination(
    site_url_or_domain: str,
    *,
    max_products: int | None = DEFAULT_MAX_PRODUCTS,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Discover products by following category/listing pages and probing pagination patterns."""
    t0 = time.perf_counter()
    start = normalize_url(site_url_or_domain)
    parsed_start = urlparse(start)
    origin = f"{parsed_start.scheme or 'https'}://{parsed_start.netloc}"
    domain = normalize_domain(start)
    found: list[str] = []
    found_set: set[str] = set()
    visited_pages: set[str] = set()
    category_queue: deque[str] = deque(
        [
            origin,
            f"{origin}/",
            # Brand index pages link to per-brand listings — good coverage for
            # shops whose sitemap misses category pages.
            f"{origin}/brands",
            f"{origin}/marki",
            f"{origin}/brands.html",
            f"{origin}/marki.html",
        ],
    )
    category_seen: set[str] = set()
    errors: list[str] = []

    def emit() -> None:
        if progress_callback is None:
            return
        progress_callback(
            {
                "current_phase": "category_pagination",
                "pages_scanned": len(visited_pages),
                "product_urls_found": len(found),
            },
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    }
    settings = get_settings()
    max_pagination_pages = settings.discovery_max_pagination_pages
    browser = _BrowserFetcher(origin) if settings.discovery_browser_fallback_enabled else None
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=headers, follow_redirects=True) as client:
        emit()
        # Seed the queue with category/listing URLs from the site's sitemaps —
        # far better coverage than BFS from the homepage alone.
        seed_sitemaps = [
            *(await _sitemap_urls_from_robots(client, origin, browser=browser)),
            f"{origin}/sitemap.xml",
            f"{origin}/index.php?route=extension/feed/google_sitemap",
        ]
        for sitemap_url in seed_sitemaps[:6]:
            body, _seed_error = await _fetch_text(client, sitemap_url, browser=browser)
            if body is None:
                continue
            page_locs, _nested = parse_sitemap_locs(body)
            for loc in page_locs:
                resolved = urljoin(sitemap_url, loc)
                if _same_domain(resolved, domain) and _is_probable_listing_url(resolved):
                    category_queue.append(resolved)
        while (
            category_queue
            and len(visited_pages) < max_pagination_pages
            and (max_products is None or len(found) < max_products)
        ):
            category_url = category_queue.popleft()
            normalized_category = normalize_generic_product_url(category_url, domain=domain)
            if normalized_category is None or normalized_category in category_seen:
                continue
            category_seen.add(normalized_category)

            page_candidates: list[tuple[str, str]] = [("base", normalized_category)]
            for page_num in range(2, _MAX_PAGES_PER_CATEGORY + 1):
                page_candidates.extend(_pagination_candidates(normalized_category, page_num))

            empty_pages_in_row = 0
            pattern_404_streak: dict[str, int] = {}
            # Once one pagination pattern yields products for this category,
            # the sibling patterns are duplicates (?p=N mirrors ?page=N) — lock
            # onto the working one instead of fetching the same content twice.
            working_pattern: str | None = None
            for pattern, page_url in page_candidates:
                if len(visited_pages) >= max_pagination_pages:
                    break
                if max_products is not None and len(found) >= max_products:
                    break
                if working_pattern is not None and pattern not in ("base", working_pattern):
                    continue
                if pattern_404_streak.get(pattern, 0) >= 2:
                    continue
                normalized_page = normalize_generic_product_url(page_url, domain=domain)
                if normalized_page is None or normalized_page in visited_pages:
                    continue
                visited_pages.add(normalized_page)
                html, error = await _fetch_html(client, normalized_page, browser=browser)
                if html is None:
                    if error:
                        errors.append(error)
                        if error.startswith(("status_404:", "browser_status_404:")):
                            pattern_404_streak[pattern] = pattern_404_streak.get(pattern, 0) + 1
                    empty_pages_in_row += 1
                    emit()
                    if empty_pages_in_row >= 4:
                        break
                    continue

                pattern_404_streak.pop(pattern, None)
                before = len(found)
                soup = BeautifulSoup(html, "html.parser")
                for node in soup.select("a[href]"):
                    href = node.get("href") or ""
                    absolute = urljoin(normalized_page, href)
                    normalized = normalize_generic_product_url(absolute, domain=domain)
                    if normalized is None:
                        continue
                    if _is_probable_dynamic_product_url(normalized):
                        if normalized not in found_set:
                            found_set.add(normalized)
                            found.append(normalized)
                            if max_products is not None and len(found) >= max_products:
                                break
                    elif (
                        _is_probable_listing_url(normalized) or _is_brand_listing_url(normalized)
                    ) and normalized not in category_seen:
                        category_queue.append(normalized)
                # schema.org JSON-LD (ItemList/Product) names product pages
                # explicitly — stronger signal than slug heuristics on <a> tags.
                if max_products is None or len(found) < max_products:
                    for payload in _json_payloads_from_html(html):
                        for raw_url in _iter_json_url_candidates(payload, origin=origin):
                            done = _add_found_url(
                                found,
                                found_set,
                                raw_url,
                                domain=domain,
                                dynamic=True,
                                max_products=max_products,
                            )
                            if done:
                                break
                if len(found) > before:
                    empty_pages_in_row = 0
                    if pattern != "base" and working_pattern is None:
                        working_pattern = pattern
                else:
                    empty_pages_in_row += 1
                emit()
                if empty_pages_in_row >= 4:
                    break

    if browser is not None:
        await browser.close()
    return found, {
        "source": "generic_category_pagination",
        "domain": domain,
        "pages_scanned": len(visited_pages),
        "limit_reached": max_products is not None and len(found) >= max_products,
        "max_products": max_products,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "sample_product_urls": found[:10],
        "errors": errors[:50],
    }


async def collect_generic_product_urls_from_search_index(
    site_url_or_domain: str,
    *,
    max_products: int | None = DEFAULT_MAX_PRODUCTS,
    max_queries: int = 16,
    extra_terms: list[str] | None = None,
    patient_mode: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Fallback discovery from public search-result indexes."""
    t0 = time.perf_counter()
    start = normalize_url(site_url_or_domain)
    domain = normalize_domain(start)
    found: list[str] = []
    found_set: set[str] = set()
    errors: list[str] = []
    checked_queries = 0
    rate_limit_pauses = 0

    def emit(phase: str = "searching_external_indexes") -> None:
        if progress_callback is None:
            return
        progress_callback(
            {
                "current_phase": phase,
                "product_urls_found": len(found),
                "external_queries_checked": checked_queries,
                "rate_limit_pauses": rate_limit_pauses,
            },
        )

    seed_terms = [*_SEARCH_SEED_TERMS, *(extra_terms or [])]
    query_queue: deque[str] = deque(f"site:{domain} {term}" for term in seed_terms)
    seen_queries: set[str] = set()
    seen_terms: set[str] = set(seed_terms)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    }
    timeout = httpx.Timeout(_EXTERNAL_SEARCH_TIMEOUT, connect=5.0)
    async with (
        httpx.AsyncClient(timeout=timeout, headers=headers) as client,
        httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}) as reader_client,
    ):
        async def query_external_indexes(query: str) -> tuple[str, list[str], list[str]]:
            query_errors: list[str] = []
            result_urls: list[str] = []
            reader_error: str | None = None
            try:
                reader_resp = await reader_client.get(
                    "https://r.jina.ai/http://duckduckgo.com/html/",
                    params={"q": query},
                    follow_redirects=True,
                )
                if reader_resp.status_code == 200:
                    result_urls = _extract_search_urls_from_text(reader_resp.text)
                else:
                    reader_error = f"search_reader_status_{reader_resp.status_code}:{query}"
            except Exception as exc:  # noqa: BLE001
                reader_error = f"search_reader_error:{query}:{type(exc).__name__}:{exc}"

            if not result_urls:
                direct_error: str | None = None
                try:
                    resp = await client.get(
                        "https://html.duckduckgo.com/html/",
                        params={"q": query},
                        follow_redirects=True,
                    )
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, "html.parser")
                        for node in soup.select("a.result__a[href], a[href]"):
                            raw = _extract_search_result_url(node.get("href") or "")
                            if raw is not None:
                                result_urls.append(raw)
                    else:
                        direct_error = f"search_status_{resp.status_code}:{query}"
                except Exception as exc:  # noqa: BLE001
                    direct_error = f"search_error:{query}:{type(exc).__name__}:{exc}"
                if not result_urls:
                    if reader_error:
                        query_errors.append(reader_error)
                    if direct_error:
                        query_errors.append(direct_error)
            return query, result_urls, query_errors

        emit()
        while query_queue and checked_queries < max_queries:
            if max_products is not None and len(found) >= max_products:
                break
            batch: list[str] = []
            while query_queue and checked_queries + len(batch) < max_queries and len(batch) < _EXTERNAL_SEARCH_BATCH_SIZE:
                query = query_queue.popleft()
                if query in seen_queries:
                    continue
                seen_queries.add(query)
                batch.append(query)
            if not batch:
                continue
            checked_queries += len(batch)
            results = await asyncio.gather(*(query_external_indexes(query) for query in batch))
            batch_rate_limited = False
            for _, result_urls, query_errors in results:
                errors.extend(query_errors)
                if any("status_429:" in error for error in query_errors):
                    batch_rate_limited = True
                for raw in result_urls:
                    normalized = normalize_generic_product_url(raw, domain=domain)
                    if normalized is None:
                        continue
                    seed_term = _search_seed_term_from_url(normalized)
                    if seed_term and seed_term not in seen_terms and len(seen_terms) < max_queries * 2:
                        seen_terms.add(seed_term)
                        query_queue.appendleft(f"site:{domain} {seed_term}")
                    if not _is_probable_external_product_url(normalized):
                        continue
                    if normalized in found_set:
                        continue
                    found_set.add(normalized)
                    found.append(normalized)
                    if max_products is not None and len(found) >= max_products:
                        break
                if max_products is not None and len(found) >= max_products:
                    break
            emit()
            if patient_mode and query_queue and checked_queries < max_queries:
                if batch_rate_limited:
                    rate_limit_pauses += 1
                    emit("waiting_external_rate_limit")
                    await asyncio.sleep(_EXTERNAL_SEARCH_RATE_LIMIT_DELAY)
                else:
                    await asyncio.sleep(_EXTERNAL_SEARCH_PATIENT_BATCH_DELAY)

    return found, {
        "source": "generic_external_search",
        "domain": domain,
        "queries_checked": checked_queries,
        "rate_limit_pauses": rate_limit_pauses,
        "patient_mode": patient_mode,
        "limit_reached": max_products is not None and len(found) >= max_products,
        "max_products": max_products,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "sample_product_urls": found[:10],
        "errors": errors[:50],
    }


async def collect_generic_product_urls_from_common_crawl(
    site_url_or_domain: str,
    *,
    max_products: int | None = DEFAULT_MAX_PRODUCTS,
    max_indexes: int = 6,
    max_index_results: int = _MAX_EXTERNAL_INDEX_RESULTS,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Fallback discovery from Common Crawl URL indexes."""
    t0 = time.perf_counter()
    start = normalize_url(site_url_or_domain)
    domain = normalize_domain(start)
    found: list[str] = []
    found_set: set[str] = set()
    errors: list[str] = []
    indexes_checked = 0

    def emit() -> None:
        if progress_callback is None:
            return
        progress_callback(
            {
                "current_phase": "searching_external_indexes",
                "product_urls_found": len(found),
            },
        )

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        indexes = await _common_crawl_indexes(client)
        emit()
        for index in indexes[:max_indexes]:
            if max_products is not None and len(found) >= max_products:
                break
            api = index.get("cdx-api")
            if not api:
                continue
            indexes_checked += 1
            try:
                resp = await client.get(
                    str(api),
                    params={
                        "url": f"{domain}/*",
                        "output": "json",
                        "fl": "url,status",
                        "filter": "status:200",
                        "collapse": "urlkey",
                        "limit": str(max_index_results),
                    },
                    follow_redirects=True,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"common_crawl_error:{index.get('id')}:{type(exc).__name__}:{exc}")
                continue
            if resp.status_code != 200:
                errors.append(f"common_crawl_status_{resp.status_code}:{index.get('id')}")
                continue

            for line in resp.text.splitlines():
                if max_products is not None and len(found) >= max_products:
                    break
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                raw = str(row.get("url") or "")
                normalized = normalize_generic_product_url(raw, domain=domain)
                if normalized is None or not _is_probable_external_product_url(normalized):
                    continue
                if normalized in found_set:
                    continue
                found_set.add(normalized)
                found.append(normalized)
            emit()

    return found, {
        "source": "generic_common_crawl",
        "domain": domain,
        "indexes_checked": indexes_checked,
        "limit_reached": max_products is not None and len(found) >= max_products,
        "max_products": max_products,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "sample_product_urls": found[:10],
        "errors": errors[:50],
    }


async def collect_generic_product_urls_from_wayback(
    site_url_or_domain: str,
    *,
    max_products: int | None = DEFAULT_MAX_PRODUCTS,
    max_index_results: int = _MAX_EXTERNAL_INDEX_RESULTS,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Discovery from the Wayback Machine CDX index (same idea as Common Crawl)."""
    t0 = time.perf_counter()
    domain = normalize_domain(normalize_url(site_url_or_domain))
    found: list[str] = []
    found_set: set[str] = set()
    errors: list[str] = []

    if progress_callback is not None:
        progress_callback({"current_phase": "searching_external_indexes", "product_urls_found": 0})
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        try:
            resp = await client.get(
                "https://web.archive.org/cdx/search/cdx",
                params={
                    "url": f"{domain}/*",
                    "output": "json",
                    "fl": "original",
                    "filter": "statuscode:200",
                    "collapse": "urlkey",
                    "limit": str(max_index_results),
                },
                follow_redirects=True,
            )
            if resp.status_code != 200:
                errors.append(f"wayback_status_{resp.status_code}")
            else:
                rows = resp.json()
                for row in rows[1:] if isinstance(rows, list) else []:
                    if max_products is not None and len(found) >= max_products:
                        break
                    raw = str(row[0]) if isinstance(row, list) and row else ""
                    normalized = normalize_generic_product_url(raw, domain=domain)
                    if normalized is None or not _is_probable_external_product_url(normalized):
                        continue
                    if normalized in found_set:
                        continue
                    found_set.add(normalized)
                    found.append(normalized)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"wayback_error:{type(exc).__name__}:{exc}")

    if progress_callback is not None:
        progress_callback({"current_phase": "searching_external_indexes", "product_urls_found": len(found)})
    return found, {
        "source": "generic_wayback",
        "domain": domain,
        "queries_checked": 1,
        "limit_reached": max_products is not None and len(found) >= max_products,
        "max_products": max_products,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "sample_product_urls": found[:10],
        "errors": errors[:50],
    }


async def collect_generic_product_urls_from_merchant_feeds(
    site_url_or_domain: str,
    *,
    max_products: int | None = DEFAULT_MAX_PRODUCTS,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Discovery from Google Shopping / Facebook catalog feeds at well-known paths.

    A merchant feed lists product pages by definition, so entries are trusted
    (minus static/excluded noise) instead of running slug heuristics."""
    t0 = time.perf_counter()
    start = normalize_url(site_url_or_domain)
    parsed_start = urlparse(start)
    origin = f"{parsed_start.scheme or 'https'}://{parsed_start.netloc}"
    domain = normalize_domain(start)
    found: list[str] = []
    found_set: set[str] = set()
    errors: list[str] = []
    feeds_checked = 0
    feeds_with_products = 0

    def emit() -> None:
        if progress_callback is not None:
            progress_callback({"current_phase": "reading_merchant_feeds", "product_urls_found": len(found)})

    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "application/xml,text/xml,application/rss+xml,*/*;q=0.8",
        "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    }
    emit()
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=headers, follow_redirects=True) as client:
        for feed_path in _MERCHANT_FEED_PATHS:
            if max_products is not None and len(found) >= max_products:
                break
            feed_url = urljoin(origin, feed_path)
            feeds_checked += 1
            body, error = await _fetch_text(client, feed_url)
            if body is None:
                if error and not error.startswith("status_404"):
                    errors.append(error)
                continue
            page_locs, _nested = parse_sitemap_locs(body)
            before = len(found)
            for loc in page_locs:
                normalized = normalize_generic_product_url(urljoin(feed_url, loc), domain=domain)
                if normalized is None or _is_static_or_excluded_url(normalized):
                    continue
                if normalized in found_set:
                    continue
                found_set.add(normalized)
                found.append(normalized)
                if max_products is not None and len(found) >= max_products:
                    break
            if len(found) > before:
                feeds_with_products += 1
                emit()

    return found, {
        "source": "generic_merchant_feeds",
        "domain": domain,
        "feeds_checked": feeds_checked,
        "feeds_with_products": feeds_with_products,
        "limit_reached": max_products is not None and len(found) >= max_products,
        "max_products": max_products,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "sample_product_urls": found[:10],
        "errors": errors[:50],
    }


async def collect_generic_product_urls_from_autocomplete(
    site_url_or_domain: str,
    *,
    max_products: int | None = DEFAULT_MAX_PRODUCTS,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Discovery via the shop's own search-suggest/autocomplete JSON endpoint,
    enumerated with single-letter prefixes (Latin + Cyrillic)."""
    t0 = time.perf_counter()
    start = normalize_url(site_url_or_domain)
    parsed_start = urlparse(start)
    origin = f"{parsed_start.scheme or 'https'}://{parsed_start.netloc}"
    domain = normalize_domain(start)
    found: list[str] = []
    found_set: set[str] = set()
    errors: list[str] = []
    queries_checked = 0

    def emit() -> None:
        if progress_callback is not None:
            progress_callback(
                {
                    "current_phase": "probing_autocomplete",
                    "product_urls_found": len(found),
                    "external_queries_checked": queries_checked,
                },
            )

    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json,*/*;q=0.8",
        "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    }

    async def query_endpoint(client: httpx.AsyncClient, template: str, term: str) -> int:
        nonlocal queries_checked
        endpoint = urljoin(origin, template.format(q=quote(term)))
        queries_checked += 1
        try:
            resp = await client.get(endpoint)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"autocomplete_error:{endpoint}:{type(exc).__name__}")
            return 0
        if resp.status_code != 200:
            return 0
        text = resp.text.lstrip()
        if not text.startswith(("{", "[")) or len(resp.content) > _MAX_DYNAMIC_JSON_BYTES:
            return 0
        try:
            payload = resp.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 0
        added = 0
        for raw_url in _iter_json_url_candidates(payload, origin=origin):
            before = len(found)
            done = _add_found_url(found, found_set, raw_url, domain=domain, dynamic=True, max_products=max_products)
            added += len(found) - before
            if done:
                break
        return added

    emit()
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=headers, follow_redirects=True) as client:
        # Probe each endpoint template with one term; enumerate only working ones.
        working_template: str | None = None
        for template in _AUTOCOMPLETE_ENDPOINT_TEMPLATES:
            if await query_endpoint(client, template, "a") > 0:
                working_template = template
                break
        if working_template is not None:
            for prefix in _AUTOCOMPLETE_PREFIXES:
                if max_products is not None and len(found) >= max_products:
                    break
                await query_endpoint(client, working_template, prefix)
                emit()

    return found, {
        "source": "generic_autocomplete",
        "domain": domain,
        "queries_checked": queries_checked,
        "working_endpoint": working_template,
        "limit_reached": max_products is not None and len(found) >= max_products,
        "max_products": max_products,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "sample_product_urls": found[:10],
        "errors": errors[:50],
    }


async def collect_generic_product_urls_from_dynamic_endpoints(
    site_url_or_domain: str,
    *,
    max_products: int | None = DEFAULT_MAX_PRODUCTS,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Fallback discovery from public ecommerce JSON endpoints and Playwright XHR responses."""
    t0 = time.perf_counter()
    start = normalize_url(site_url_or_domain)
    parsed_start = urlparse(start)
    origin = f"{parsed_start.scheme or 'https'}://{parsed_start.netloc}"
    domain = normalize_domain(start)
    found: list[str] = []
    found_set: set[str] = set()
    errors: list[str] = []
    endpoints_checked = 0
    json_payloads_checked = 0
    xhr_responses_checked = 0

    def emit(phase: str = "sniffing_dynamic_endpoints") -> None:
        if progress_callback is None:
            return
        progress_callback(
            {
                "current_phase": phase,
                "product_urls_found": len(found),
                "pages_scanned": 1,
            },
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    }

    emit()
    # Magento 2 exposes the full catalog over /graphql — works for any Magento
    # shop (douglas.bg is one), so detect it generically instead of hardcoding.
    is_magento = _is_douglas_domain(domain)
    if not is_magento:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=headers, follow_redirects=True) as probe_client:
            is_magento = await probe_magento_graphql(probe_client, origin)
    if is_magento:
        douglas_urls, douglas_diag = await _collect_magento_product_urls_from_graphql(
            origin,
            domain=domain,
            max_products=max_products,
            progress_callback=progress_callback,
        )
        for raw_url in douglas_urls:
            done = _add_found_url(
                found,
                found_set,
                raw_url,
                domain=domain,
                dynamic=True,
                max_products=max_products,
            )
            if done:
                break
        errors.extend(str(e) for e in douglas_diag.get("errors") or [])
        json_payloads_checked += int(douglas_diag.get("pages_scanned", 0) or 0)
        emit("magento_graphql_products")
        if found or (max_products is not None and len(found) >= max_products):
            return found, {
                "source": "generic_dynamic_endpoints+magento_graphql_products",
                "domain": domain,
                "endpoints_checked": endpoints_checked,
                "json_payloads_checked": json_payloads_checked,
                "xhr_responses_checked": xhr_responses_checked,
                "pages_scanned": int(douglas_diag.get("pages_scanned", 0) or 0),
                "graphql_total_count": douglas_diag.get("graphql_total_count", 0),
                "graphql_total_pages": douglas_diag.get("graphql_total_pages", 0),
                "limit_reached": max_products is not None and len(found) >= max_products,
                "max_products": max_products,
                "duration_ms": int((time.perf_counter() - t0) * 1000),
                "sample_product_urls": found[:10],
                "errors": errors[:50],
            }

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=headers, follow_redirects=True) as client:
        for endpoint_template in _DYNAMIC_ENDPOINT_PATHS:
            for page in range(1, _MAX_DYNAMIC_ENDPOINT_PAGES + 1):
                if max_products is not None and len(found) >= max_products:
                    break
                endpoint = urljoin(origin, endpoint_template.format(page=page))
                endpoints_checked += 1
                try:
                    resp = await client.get(endpoint)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"dynamic_endpoint_error:{endpoint}:{type(exc).__name__}:{exc}")
                    continue
                if resp.status_code in (404, 410):
                    break
                if resp.status_code != 200:
                    errors.append(f"dynamic_endpoint_status_{resp.status_code}:{endpoint}")
                    continue
                content_type = resp.headers.get("content-type", "")
                if "json" not in content_type.lower() and not resp.text.lstrip().startswith(("{", "[")):
                    continue
                if len(resp.content) > _MAX_DYNAMIC_JSON_BYTES:
                    errors.append(f"dynamic_endpoint_too_large:{endpoint}")
                    continue
                try:
                    payload = resp.json()
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                json_payloads_checked += 1
                before = len(found)
                for raw_url in _iter_json_url_candidates(payload, origin=origin):
                    done = _add_found_url(
                        found,
                        found_set,
                        raw_url,
                        domain=domain,
                        dynamic=True,
                        max_products=max_products,
                    )
                    if done:
                        break
                emit()
                if len(found) == before:
                    break

        if max_products is None or len(found) < max_products:
            try:
                home_resp = await client.get(origin)
                if home_resp.status_code == 200 and len(home_resp.content) <= _MAX_DYNAMIC_JSON_BYTES:
                    for payload in _json_payloads_from_html(home_resp.text):
                        json_payloads_checked += 1
                        for raw_url in _iter_json_url_candidates(payload, origin=origin):
                            done = _add_found_url(
                                found,
                                found_set,
                                raw_url,
                                domain=domain,
                                dynamic=True,
                                max_products=max_products,
                            )
                            if done:
                                break
                elif home_resp.status_code != 200:
                    errors.append(f"dynamic_home_status_{home_resp.status_code}:{origin}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"dynamic_home_error:{origin}:{type(exc).__name__}:{exc}")

    if max_products is None or len(found) < max_products:
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                try:
                    context = await browser.new_context(locale="bg-BG", user_agent=headers["User-Agent"])
                    page = await context.new_page()

                    async def handle_response(response) -> None:  # type: ignore[no-untyped-def]
                        nonlocal xhr_responses_checked, json_payloads_checked
                        if max_products is not None and len(found) >= max_products:
                            return
                        if xhr_responses_checked >= _MAX_PLAYWRIGHT_RESPONSES:
                            return
                        request = response.request
                        if request.resource_type not in {"xhr", "fetch", "document"}:
                            return
                        raw_response_url = response.url
                        _add_found_url(
                            found,
                            found_set,
                            raw_response_url,
                            domain=domain,
                            dynamic=True,
                            max_products=max_products,
                        )
                        content_type = response.headers.get("content-type", "")
                        if "json" not in content_type.lower():
                            return
                        xhr_responses_checked += 1
                        try:
                            body = await response.body()
                        except Exception as exc:  # noqa: BLE001
                            errors.append(f"dynamic_xhr_body_error:{raw_response_url}:{type(exc).__name__}:{exc}")
                            return
                        if len(body) > _MAX_DYNAMIC_JSON_BYTES:
                            errors.append(f"dynamic_xhr_too_large:{raw_response_url}")
                            return
                        try:
                            payload = json.loads(body.decode("utf-8", errors="ignore"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            return
                        json_payloads_checked += 1
                        for raw_url in _iter_json_url_candidates(payload, origin=origin):
                            done = _add_found_url(
                                found,
                                found_set,
                                raw_url,
                                domain=domain,
                                dynamic=True,
                                max_products=max_products,
                            )
                            if done:
                                break

                    page.on("response", lambda response: asyncio.create_task(handle_response(response)))
                    await page.goto(origin, wait_until="domcontentloaded", timeout=20_000)
                    await page.wait_for_timeout(4_000)
                    for node in await page.locator("a[href]").evaluate_all(
                        "(nodes) => nodes.slice(0, 500).map((n) => n.href)",
                    ):
                        if isinstance(node, str):
                            done = _add_found_url(
                                found,
                                found_set,
                                node,
                                domain=domain,
                                dynamic=True,
                                max_products=max_products,
                            )
                            if done:
                                break
                finally:
                    await browser.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"dynamic_playwright_error:{type(exc).__name__}:{exc}")

    if max_products is not None and len(found) > max_products:
        del found[max_products:]
    emit()
    return found, {
        "source": "generic_dynamic_endpoints",
        "domain": domain,
        "endpoints_checked": endpoints_checked,
        "json_payloads_checked": json_payloads_checked,
        "xhr_responses_checked": xhr_responses_checked,
        "pages_scanned": 1,
        "limit_reached": max_products is not None and len(found) >= max_products,
        "max_products": max_products,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "sample_product_urls": found[:10],
        "errors": errors[:50],
    }


async def collect_generic_product_urls_from_site_search(
    site_url_or_domain: str,
    *,
    search_terms: list[str],
    max_products: int | None = DEFAULT_MAX_PRODUCTS,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Use the site's own search UI/API to discover product URLs for selected brands/terms."""
    t0 = time.perf_counter()
    start = normalize_url(site_url_or_domain)
    parsed_start = urlparse(start)
    origin = f"{parsed_start.scheme or 'https'}://{parsed_start.netloc}"
    domain = normalize_domain(start)
    terms: list[str] = []
    seen_terms: set[str] = set()
    for raw in search_terms:
        term = " ".join(str(raw).split()).strip()
        if len(term) < 2:
            continue
        key = term.lower()
        if key in seen_terms:
            continue
        seen_terms.add(key)
        terms.append(term)
        if len(terms) >= _MAX_SITE_SEARCH_TERMS:
            break

    found: list[str] = []
    found_set: set[str] = set()
    errors: list[str] = []
    terms_checked = 0
    pages_scanned = 0
    xhr_responses_checked = 0
    json_payloads_checked = 0

    def emit() -> None:
        if progress_callback is None:
            return
        progress_callback(
            {
                "current_phase": "site_search",
                "product_urls_found": len(found),
                "pages_scanned": pages_scanned,
                "external_queries_checked": terms_checked,
            },
        )

    if not terms:
        return found, {
            "source": "generic_site_search",
            "domain": domain,
            "terms_checked": 0,
            "pages_scanned": 0,
            "duration_ms": int((time.perf_counter() - t0) * 1000),
            "sample_product_urls": [],
            "errors": ["site_search_no_terms"],
        }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    }

    emit()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                context = await browser.new_context(locale="bg-BG", user_agent=headers["User-Agent"])
                page = await context.new_page()
                active_search = False
                active_search_term = ""

                async def handle_response(response) -> None:  # type: ignore[no-untyped-def]
                    nonlocal xhr_responses_checked, json_payloads_checked
                    if not active_search:
                        return
                    if max_products is not None and len(found) >= max_products:
                        return
                    if xhr_responses_checked >= _MAX_PLAYWRIGHT_RESPONSES:
                        return
                    if response.request.resource_type not in {"xhr", "fetch"}:
                        return
                    if normalize_domain(response.url) != domain:
                        return
                    content_type = response.headers.get("content-type", "")
                    if "json" not in content_type.lower():
                        return
                    xhr_responses_checked += 1
                    try:
                        body = await response.body()
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"site_search_xhr_body_error:{response.url}:{type(exc).__name__}:{exc}")
                        return
                    if len(body) > _MAX_DYNAMIC_JSON_BYTES:
                        return
                    try:
                        payload = json.loads(body.decode("utf-8", errors="ignore"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        return
                    json_payloads_checked += 1
                    for raw_url in _iter_json_url_candidates(payload, origin=origin):
                        if active_search_term and not _url_matches_site_search_term(raw_url, active_search_term):
                            continue
                        done = _add_found_url(found, found_set, raw_url, domain=domain, dynamic=True, max_products=max_products)
                        if done:
                            break

                page.on("response", lambda response: asyncio.create_task(handle_response(response)))

                for term in terms:
                    if max_products is not None and len(found) >= max_products:
                        break
                    terms_checked += 1
                    await page.goto(origin, wait_until="domcontentloaded", timeout=20_000)
                    pages_scanned += 1
                    active_search = True
                    active_search_term = term
                    search_input = None
                    for selector in _SEARCH_INPUT_SELECTORS:
                        locator = page.locator(selector).first
                        try:
                            if await locator.count() > 0 and await locator.is_visible(timeout=1_000):
                                search_input = locator
                                break
                        except Exception:
                            continue
                    if search_input is None:
                        errors.append(f"site_search_input_not_found:{term}")
                        break

                    await search_input.fill(term)
                    await search_input.press("Enter")
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=8_000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(1_500)
                    await _settle_listing_page(page)
                    await _collect_product_links_from_page(
                        page,
                        found,
                        found_set,
                        domain=domain,
                        max_products=max_products,
                        search_term=term,
                    )
                    emit()

                    for search_url in _site_search_url_candidates(origin, term):
                        if max_products is not None and len(found) >= max_products:
                            break
                        try:
                            await page.goto(search_url, wait_until="domcontentloaded", timeout=12_000)
                            pages_scanned += 1
                            await page.wait_for_timeout(1_000)
                            await _settle_listing_page(page)
                            await _collect_product_links_from_page(
                                page,
                                found,
                                found_set,
                                domain=domain,
                                max_products=max_products,
                                search_term=term,
                            )
                            emit()
                        except Exception:
                            continue

                    for _ in range(1, _MAX_SITE_SEARCH_RESULT_PAGES):
                        if max_products is not None and len(found) >= max_products:
                            break
                        next_link = page.locator(
                            "a[rel='next'], a.next, .next a, [aria-label*='Next'], [aria-label*='след']",
                        ).first
                        try:
                            if await next_link.count() == 0 or not await next_link.is_visible(timeout=800):
                                break
                            await next_link.click()
                            pages_scanned += 1
                            await page.wait_for_load_state("domcontentloaded", timeout=8_000)
                            await page.wait_for_timeout(1_000)
                            await _settle_listing_page(page)
                            await _collect_product_links_from_page(
                                page,
                                found,
                                found_set,
                                domain=domain,
                                max_products=max_products,
                                search_term=term,
                            )
                            emit()
                        except Exception:
                            break
            finally:
                await browser.close()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"site_search_playwright_error:{type(exc).__name__}:{exc}")

    return found, {
        "source": "generic_site_search",
        "domain": domain,
        "terms_checked": terms_checked,
        "pages_scanned": pages_scanned,
        "xhr_responses_checked": xhr_responses_checked,
        "json_payloads_checked": json_payloads_checked,
        "limit_reached": max_products is not None and len(found) >= max_products,
        "max_products": max_products,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
        "sample_product_urls": found[:10],
        "errors": errors[:50],
    }
