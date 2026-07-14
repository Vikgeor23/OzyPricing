"""Registry maps retailer domains to scraper implementations."""

from app.scrapers.base import BaseScraper
from app.scrapers.sites.generic import GenericProductScraper
from app.scrapers.sites.technopolis import TechnopolisScraper
from app.utils.url_utils import TECHNOPOLIS_DOMAIN, is_technopolis, normalize_domain


def get_scraper_for_domain(
    domain: str,
    url: str,
    *,
    preferred_currency: str | None = None,
    generic_playwright_pool: object | None = None,
) -> BaseScraper:
    """Resolve scraper by competitor domain and/or listing URL host.

    Technopolis is used when the normalized domain or URL host is ``technopolis.bg``.
    Everything else falls back to the generic product scraper.
    """

    host = normalize_domain(domain)
    url_host = normalize_domain(url) if url else ""
    if host == TECHNOPOLIS_DOMAIN or url_host == TECHNOPOLIS_DOMAIN or is_technopolis(url):
        return TechnopolisScraper(url)
    return GenericProductScraper(
        url,
        preferred_currency=preferred_currency,
        playwright_pool=generic_playwright_pool,
    )
