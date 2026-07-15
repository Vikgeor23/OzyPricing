export type UUID = string;

export type Product = {
  id: UUID;
  tenant_id: UUID | null;
  sku: string;
  ean: string | null;
  brand: string | null;
  name: string;
  category: string | null;
  manufacturer_code: string | null;
  model: string | null;
  own_price: string | null;
  cost_price: string | null;
  stock_quantity: string | null;
  product_url: string | null;
  image_url: string | null;
  description: string | null;
  variant: string | null;
  color: string | null;
  size: string | null;
  storage: string | null;
  memory: string | null;
  supplier_sku: string | null;
  created_at: string;
  updated_at: string;
};

export type Competitor = {
  id: UUID;
  name: string;
  domain: string;
  country: string | null;
  currency: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
};

export type CompetitorProduct = {
  id: UUID;
  competitor_id: UUID;
  product_id: UUID | null;
  url: string;
  title: string | null;
  brand: string | null;
  ean: string | null;
  sku: string | null;
  image_url: string | null;
  last_seen_at: string | null;
  created_at: string;
  updated_at: string;
};

export type CompetitorPriceLine = {
  competitor_id?: UUID | null;
  competitor_name: string;
  domain: string;
  price: string | null;
  currency: string;
  availability: string | null;
  competitor_product_id: UUID;
  title?: string | null;
  url?: string | null;
  latest_price?: string | null;
  latest_promo_price?: string | null;
  latest_old_price?: string | null;
  latest_scraped_at?: string | null;
  match_status?: string | null;
  match_score?: string | null;
  match_method?: string | null;
};

export type PriceComparisonRow = {
  product_id: UUID;
  sku: string;
  ean: string | null;
  brand: string | null;
  name: string;
  category: string | null;
  manufacturer_code: string | null;
  own_price: string | null;
  competitor_prices: CompetitorPriceLine[];
  lowest_competitor_price: string | null;
  difference_percent: string | null;
  last_checked_at: string | null;
  status: string;
};

export type CompetitorProductOverview = {
  id: UUID;
  competitor_id: UUID;
  competitor_name: string;
  url: string;
  title: string | null;
  last_seen_at: string | null;
  latest_price: string | null;
  currency: string;
  availability: string | null;
  product_id: UUID | null;
  match_status: string | null;
  match_score: string | null;
  match_method: string | null;
  matched_sku: string | null;
  matched_product_name: string | null;
};

export type MatchCandidate = {
  product_id: UUID;
  sku: string;
  name: string;
  brand: string | null;
  ean: string | null;
  manufacturer_code: string | null;
  model: string | null;
  image_url?: string | null;
  own_price: string | null;
  match_score: string;
  match_method: string;
  match_reasons: string[];
  match_warnings: string[];
  suggested_status: string;
};

export type DashboardProductRow = {
  product_id: UUID;
  name: string;
  sku: string;
  own_price: string | null;
  lowest_competitor_price: string | null;
  difference_percent: string | null;
  last_checked_at: string | null;
};

/** Nested category from GET /competitors/tree */
export type CategoryTreeNode = {
  id: UUID;
  parent_id: UUID | null;
  name: string;
  url: string;
  level: number;
  product_count: number;
  children: CategoryTreeNode[];
};

export type CompetitorTreeItem = {
  id: UUID;
  name: string;
  domain: string;
  country: string | null;
  currency: string;
  scrape_concurrency_max: number | null;
  categories: CategoryTreeNode[];
};

export type CompetitorProductAddResponse = {
  id: UUID;
  competitor_id: UUID;
  competitor_category_id: UUID | null;
  product_id: UUID | null;
  url: string;
  title: string | null;
  created: boolean;
  scrape_task_id: string | null;
  last_seen_at: string | null;
  created_at: string;
  updated_at: string;
};

export type DiscoveryTaskStatus = {
  task_id: string;
  state: string;
  ready: boolean;
  current_phase?: string | null;
  current?: number;
  total?: number;
  product_urls_found?: number;
  new_urls_found?: number;
  created?: number;
  skipped_existing?: number;
  categories_updated?: number;
  sitemap_files_checked?: number;
  pages_scanned?: number;
  external_queries_checked?: number;
  rate_limit_pauses?: number;
  duration_ms?: number | null;
  errors?: string[];
  sample_new_urls?: string[];
  sample_existing_urls?: string[];
  discovery_methods?: DiscoveryMethodResult[];
  probe?: DiscoveryProbeInfo | null;
  result: Record<string, unknown> | null;
};

export type DetectedSubdomain = { host: string; links: number };

export type DiscoveryProbeInfo = {
  platform?: string | null;
  blocked?: boolean;
  best_method?: string | null;
  recommended_methods?: string[];
  method_reasons?: Record<string, string>;
  /** Sibling shop subdomains found on the homepage — opt-in, not crawled by default. */
  detected_subdomains?: DetectedSubdomain[];
  duration_ms?: number;
};

export type ScrapeTaskStatus = {
  task_id: string;
  state: string;
  ready: boolean;
  current: number;
  total: number;
  scraped: number;
  failed: number;
  skipped: number;
  errors: string[];
  duration_ms?: number | null;
  current_phase?: string | null;
  pages_scanned?: number;
  product_urls_found?: number;
  catalog_total?: number;
  pages_total?: number;
  pages_per_minute?: number;
  occ_api_success?: number;
  occ_api_failed?: number;
  avg_occ_ms?: number;
  lightweight_success?: number;
  playwright_fallback?: number;
  avg_scrape_ms?: number;
  products_per_minute?: number;
  http_skipped?: number;
  avg_http_ms?: number;
  avg_playwright_ms?: number;
  failed_by_reason?: Record<string, number>;
  current_concurrency?: number;
  retry_count?: number;
  timeout_pct?: number;
  success_pct?: number;
  dead_urls_skipped?: number;
  js_extract_success?: number;
  adaptive_fast_success?: number;
  adaptive_playwright_success?: number;
  result?: Record<string, unknown> | null;
};

export type MatchTaskStatus = {
  task_id: string;
  state: string;
  ready: boolean;
  current: number;
  total: number;
  matched: number;
  needs_review?: number;
  low_confidence?: number;
  no_match: number;
  no_candidate?: number;
  skipped: number;
  failed?: number;
  errors: string[];
  duration_ms?: number | null;
  current_phase?: string | null;
  products_per_minute?: number;
  skipped_by_reason?: Record<string, number>;
  result?: Record<string, unknown> | null;
};

export type FullDiscoveryResult = {
  competitor_id?: string;
  pages_scanned?: number;
  sitemap_urls_checked?: number;
  sitemap_files_checked?: number;
  product_urls_found?: number;
  new_urls_found?: number;
  created?: number;
  skipped_existing?: number;
  categories_updated?: number;
  errors?: string[];
  sample_product_urls?: string[];
  sample_new_urls?: string[];
  sample_existing_urls?: string[];
  source?: string;
  duration_ms?: number;
  limit_reached?: boolean;
  max_products?: number;
  external_queries_checked?: number;
  rate_limit_pauses?: number;
  deep_discovery?: boolean;
  seed_terms_used?: number;
  public_discovery_blocked?: boolean;
  discovery_block_reason?: string | null;
  discovery_methods?: DiscoveryMethodResult[];
  selected_discovery_methods?: string[];
  probe?: DiscoveryProbeInfo | null;
  error?: string;
};

export type DiscoveryMethodResult = {
  method: string;
  label: string;
  status: string;
  found: number;
  added: number;
  skipped_duplicate: number;
  blocked?: boolean;
  block_reason?: string | null;
  sample_urls?: string[];
  errors?: string[];
};

/** Workspace rows from GET /competitor-categories/{id}/products */
export type CategoryWorkspaceProduct = {
  competitor_product_id: UUID;
  competitor_category_id: UUID | null;
  competitor_name: string;
  category_path: string[];
  image_url: string | null;
  title: string | null;
  url: string;
  listing_ean: string | null;
  listing_manufacturer_code: string | null;
  listing_model: string | null;
  listing_brand: string | null;
  listing_sku: string | null;
  listing_size: string | null;
  listing_color: string | null;
  listing_description: string | null;
  listing_attributes: Record<string, unknown>;
  latest_price: string | null;
  regular_price: string | null;
  matched_own_price: string | null;
  promo_price: string | null;
  old_price: string | null;
  currency: string;
  availability: string | null;
  offered_by: string | null;
  delivered_by: string | null;
  last_seen_at: string | null;
  last_checked_at: string | null;
  matched_sku: string | null;
  matched_product_name: string | null;
  matched_product_id?: UUID | null;
  match_score: string | null;
  match_method: string | null;
  match_status: string | null;
  matched_by?: string | null;
  match_reason?: string | null;
  match_warnings?: string[];
  candidate_count?: number;
  top_candidates?: MatchCandidate[];
};

/** Paginated workspace response */
export type CategoryWorkspacePage = {
  rows: CategoryWorkspaceProduct[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
};

export type ComparisonCompetitor = {
  id: UUID;
  name: string;
  domain: string;
};

export type PriceComparisonPageData = {
  rows: PriceComparisonRow[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  competitors: ComparisonCompetitor[];
};

export type PriceComparisonSummary = {
  matched_products: number;
  needs_review: number;
  found_urls: number;
  tracked_sites: number;
  scraped_urls: number;
};
