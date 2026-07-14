"""Application configuration (Pydantic Settings)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Price Monitor API"
    debug: bool = False

    # Optional URL prefix when behind a reverse proxy (e.g. Cloudflare /api/* → backend).
    # Local dev: leave empty so routes stay at /health, /competitors, etc.
    api_prefix: str = ""

    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/pricing_monitor"
    database_pool_size: int = 10
    database_max_overflow: int = 30
    database_pool_timeout_sec: int = 30
    # Per-connection guardrails. Set ONLY on the web/backend process (via Compose
    # env) so a runaway interactive query (e.g. a workspace search over a
    # million-row competitor) fails fast instead of pinning the DB for everyone.
    # The celery worker must NOT set these — its batch jobs run for minutes.
    # 0 / "" disables. work_mem also lets the workspace sort stay in memory.
    db_statement_timeout_ms: int = 0
    db_work_mem: str = ""
    redis_url: str = "redis://localhost:6379/0"

    # Optional Celery overrides (Compose sets these; default to redis_url).
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None

    cors_origins: str = (
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:3001,http://127.0.0.1:3001"
    )

    # Registration allowlist by email domain (comma-separated). Not disclosed
    # to clients — other domains get a neutral "contact us" message.
    auth_allowed_email_domains: str = "ozone.bg"
    auth_token_ttl_days: int = 30

    # When false (default), scrapes update competitor_products.latest_* only — no new PriceSnapshot rows.
    price_history_enabled: bool = False

    # Batch scrape performance (hybrid HTTP + shared Playwright pool).
    scrape_concurrency: int = 12
    scrape_concurrency_min: int = 6
    scrape_concurrency_max: int = 20
    scrape_generic_concurrency: int = 32
    scrape_generic_concurrency_min: int = 8
    scrape_generic_concurrency_max: int = 48
    # Number of reused Chromium browser processes for generic batch scraping.
    # A fresh context per request runs on top of these; browsers are the heavy
    # resource. ~16 suits a 16-core/32-thread host (5950X); lower it on smaller
    # boxes (each browser is ~150-300MB RAM).
    scrape_generic_browser_pool_size: int = 16
    # Batch scrapes for douglas.bg use the bulk GraphQL feed (one browser
    # session for the whole catalog) instead of per-URL scraping.
    scrape_douglas_bulk_enabled: bool = True
    # Other Magento shops with an open /graphql endpoint get the same bulk
    # treatment over plain HTTP (no browser). Comma-separated domains.
    scrape_magento_bulk_domains: str = "hippoland.net"
    # Probe unknown domains for a usable Magento GraphQL endpoint at the start
    # of every batch scrape and use the fastest transport automatically.
    scrape_magento_bulk_autodetect: bool = True
    # Active douglas.bg coupon campaign. The API exposes only an eligibility
    # flag per product (promo_list_<letter>), not the coupon price, so the
    # campaign mapping is configured here. Clear the flag attr to disable.
    douglas_promo_flag_attr: str = "promo_list_w"
    douglas_promo_percent: float = 30.0
    douglas_promo_code: str = "BLACK30"
    scrape_douglas_concurrency: int = 4
    scrape_douglas_concurrency_min: int = 1
    scrape_douglas_concurrency_max: int = 4
    scrape_adaptive_window_high: int = 100
    scrape_adaptive_window_low: int = 200
    scrape_adaptive_timeout_rate_high: float = 0.30
    scrape_adaptive_timeout_rate_low: float = 0.10
    # Throughput-driven concurrency: instead of climbing to the configured max
    # while timeouts stay low, the controller hill-climbs on measured
    # completions/min and settles where extra parallelism stops paying for
    # itself (each slot can cost a browser). The configured max remains only a
    # hard ceiling. Thresholds scale with the probe step: an up-step of X% is
    # kept when it gains at least gain_ratio*X% throughput (linear scaling
    # passes, the knee fails); a down-step is kept while it loses at most
    # loss_ratio*X%.
    scrape_adaptive_throughput_enabled: bool = True
    scrape_adaptive_tpm_window: int = 80
    scrape_adaptive_tpm_min_secs: float = 20.0
    scrape_adaptive_tpm_gain_ratio: float = 0.5
    scrape_adaptive_tpm_loss_ratio: float = 0.3
    scrape_adaptive_tpm_hold_windows: int = 3
    # Offload generic HTML parsing (BeautifulSoup + selectors) to a small
    # process pool so a big batch is not capped by the single CPU core of its
    # celery worker process — on heavy pages (~300KB) parsing dominates and
    # the event loop serializes it. Falls back to inline parsing if the pool
    # cannot start. Pages smaller than min_bytes parse inline (IPC not worth it).
    scrape_parse_offload_enabled: bool = True
    scrape_parse_pool_size: int = 4
    scrape_parse_offload_min_bytes: int = 30_000
    # A PROGRESS task meta whose heartbeat_at is older than this is reported as
    # FAILURE by the poll endpoints — the worker died (restart/OOM) and Celery
    # never finalizes the meta, so the UI would show "Scraping..." forever.
    scrape_progress_stale_sec: int = 300
    # Batch circuit breaker: this many consecutive blocked responses
    # (captcha / 403 / 429 / 511 on the browser layer) stop the run — the site
    # is refusing us and hammering on only makes the block longer.
    scrape_block_stop_streak: int = 15
    scrape_http_enabled: bool = False
    scrape_occ_enabled: bool = True
    scrape_occ_timeout_sec: float = 15.0
    scrape_navigation_timeout_ms: int = 10_000
    scrape_retry_navigation_timeout_ms: int = 15_000
    scrape_title_wait_ms: int = 2_000
    scrape_price_selector_wait_ms: int = 3_000
    scrape_selector_timeout_ms: int = 2_000
    scrape_http_timeout_sec: float = 15.0
    scrape_progress_interval_sec: float = 3.0
    scrape_batch_commit_size: int = 20
    scrape_skip_recent_failures: bool = True
    scrape_recent_failure_hours: int = 24
    scrape_skip_dead_urls: bool = True

    # Configurable-product expansion: when a scraped page (e.g. Notino) embeds
    # multiple size variants, each with its own p-id URL / EAN / price, create a
    # sibling listing row per variant so every size is tracked on its own line.
    scrape_expand_variants: bool = True

    # Discovery: when httpx is blocked (HTTP 403 / Cloudflare "just a moment"
    # challenge), retry the same URL through a real Chromium navigation that
    # can pass JS/managed challenges. Headful (under a virtual X display) beats
    # interactive Turnstile challenges that headless cannot solve.
    discovery_browser_fallback_enabled: bool = True
    discovery_browser_headful: bool = True
    discovery_browser_challenge_wait_sec: float = 20.0
    discovery_browser_max_pages: int = 220
    # Global page budget for generic category-pagination discovery per run.
    # High on purpose: sites with many categories (e.g. OpenCart shops) need
    # thousands of listing pages for full coverage; small sites exhaust their
    # queue long before hitting this.
    discovery_max_pagination_pages: int = 5000

    @property
    def api_prefix_path(self) -> str:
        """Normalized prefix without trailing slash, e.g. ``/api`` or ``""``."""
        raw = (self.api_prefix or "").strip()
        if not raw:
            return ""
        if not raw.startswith("/"):
            raw = f"/{raw}"
        return raw.rstrip("/")

    @property
    def effective_celery_broker(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def effective_celery_backend(self) -> str:
        return self.celery_result_backend or self.redis_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
