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
    return u.toString();
  } catch {
    return raw;
  }
}

/**
 * The key that decides whether we have already been somewhere.
 *
 * This is where the trailing slash belongs, and for two years it was in
 * `normalizeUrl` instead -- so the crawler *fetched* `/contact` when the site
 * linked `/contact/`, and recorded the answer against the link. Servers do not
 * agree that those are the same address:
 *
 *     https://dermalclinic.co.uk/contact     404
 *     https://dermalclinic.co.uk/contact/    200
 *     https://skinessence.com.au/book-a-treatment    404
 *     https://skinessence.com.au/book-a-treatment/   200
 *
 * Both checked live on 2026-09-10, against crawl rows saying 404. The evidence
 * was a fact about a URL we invented by editing theirs, and it reached a
 * finding that told the business a working page was broken.
 *
 * Folding the two forms is still right for "have I crawled this already" --
 * that is a question about our budget, not about their server -- so the two
 * jobs are now separate functions.
 */
function dedupeKey(url: string): string {
  try {
    const u = new URL(url);
    if (u.pathname.endsWith('/') && u.pathname !== '/') u.pathname = u.pathname.slice(0, -1);
    return u.toString();
  } catch {
    return url;
  }
}

/**
 * How many distinct call-to-action targets one crawl will navigate to.
 *
 * Small on purpose. These are extra requests to somebody else's server on top
 * of an 18-page crawl, and the finding they support is site-wide -- one dead
 * booking button is the story whether it is dead on one page or on twelve. A
 * page may carry 30 calls to action and most sites repeat the same handful
 * across every template, so deduplication does most of the work and this is
 * the ceiling for the rest.
 */
const MAX_CTA_PROBES = 10;

/** A rendered page shorter than this is treated as having nothing on it. */
const EMPTY_TEXT_CHARS = 40;

/** Pause before the second look, so a rate limiter has a moment to forget us. */
const RECHECK_DELAY_MS = 1_500;

interface ProbeOutcome {
  status: number | null;
  isEmpty: boolean | null;
}

/**
 * Navigate to one call-to-action target and say what is there.
 *
 * **An error is checked twice.** Statuses flap, and not rarely:
 *
 *     whitesmileancoats.com/contact   404   14:57:08
 *     whitesmileancoats.com/contact   200   14:57:18
 *
 * Ten seconds apart, alternating across dozens of fetches over three weeks --
 * rate limiting, a bot defence, or a flaky origin, and indistinguishable from
 * a genuinely missing page on one sample. The finding this feeds is CRITICAL
 * and says "the button on your website leads nowhere", so one sample is not
 * enough to earn it. A success on either look wins: we are trying to prove the
 * page is gone, and having loaded it once disproves that.
 */
async function probeTarget(
  context: BrowserContext,
  url: string,
  deadline: number,
): Promise<ProbeOutcome | null> {
  const verdict = await validateUrl(url);
  if (!verdict.allowed) return null;

  let last: ProbeOutcome | null = null;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    if (Date.now() > deadline) break;
    const page = await context.newPage();
    try {
      const response = await page.goto(url, {
        waitUntil: 'domcontentloaded',
        timeout: Math.min(15_000, Math.max(4_000, deadline - Date.now())),
      });
      const status = response?.status() ?? null;
      let isEmpty: boolean | null = null;
      try {
        const text = (await page.evaluate(
          'document.body ? document.body.innerText.trim().length : 0',
        )) as number;
        isEmpty = text < EMPTY_TEXT_CHARS;
      } catch {
        /* a page that will not answer about itself is not evidence of empty */
      }
      last = { status, isEmpty };
      // Good news is believed immediately; bad news gets a second look.
      if (status !== null && status < 400) return last;
    } catch {
      last = last ?? { status: null, isEmpty: null };
    } finally {
      if (!page.isClosed()) await page.close().catch(() => undefined);
    }
    if (attempt === 0 && Date.now() + RECHECK_DELAY_MS < deadline) {
      await new Promise((r) => setTimeout(r, RECHECK_DELAY_MS));
    }
  }
  return last;
}

/**
 * Fill in `target_status` for the calls to action this crawl collected.
 *
 * Until now the worker wrote `target_status: null` as a literal, so across
 * 217,024 recorded calls to action not one was ever measured -- and the rule
 * that reads it, a CRITICAL "your Book Now button leads nowhere", could never
 * fire. The rule was right to insist on a measurement rather than infer one:
 * resolving it from pages the crawl happened to visit was tried on the live
 * data and produced a false claim half the time.
 *
 * Runs after the crawl so it spends only what is left of the budget, and only
 * on targets nothing else has already answered for.
 */
async function probeCtaTargets(
  context: BrowserContext,
  result: CrawlResult,
  deadline: number,
): Promise<void> {
  const wanted = new Map<string, string>();
  for (const page of result.pages) {
    if (page.http_status !== null && page.http_status >= 400) continue;
    const base = page.final_url || page.url;
    let origin: string;
    try {
      origin = new URL(base).origin;
    } catch {
      continue;
    }
    for (const cta of page.ctas) {
      if (!cta.is_visible || !cta.href) continue;
      let absolute: URL;
      try {
        absolute = new URL(cta.href, base);
      } catch {
        continue;
      }
      // Same site only: a booking widget on a third-party domain is somebody
      // else's outage, and naming it as this business's defect is the error
      // this whole module exists to avoid.
      if (absolute.origin !== origin) continue;
      if (absolute.toString() === base) continue;
      absolute.hash = '';
      const key = absolute.toString();
      if (!wanted.has(key) && wanted.size < MAX_CTA_PROBES) wanted.set(key, key);
    }
  }

  const measured = new Map<string, ProbeOutcome>();
  for (const url of wanted.keys()) {
    if (Date.now() > deadline) break;
    const outcome = await probeTarget(context, url, deadline);
    if (outcome) measured.set(url, outcome);
  }
  if (measured.size === 0) return;

  for (const page of result.pages) {
    const base = page.final_url || page.url;
    for (const cta of page.ctas) {
      if (!cta.href) continue;
      let key: string;
      try {
        const u = new URL(cta.href, base);
        u.hash = '';
        key = u.toString();
      } catch {
        continue;
      }
      const outcome = measured.get(key);
      if (!outcome) continue;
      cta.target_status = outcome.status;
      cta.target_is_empty = outcome.isEmpty;
    }
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

/**
 * Re-measure one URL, cheaply, so a claim can be checked before it is sent.
 *
 * Every message Titan sends asserts something about a page: "the link in your
 * navigation returns 404", "your Book Now button leads nowhere". That
 * assertion was measured when the site was crawled, and the send gate only
 * asks how *old* the measurement is -- never whether it is still true. A
 * business that fixed its booking page a fortnight ago still gets told it is
 * broken, and `audit_findings.contradicted` exists for exactly this and has
 * never been written by anything.
 *
 * This is the cheap half of the answer: one URL, one browser context, the same
 * double-sampling `probeTarget` already uses, and no crawl. It lives here
 * rather than in the API because the browser worker is deliberately the only
 * component that fetches attacker-controlled URLs and deliberately the only one
 * with no database, mail or model credentials. Moving this fetch into the API
 * container would hand a hostile page a process that holds all three.
 *
 * Returns what was seen, never a verdict. Deciding whether a status contradicts
 * a claim belongs with the claim, which is in Python.
 */
export async function recheckUrl(
  url: string,
  opts: { userAgent: string; timeoutSeconds: number },
): Promise<{
  url: string;
  allowed: boolean;
  blocked_reason: string | null;
  status: number | null;
  is_empty: boolean | null;
  duration_ms: number;
  worker_version: string;
}> {
  const started = Date.now();
  const out = {
    url,
    allowed: true,
    blocked_reason: null as string | null,
    status: null as number | null,
    is_empty: null as boolean | null,
    duration_ms: 0,
    worker_version: WORKER_VERSION,
  };

  const verdict = await validateUrl(url);
  if (!verdict.allowed) {
    out.allowed = false;
    out.blocked_reason = verdict.reason ?? 'url_guard_refused';
    out.duration_ms = Date.now() - started;
    return out;
  }

  const deadline = started + Math.max(5, opts.timeoutSeconds) * 1000;
  let browser: Browser | null = null;
  let context: BrowserContext | null = null;
  try {
    const launchOptions: any = {
      args: [
        '--disable-dev-shm-usage',
        '--no-zygote',
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
      userAgent: opts.userAgent,
      viewport: DESKTOP,
      ignoreHTTPSErrors: false,
      javaScriptEnabled: true,
      serviceWorkers: 'block',
      bypassCSP: false,
    });
    await context.clearCookies();

    const outcome = await probeTarget(context, url, deadline);
    if (outcome) {
      out.status = outcome.status;
      out.is_empty = outcome.isEmpty;
    }
  } catch {
    // A probe that could not run is not evidence that anything changed. The
    // caller treats a null status as inconclusive and sends anyway, which is
    // the right asymmetry: a wrong "still broken" costs a false claim, and a
    // wrong "now fixed" costs a lead nobody was going to write to twice.
  } finally {
    if (context) await context.close().catch(() => undefined);
    if (browser) await browser.close().catch(() => undefined);
  }

  out.duration_ms = Date.now() - started;
  return out;
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
      const key = dedupeKey(item.url);
      if (seen.has(key) || item.depth > req.max_depth) continue;
      seen.add(key);

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
            if (!seen.has(dedupeKey(next)) && queue.length < req.max_pages * 3) {
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

    // After the crawl, with whatever budget is left: the one measurement the
    // worker has always declared and never taken. Failure here is swallowed --
    // an unmeasured call to action simply stays unmeasured, which is the state
    // every crawl before this one shipped in, and it must not cost us a crawl
    // that otherwise succeeded.
    try {
      await probeCtaTargets(context, result, deadline);
    } catch (err) {
      result.failure_reason ??= `cta_probe_failed: ${(err as Error).message.slice(0, 200)}`;
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
