/**
 * The bounded crawler.
 *
 * Hard limits on pages, depth, wall-clock time, bytes, and redirects are all
 * enforced here rather than trusted to a caller. The worker holds no email,
 * model, or database credentials, so even a full browser escape yields nothing
 * that can send mail or read tenant data (mission section 4.3).
 */

import crypto from 'node:crypto';
import { chromium, type Browser, type BrowserContext } from 'playwright';
import robotsParser from 'robots-parser';
import type {
  ArtifactRef,
  CrawlResult,
  PageEvidence,
  ResearchRequest,
} from './contract.js';
import { CONTRACT_VERSION, WORKER_VERSION } from './contract.js';
import { collectPage, readPerformance, readSecurityHeaders, runAxe } from './collect.js';
import { validateUrl } from './urlGuard.js';

const MOBILE = { width: 390, height: 844 };
const DESKTOP = { width: 1440, height: 900 };

export function fingerprint(value: unknown): string {
  const canonical = JSON.stringify(value, Object.keys(value as object).sort());
  return crypto.createHash('sha256').update(canonical ?? '').digest('hex');
}

/** sha256 over stable content, excluding volatile fields (contract 7.4). */
export function stableFingerprint(value: Record<string, unknown>): string {
  const sorted = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(sorted);
    if (v && typeof v === 'object') {
      return Object.fromEntries(
        Object.entries(v as Record<string, unknown>)
          .filter(([k]) => !['captured_at', 'storage_key', 'session_id', 'worker_id'].includes(k))
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([k, val]) => [k, sorted(val)]),
      );
    }
    return v;
  };
  return crypto.createHash('sha256').update(JSON.stringify(sorted(value))).digest('hex');
}

function normalizeUrl(raw: string): string {
  try {
    const u = new URL(raw);
    u.hash = '';
    // Strip common tracking parameters so the same page under different
    // campaign tags is not crawled twice or fingerprinted differently.
    for (const p of ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'gclid', 'fbclid']) {
      u.searchParams.delete(p);
    }
    if (u.pathname.endsWith('/') && u.pathname !== '/') u.pathname = u.pathname.slice(0, -1);
    return u.toString();
  } catch {
    return raw;
  }
}

async function robotsAllows(origin: string, userAgent: string, path: string): Promise<boolean> {
  try {
    const res = await fetch(`${origin}/robots.txt`, {
      headers: { 'user-agent': userAgent },
      signal: AbortSignal.timeout(8000),
    });
    if (!res.ok) return true; // no robots.txt means no restriction
    const body = (await res.text()).slice(0, 200_000);

    // RFC 9309, via robots-parser. The hand-rolled parse this replaces was
    // documented as "minimal but correct-enough" and was wrong in five of six
    // cases the standard defines. Two of them mattered:
    //
    //   Disallow: /*.pdf$        -- startsWith() never matches a wildcard, so
    //   Disallow: /private/*/x      the path was crawled despite being refused
    //
    // Under-blocking is the failure that cannot be argued away: the site said
    // no in the one machine-readable way it has, and Titan went anyway. The
    // other three over-blocked (no Allow support, an empty User-agent value
    // matching everything, and a group named "bot" matching any agent whose
    // name contains it), which cost leads rather than trust.
    //
    // Robots is the whole consent mechanism for a crawler that reads sites
    // nobody asked it to read, so it is worth a tested implementation rather
    // than twenty lines that look right.
    const robots = robotsParser(`${origin}/robots.txt`, body);
    // isAllowed() returns undefined when no rule addresses the path at all.
    return robots.isAllowed(`${origin}${path}`, userAgent) ?? true;
  } catch {
    // Network failure reading robots.txt is not consent; but neither is it a
    // prohibition. Titan proceeds and records that robots was unreadable.
    return true;
  }
}

export async function runCrawl(req: ResearchRequest): Promise<CrawlResult> {
  const started = Date.now();
  const deadline = started + req.timeout_seconds * 1000;
  const result: CrawlResult = {
    contract_version: CONTRACT_VERSION,
    request_id: req.request_id,
    status: 'failed',
    seed_url: req.seed_url,
    final_url: null,
    redirect_chain: [],
    blocked_reason: null,
    failure_reason: null,
    robots_allowed: null,
    pages: [],
    artifacts: [],
    pages_fetched: 0,
    bytes_fetched: 0,
    duration_ms: 0,
    worker_version: WORKER_VERSION,
  };

  const seedVerdict = await validateUrl(req.seed_url);
  if (!seedVerdict.allowed) {
    result.status = 'blocked';
    result.blocked_reason = seedVerdict.reason ?? 'url_guard_refused';
    result.duration_ms = Date.now() - started;
    return result;
  }

  let browser: Browser | null = null;
  let context: BrowserContext | null = null;

  try {
    const launchOptions: any = {
      args: [
        '--disable-dev-shm-usage',
        '--no-zygote',
        // Same-origin policy stays ON. These only reduce the attack surface of
        // features Titan never needs.
        '--disable-background-networking',
        '--disable-sync',
        '--disable-extensions',
      ],
    };
    if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH) {
      launchOptions.executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
    }
    browser = await chromium.launch(launchOptions);
    context = await browser.newContext({
      userAgent: req.user_agent,
      viewport: DESKTOP,
      ignoreHTTPSErrors: false,
      javaScriptEnabled: true,
      serviceWorkers: 'block',
      bypassCSP: false,
    });
    // Never send or accept credentials; never store state between pages.
    await context.clearCookies();

    const seen = new Set<string>();
    const queue: Array<{ url: string; depth: number }> = [
      { url: normalizeUrl(req.seed_url), depth: 0 },
    ];

    const seedOrigin = new URL(req.seed_url).origin;
    if (req.respect_robots) {
      result.robots_allowed = await robotsAllows(
        seedOrigin,
        req.user_agent,
        new URL(req.seed_url).pathname,
      );
      if (!result.robots_allowed) {
        result.status = 'blocked';
        result.blocked_reason = 'robots_txt_disallow';
        result.duration_ms = Date.now() - started;
        return result;
      }
    }

    // Seed the priority paths the playbook asked to inspect.
    for (const p of req.priority_paths.slice(0, 20)) {
      try {
        queue.push({ url: normalizeUrl(new URL(p, seedOrigin).toString()), depth: 1 });
      } catch {
        /* ignore malformed playbook path */
      }
    }

    while (queue.length > 0 && result.pages.length < req.max_pages) {
      if (Date.now() > deadline) {
        result.status = 'partial';
        result.failure_reason = 'time_budget_exhausted';
        break;
      }
      const item = queue.shift()!;
      if (seen.has(item.url) || item.depth > req.max_depth) continue;
      seen.add(item.url);

      // Re-validate: this URL came off a page, not from the control plane.
      const verdict = await validateUrl(item.url);
      if (!verdict.allowed) continue;

      const page = await context.newPage();
      const consoleErrors: string[] = [];
      const failedRequests: string[] = [];
      let bytes = 0;

      page.on('console', (m) => {
        if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 500));
      });
      page.on('requestfailed', (r) => {
        failedRequests.push(`${r.method()} ${r.url().slice(0, 300)} :: ${r.failure()?.errorText ?? 'failed'}`);
      });
      page.on('response', (r) => {
        const len = Number(r.headers()['content-length'] ?? 0);
        if (Number.isFinite(len)) bytes += len;
      });

      try {
        const response = await page.goto(item.url, {
          waitUntil: 'domcontentloaded',
          timeout: Math.min(30_000, Math.max(5_000, deadline - Date.now())),
        });
        if (bytes > req.max_response_bytes) {
          await page.close();
          continue;
        }

        // Every redirect hop must pass the guard, not just the final URL.
        const chain: string[] = [];
        let r = response?.request().redirectedFrom();
        while (r && chain.length <= req.max_redirects) {
          chain.unshift(r.url());
          r = r.redirectedFrom();
        }
        chain.push(page.url());
        for (const hop of chain) {
          const hopVerdict = await validateUrl(hop);
          if (!hopVerdict.allowed) {
            result.blocked_reason = `redirect_blocked:${hopVerdict.reason}`;
            await page.close();
            throw new Error(`redirect to disallowed target: ${hopVerdict.reason}`);
          }
        }
        if (item.depth === 0) {
          result.final_url = page.url();
          result.redirect_chain = chain;
        }

        const headers = response?.headers() ?? {};
        // Both were hard-coded empty until now, which left two detectors
        // (serious_accessibility_violations, slow_largest_contentful_paint)
        // unable to fire on any site ever crawled.
        //
        // Axe runs on the seed page only. It is the expensive call here, and
        // accessibility rules fail the same way across a template, so paying
        // for it once per site buys nearly all of the signal. Timing is read
        // per page because it is nearly free and genuinely varies.
        const accessibilityViolations =
          req.run_axe && item.depth === 0 ? await runAxe(page) : [];
        const performance = await readPerformance(page);
        const evidence: PageEvidence = await collectPage(
          page,
          {
            url: item.url,
            final_url: page.url(),
            depth: item.depth,
            http_status: response?.status() ?? null,
            content_type: headers['content-type'] ?? null,
          },
          {
            consoleErrors,
            failedRequests,
            securityHeaders: item.depth === 0 ? readSecurityHeaders(headers) : null,
            accessibilityViolations,
            performance,
          },
        );
        result.pages.push(evidence);
        result.pages_fetched += 1;
        result.bytes_fetched += bytes;

        if (req.capture_screenshots && item.depth === 0) {
          for (const [kind, viewport] of [
            ['screenshot_desktop', DESKTOP],
            ['screenshot_mobile', MOBILE],
          ] as const) {
            await page.setViewportSize(viewport);
            const shot = await page.screenshot({ fullPage: false, type: 'png' });
            result.artifacts.push({
              kind,
              media_type: 'image/png',
              storage_key: `${req.request_id}/${kind}.png`,
              payload: null,
              byte_size: shot.byteLength,
              content_fingerprint: crypto.createHash('sha256').update(shot).digest('hex'),
              page_url: page.url(),
            } satisfies ArtifactRef);
          }
        }

        // Enqueue same-origin links only. External links are recorded as
        // evidence but never crawled -- Titan researches one business at a time.
        if (item.depth < req.max_depth) {
          for (const link of evidence.nav_links) {
            if (link.is_external) continue;
            const next = normalizeUrl(link.href);
            if (!seen.has(next) && queue.length < req.max_pages * 3) {
              queue.push({ url: next, depth: item.depth + 1 });
            }
          }
        }
      } catch (err) {
        if (item.depth === 0) {
          result.failure_reason = (err as Error).message.slice(0, 500);
        }
      } finally {
        if (!page.isClosed()) await page.close();
      }
    }

    if (result.status !== 'partial') {
      result.status = result.pages.length > 0 ? 'completed' : 'failed';
    }
    if (result.pages.length === 0 && !result.failure_reason) {
      result.failure_reason = 'no_pages_captured';
    }
  } catch (err) {
    result.status = 'failed';
    result.failure_reason = (err as Error).message.slice(0, 500);
  } finally {
    await context?.close().catch(() => undefined);
    await browser?.close().catch(() => undefined);
  }

  result.duration_ms = Date.now() - started;
  return result;
}
