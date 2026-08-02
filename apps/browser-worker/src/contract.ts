/**
 * Wire contract with the Titan control plane.
 *
 * The Python side is `apps/api/titan/contracts/evidence.py`. Both declare
 * CONTRACT_VERSION and a drift test compares the field sets, so a change on one
 * side that is not mirrored on the other fails CI rather than silently dropping
 * evidence.
 */

export const CONTRACT_VERSION = '1.0.0';
export const WORKER_VERSION = '0.2.0';

export interface ResearchRequest {
  request_id: string;
  seed_url: string;
  max_pages: number;
  max_depth: number;
  timeout_seconds: number;
  max_response_bytes: number;
  max_redirects: number;
  user_agent: string;
  respect_robots: boolean;
  capture_screenshots: boolean;
  run_lighthouse: boolean;
  run_axe: boolean;
  priority_paths: string[];
  /**
   * Addresses the control plane already vetted with its SSRF guard. The worker
   * pins its connections to these rather than re-resolving the hostname, which
   * is what closes the DNS-rebinding window.
   */
  pinned_ips: string[];
}

export interface LinkObservation {
  text: string | null;
  href: string;
  is_external: boolean;
  status: number | null;
  is_broken: boolean | null;
}

export interface FormObservation {
  selector: string;
  action: string | null;
  method: string | null;
  field_count: number;
  /** Field NAMES only. Values are never captured and forms are never submitted. */
  field_names: string[];
  has_submit: boolean;
}

export interface CtaObservation {
  selector: string;
  text: string | null;
  href: string | null;
  is_visible: boolean;
  target_status: number | null;
  target_is_empty: boolean | null;
}

export interface AccessibilityViolation {
  rule_id: string;
  impact: 'minor' | 'moderate' | 'serious' | 'critical' | null;
  description: string | null;
  node_count: number;
  sample_selector: string | null;
}

export interface PerformanceMetrics {
  performance_score: number | null;
  accessibility_score: number | null;
  seo_score: number | null;
  best_practices_score: number | null;
  largest_contentful_paint_ms: number | null;
  total_blocking_time_ms: number | null;
  cumulative_layout_shift: number | null;
  speed_index_ms: number | null;
}

export interface SecurityHeaders {
  strict_transport_security: string | null;
  content_security_policy: string | null;
  x_content_type_options: string | null;
  x_frame_options: string | null;
  referrer_policy: string | null;
  has_mixed_content: boolean;
}

export interface PageEvidence {
  url: string;
  final_url: string;
  depth: number;
  http_status: number | null;
  content_type: string | null;
  title: string | null;
  meta_description: string | null;
  canonical_url: string | null;
  robots_meta: string | null;
  lang: string | null;
  has_viewport_meta: boolean;
  headings: string[];
  nav_links: LinkObservation[];
  forms: FormObservation[];
  ctas: CtaObservation[];
  visible_emails: string[];
  visible_phones: string[];
  booking_links: string[];
  contact_links: string[];
  social_links: string[];
  review_links: string[];
  structured_data_types: string[];
  technologies: string[];
  console_errors: string[];
  failed_requests: string[];
  images_missing_alt: number;
  image_count: number;
  has_chat_widget: boolean;
  has_cookie_obstruction: boolean;
  security_headers: SecurityHeaders | null;
  accessibility_violations: AccessibilityViolation[];
  performance: PerformanceMetrics | null;
  text_excerpt: string | null;
  word_count: number;
  captured_at: string;
}

export type ArtifactKind =
  | 'screenshot_mobile'
  | 'screenshot_desktop'
  | 'lighthouse'
  | 'axe'
  | 'console'
  | 'network_failures'
  | 'headers';

export interface ArtifactRef {
  kind: ArtifactKind;
  media_type: string;
  storage_key: string | null;
  payload: Record<string, unknown> | null;
  byte_size: number | null;
  content_fingerprint: string;
  page_url: string | null;
}

export interface CrawlResult {
  contract_version: string;
  request_id: string;
  status: 'completed' | 'blocked' | 'failed' | 'partial';
  seed_url: string;
  final_url: string | null;
  redirect_chain: string[];
  blocked_reason: string | null;
  failure_reason: string | null;
  robots_allowed: boolean | null;
  pages: PageEvidence[];
  artifacts: ArtifactRef[];
  pages_fetched: number;
  bytes_fetched: number;
  duration_ms: number;
  worker_version: string;
}
