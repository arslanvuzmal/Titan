/**
 * In-page DOM collection.
 *
 * Everything in this file runs INSIDE the untrusted page via page.evaluate, so
 * it must be self-contained and defensive. It observes and never acts: no form
 * is submitted, no purchase made, no appointment booked, no CAPTCHA solved, no
 * authentication attempted (mission section 7.3).
 */

import { createRequire } from 'node:module';
import type { Page } from 'playwright';
import type {
  AccessibilityViolation,
  PageEvidence,
  PerformanceMetrics,
  SecurityHeaders,
} from './contract.js';

const MAX_TEXT = 20000;
const MAX_AXE_VIOLATIONS = 50;

//: The closed set the contract allows. Anything else axe reports is recorded as
//: null rather than passed through, so a new impact level in a future axe
//: release cannot widen the contract without somebody deciding to.
const AXE_IMPACTS = new Set(['minor', 'moderate', 'serious', 'critical']);

// Resolved once, from the installed package rather than a CDN: the worker runs
// with no outbound network beyond the site being crawled, and pulling a script
// off the internet into an untrusted page would be a supply chain step nobody
// reviewed.
const AXE_SOURCE_PATH = createRequire(import.meta.url).resolve('axe-core/axe.min.js');

/**
 * Pure string script evaluated inside the untrusted page DOM context.
 * Kept as a string so tsx/esbuild name-mangling never injects __name helpers.
 */
const DOM_COLLECTOR_SCRIPT = `() => {
  const cap = (arr, n) => arr.slice(0, n);
  const txt = (el) =>
    el ? (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 300) || null : null;

  const cssPath = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    let node = el;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < 5) {
      let part = node.tagName.toLowerCase();
      const testId = node.getAttribute('data-testid');
      if (testId) {
        part += '[data-testid="' + testId + '"]';
        parts.unshift(part);
        break;
      }
      const parent = node.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
        if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      node = parent;
      depth += 1;
    }
    return parts.join(' > ');
  };

  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    const s = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };

  const origin = window.location.origin;
  const anchors = Array.from(document.querySelectorAll('a[href]'));
  const hrefs = anchors
    .map((a) => a.href)
    .filter((h) => h.startsWith('http://') || h.startsWith('https://'));

  const bookingPattern =
    /(calendly|cal\\.com|acuity|squareup\\/appointments|setmore|booksy|opentable|resy|simplybook|youcanbook|appointlet|book|schedule|reserve|appointment)/i;
  const contactPattern = /(contact|get-in-touch|enquir|inquir|quote|consultation)/i;
  const socialPattern =
    /(facebook\\.com|instagram\\.com|linkedin\\.com|x\\.com|twitter\\.com|youtube\\.com|tiktok\\.com|yelp\\.com)/i;
  const reviewPattern = /(yelp\\.com|trustpilot|google\\.[a-z.]+\\/maps|reviews)/i;

  const bodyText = (document.body?.innerText || '').replace(/\\s+/g, ' ').trim();

  // A mailto: href is a URI and must be decoded before it is an address:
  // "mailto:%20info@x.com" names the mailbox "info@x.com" reached past a
  // leading space, not one called "%20info". Undecoded, that string passed
  // every downstream gate and hard-bounced against a real domain.
  const decodeMailto = (href) => {
    const raw = href.replace(/^mailto:/i, '').split('?')[0];
    let decoded = raw;
    try {
      decoded = decodeURIComponent(raw);
    } catch (err) {
      decoded = raw;
    }
    return decoded.trim().toLowerCase();
  };
  const mailtos = anchors
    .filter((a) => a.protocol === 'mailto:')
    .map((a) => decodeMailto(a.href))
    // Whitespace surviving the decode means it was inside the address rather
    // than around it, and trimming would silently invent a different mailbox.
    .filter((e) => e && !/\\s/.test(e));

  // Read per text node, not from the flattened body. innerText puts no
  // separator between adjacent inline elements, so "<span>0606</span><a>
  // info@x.com</a>" flattens to "0606info@x.com" -- a syntactically perfect
  // address that nothing downstream can distinguish from a real "07handyman@",
  // and the second of the two shapes that reached a real send.
  //
  // The cost is an address deliberately split across elements, which is missed
  // rather than mangled. Losing one is the better failure: the mailto: pass
  // above catches most of them, and a fabricated address is billed to sender
  // reputation while a missing one is not.
  const emailPattern = /[a-z0-9._+-]+@[a-z0-9-]+(?:\\.[a-z0-9-]+)+/gi;
  const skipText = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
  const textEmails = [];
  if (document.body) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.parentElement && skipText.has(node.parentElement.tagName)) continue;
      const found = (node.nodeValue || '').match(emailPattern) || [];
      for (const hit of found) textEmails.push(hit.toLowerCase());
    }
  }
  const tels = anchors.filter((a) => a.protocol === 'tel:').map((a) => a.href.replace(/^tel:/i, ''));
  const textPhones = bodyText.match(/(\\+?\\d[\\d\\s().-]{7,}\\d)/g) || [];

  const images = Array.from(document.images);
  const structured = Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
    .flatMap((s) => {
      try {
        const parsed = JSON.parse(s.textContent || '{}');
        const nodes = Array.isArray(parsed) ? parsed : [parsed];
        return nodes.map((n) => String(n['@type'] || '')).filter(Boolean);
      } catch {
        return [];
      }
    });

  const tech = [];
  const w = window;
  if (w.wp || document.querySelector('link[href*="wp-content"]')) tech.push('wordpress');
  if (w.Shopify) tech.push('shopify');
  if (document.querySelector('[data-wf-page]')) tech.push('webflow');
  if (document.querySelector('#__next')) tech.push('nextjs');
  if (w.Wix || document.querySelector('[data-wix-id]')) tech.push('wix');
  if (w.Squarespace) tech.push('squarespace');
  if (w.gtag || w.dataLayer) tech.push('google-analytics');
  if (w.fbq) tech.push('meta-pixel');
  if (w.hbspt) tech.push('hubspot');

  const chatSelectors =
    '#intercom-frame,.intercom-launcher,#drift-widget,#tidio-chat,#crisp-chatbox,' +
    '[id*="livechat"],[class*="chat-widget"],#hubspot-messages-iframe-container,.tawk-min-container';
  const cookieSelectors =
    '#onetrust-banner-sdk,#cookie-banner,.cookie-consent,#cookieConsent,[id*="cookie-notice"],' +
    '.cc-window,#CybotCookiebotDialog';

  const ctaKeywords =
    /(book|schedule|contact|call|get started|free|consultation|quote|enquire|inquire|sign up|join|apply|request|order|buy|shop|reserve)/i;
  const ctas = cap(
    anchors
      .concat(Array.from(document.querySelectorAll('button')))
      .filter((el) => ctaKeywords.test(el.textContent || ''))
      .filter((el) => isVisible(el)),
    30,
  ).map((el) => ({
    selector: cssPath(el),
    text: txt(el),
    href: el.href || null,
    is_visible: true,
    target_status: null,
    target_is_empty: null,
  }));

  const forms = cap(Array.from(document.querySelectorAll('form')), 20).map((f) => {
    const fields = Array.from(f.querySelectorAll('input,select,textarea')).filter(
      (i) => i.type !== 'hidden',
    );
    return {
      selector: cssPath(f),
      action: f.getAttribute('action'),
      method: (f.getAttribute('method') || 'get').toLowerCase(),
      field_count: fields.length,
      field_names: cap(
        fields.map((i) => i.getAttribute('name') || i.getAttribute('id') || '').filter(Boolean),
        40,
      ),
      has_submit: !!f.querySelector('[type="submit"],button:not([type="button"])'),
    };
  });

  return {
    title: document.title || null,
    meta_description:
      document.querySelector('meta[name="description"]')?.getAttribute('content') || null,
    canonical_url: document.querySelector('link[rel="canonical"]')?.getAttribute('href') || null,
    robots_meta: document.querySelector('meta[name="robots"]')?.getAttribute('content') || null,
    lang: document.documentElement.getAttribute('lang'),
    has_viewport_meta: !!document.querySelector('meta[name="viewport"]'),
    headings: cap(
      Array.from(document.querySelectorAll('h1,h2,h3')).map((h) => txt(h) || ''),
      40,
    ).filter(Boolean),
    nav_links: cap(
      anchors
        .filter((a) => a.href.startsWith('http'))
        .map((a) => ({
          text: txt(a),
          href: a.href,
          is_external: !a.href.startsWith(origin),
          status: null,
          is_broken: null,
        })),
      200,
    ),
    forms,
    ctas,
    visible_emails: cap(Array.from(new Set([...mailtos, ...textEmails])), 20),
    visible_phones: cap(
      Array.from(new Set([...tels, ...textPhones])).map((p) => p.trim()),
      20,
    ),
    booking_links: cap(Array.from(new Set(hrefs.filter((h) => bookingPattern.test(h)))), 20),
    contact_links: cap(Array.from(new Set(hrefs.filter((h) => contactPattern.test(h)))), 20),
    social_links: cap(Array.from(new Set(hrefs.filter((h) => socialPattern.test(h)))), 30),
    review_links: cap(Array.from(new Set(hrefs.filter((h) => reviewPattern.test(h)))), 20),
    structured_data_types: cap(Array.from(new Set(structured)), 30),
    technologies: cap(Array.from(new Set(tech)), 30),
    images_missing_alt: images.filter((i) => !i.getAttribute('alt')).length,
    image_count: images.length,
    has_chat_widget: !!document.querySelector(chatSelectors),
    has_cookie_obstruction: !!document.querySelector(cookieSelectors),
    text_excerpt: bodyText.slice(0, ${MAX_TEXT}),
    word_count: bodyText ? bodyText.split(/\\s+/).length : 0,
  };
}`;

export function readSecurityHeaders(headers: Record<string, string>): SecurityHeaders {
  const get = (k: string) => headers[k] ?? headers[k.toLowerCase()] ?? null;
  return {
    strict_transport_security: get('strict-transport-security'),
    content_security_policy: get('content-security-policy'),
    x_content_type_options: get('x-content-type-options'),
    x_frame_options: get('x-frame-options'),
    referrer_policy: get('referrer-policy'),
    has_mixed_content: false,
  };
}

/**
 * Largest Contentful Paint, as the browser measured it.
 *
 * Read from the page's own PerformanceObserver rather than derived from
 * navigation timings, because LCP is defined as the render time of the largest
 * element and nothing in the navigation entry knows which element that was.
 * `buffered: true` replays the entries that already fired before this ran.
 *
 * Returns null on any failure. A missing measurement and a fast page must not
 * look alike: the detector treats null as "not measured" and stays silent.
 */
export async function readPerformance(page: Page): Promise<PerformanceMetrics | null> {
  try {
    const measured = (await page.evaluate(`new Promise((resolve) => {
      let lcp = null;
      let cls = 0;
      let seen = false;
      try {
        const lcpObserver = new PerformanceObserver((list) => {
          const entries = list.getEntries();
          if (entries.length) {
            lcp = entries[entries.length - 1].startTime;
            seen = true;
          }
        });
        lcpObserver.observe({ type: 'largest-contentful-paint', buffered: true });

        const clsObserver = new PerformanceObserver((list) => {
          for (const entry of list.getEntries()) {
            if (!entry.hadRecentInput) cls += entry.value;
            seen = true;
          }
        });
        clsObserver.observe({ type: 'layout-shift', buffered: true });

        setTimeout(() => {
          lcpObserver.disconnect();
          clsObserver.disconnect();
          resolve(seen ? { lcp: lcp, cls: cls } : null);
        }, 400);
      } catch (err) {
        resolve(null);
      }
    })`)) as { lcp: number | null; cls: number } | null;

    if (measured === null) return null;
    return {
      // Null, and deliberately so: these four are Lighthouse category scores
      // and Titan does not run Lighthouse. Filling them with something derived
      // would put a number nobody measured in front of a recipient.
      performance_score: null,
      accessibility_score: null,
      seo_score: null,
      best_practices_score: null,
      largest_contentful_paint_ms: measured.lcp,
      total_blocking_time_ms: null,
      cumulative_layout_shift: measured.cls,
      speed_index_ms: null,
    };
  } catch {
    return null;
  }
}

/**
 * axe-core, injected into the page and run against it.
 *
 * The page is untrusted, so everything that comes back is treated as data: the
 * fields are coerced, the impact is checked against the closed set the contract
 * allows, and both the violation list and the selector length are capped. A
 * page that returns something enormous or malformed yields an empty list rather
 * than propagating.
 *
 * Failure is silent by design. Accessibility findings are a bonus on a crawl
 * whose main job is the conversion path, and a site that breaks axe should not
 * cost us the rest of the evidence.
 */
export async function runAxe(page: Page): Promise<AccessibilityViolation[]> {
  try {
    await page.addScriptTag({ path: AXE_SOURCE_PATH });
    const raw = (await page.evaluate(`
      (typeof axe === 'undefined'
        ? Promise.resolve(null)
        : axe
            .run(document, {
              resultTypes: ['violations'],
              performanceTimer: false,
            })
            .then((r) =>
              (r.violations || []).slice(0, 50).map((v) => ({
                rule_id: String(v.id || ''),
                impact: v.impact == null ? null : String(v.impact),
                description: v.help == null ? null : String(v.help),
                node_count: Array.isArray(v.nodes) ? v.nodes.length : 0,
                sample_selector:
                  v.nodes && v.nodes[0] && Array.isArray(v.nodes[0].target)
                    ? String(v.nodes[0].target[0])
                    : null,
              })),
            )
            .catch(() => null))
    `)) as unknown;

    if (!Array.isArray(raw)) return [];
    return raw.slice(0, MAX_AXE_VIOLATIONS).map((v) => {
      const row = v as Record<string, unknown>;
      const impact = String(row.impact ?? '');
      return {
        rule_id: String(row.rule_id ?? '').slice(0, 120),
        impact: AXE_IMPACTS.has(impact) ? (impact as AccessibilityViolation['impact']) : null,
        description: row.description == null ? null : String(row.description).slice(0, 300),
        node_count: Number.isFinite(Number(row.node_count)) ? Number(row.node_count) : 0,
        sample_selector:
          row.sample_selector == null ? null : String(row.sample_selector).slice(0, 300),
      };
    });
  } catch {
    return [];
  }
}

export async function collectPage(
  page: Page,
  base: Pick<PageEvidence, 'url' | 'final_url' | 'depth' | 'http_status' | 'content_type'>,
  extras: {
    consoleErrors: string[];
    failedRequests: string[];
    securityHeaders: SecurityHeaders | null;
    accessibilityViolations?: AccessibilityViolation[];
    performance?: PerformanceMetrics | null;
  },
): Promise<PageEvidence> {
  // page.evaluate(<string>) is typed `unknown`, so the result is annotated
  // with the fields the collector returns. Everything not listed here is
  // supplied by `base` or `extras` below.
  type CollectedDom = Omit<
    PageEvidence,
    | 'url'
    | 'final_url'
    | 'depth'
    | 'http_status'
    | 'content_type'
    | 'console_errors'
    | 'failed_requests'
    | 'security_headers'
    | 'accessibility_violations'
    | 'performance'
    | 'captured_at'
  >;
  const dom = (await page.evaluate(
    `(${DOM_COLLECTOR_SCRIPT})()`,
  )) as CollectedDom;
  return {
    ...base,
    ...dom,
    console_errors: extras.consoleErrors.slice(0, 50),
    failed_requests: extras.failedRequests.slice(0, 50),
    security_headers: extras.securityHeaders,
    accessibility_violations: extras.accessibilityViolations ?? [],
    performance: extras.performance ?? null,
    captured_at: new Date().toISOString(),
  };
}
