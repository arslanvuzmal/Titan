/**
 * Finding businesses through Google Maps search.
 *
 * This exists alongside the Places API, not instead of it. Places is the
 * sanctioned route, it is cheap, and it returns a licensed record. What it
 * cannot do is go past sixty results for a query, and sixty is the reason a
 * city "runs dry" after one search when the city plainly has more businesses
 * in it.
 *
 * ## What this is allowed to read, and how that was established
 *
 * Google's robots.txt draws the line precisely:
 *
 *     Allow:    /maps/search/      <- this module
 *     Allow:    /maps/?q=
 *     Disallow: /maps/             <- /maps/place/... is in no Allow line
 *
 * So a search is permitted and an individual place page is not. That boundary
 * is enforced in code below, not left to whoever edits the URL next, because
 * Titan attaches a one-pager to every message saying "robots.txt obeyed" --
 * and a system whose discipline is never asserting what it cannot evidence
 * cannot afford that sentence to be false.
 *
 * robots.txt is checked live on every run rather than trusted from this
 * comment. If Google changes it, this stops.
 *
 * ## What this deliberately does not do
 *
 * It does not solve CAPTCHAs, rotate identities, spoof fingerprints, or retry
 * around a block. When Google declines to serve the page, that is an answer:
 * the run reports `blocked` with the reason and stops. Automation that hides
 * what it is would also make the one-pager's claim false, and an estate that
 * quietly evades detection is one nobody can honestly describe to a recipient.
 *
 * It sends the same self-identifying user agent as the crawler, which names
 * the operator and links to a page explaining the bot.
 *
 * ## Terms of service
 *
 * robots.txt and Google's ToS are different instruments. This module is
 * robots-compliant; the ToS restrict automated extraction regardless, and
 * running it is a commercial decision taken by the operator with that known.
 * Recorded here so nobody later discovers it by accident.
 */

import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import robotsParser from 'robots-parser';
import { WORKER_VERSION } from './contract.js';

/** One business as the results feed showed it. */
export interface MapsBusiness {
  /** Display name, as written on the card. */
  name: string;
  /** Google's own listing URL for the business. Provenance, not a crawl target. */
  listing_url: string | null;
  /** Star rating, when the card showed one. */
  rating: number | null;
  /** Review count, when the card showed one. */
  review_count: number | null;
  /** Category as Google labels it -- "Dentist", "Med spa". */
  category: string | null;
  /** Address line as shown. Rarely complete; Places is the better source. */
  address: string | null;
  /** Telephone, when the card exposed one. */
  phone: string | null;
  /**
   * Whether the card offered a "Website" action.
   *
   * The signal this module exists for. `false` is a business Google knows
   * about and that has nowhere to send anybody -- which Places can only tell
   * you by omission, and only for the sixty results it returns.
   */
  has_website: boolean;
  /** The website the card linked to, when it had one. */
  website: string | null;
}

export interface MapsDiscoveryResult {
  status: 'completed' | 'blocked' | 'failed';
  query: string;
  /** Why nothing was returned, when nothing was. */
  blocked_reason: string | null;
  robots_allowed: boolean | null;
  businesses: MapsBusiness[];
  /** How many scroll passes the feed accepted before it stopped growing. */
  scrolls: number;
  duration_ms: number;
  worker_version: string;
}

/** Only ever a search. Constructed here so no caller can pass a place URL. */
const SEARCH_ORIGIN = 'https://www.google.com';

/** Feed stops growing: two passes with no new cards and we are at the end. */
const STABLE_PASSES_BEFORE_STOP = 2;

/**
 * Politeness between scrolls.
 *
 * Not an evasion measure -- it is one request's worth of work at a human-ish
 * pace, on a surface robots permits. Lowering it would not make the run more
 * capable, only ruder.
 */
const SCROLL_PAUSE_MS = 1200;

/** Words Google uses on its own interstitials when it declines to serve. */
const BLOCK_MARKERS = [
  'unusual traffic',
  'not a robot',
  'recaptcha',
  'our systems have detected',
  'automated queries',
];

async function robotsAllowsSearch(userAgent: string, path: string): Promise<boolean | null> {
  try {
    const res = await fetch(`${SEARCH_ORIGIN}/robots.txt`, {
      headers: { 'user-agent': userAgent },
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) return null;
    const robots = robotsParser(`${SEARCH_ORIGIN}/robots.txt`, await res.text());
    return robots.isAllowed(`${SEARCH_ORIGIN}${path}`, userAgent) ?? null;
  } catch {
    // Unreadable robots.txt is not consent. For a surface this module is only
    // allowed on by an explicit Allow line, absence of proof is refusal.
    return null;
  }
}

/**
 * Accept the consent interstitial if one is shown.
 *
 * This is the EU cookie wall, not a bot check: clicking it is what a person
 * does, and declining leaves the results feed unrendered. The most
 * privacy-preserving option is chosen where the page offers one.
 */
async function passConsent(page: Page): Promise<void> {
  const reject = page.getByRole('button', { name: /reject all|decline|nur essenzielle/i });
  if (await reject.count().catch(() => 0)) {
    await reject.first().click({ timeout: 5000 }).catch(() => undefined);
    return;
  }
  const accept = page.getByRole('button', { name: /accept all|i agree|alle akzeptieren/i });
  if (await accept.count().catch(() => 0)) {
    await accept.first().click({ timeout: 5000 }).catch(() => undefined);
  }
}

async function looksBlocked(page: Page): Promise<string | null> {
  const text = (await page.textContent('body').catch(() => '') ?? '').toLowerCase();
  for (const marker of BLOCK_MARKERS) {
    if (text.includes(marker)) return `google_declined: ${marker}`;
  }
  return null;
}

/**
 * Read every card currently in the feed.
 *
 * Runs in the page so the DOM is read once rather than round-tripped per
 * field. Selectors are Google's and will change; everything is optional and a
 * missing field is null rather than an exception, so a layout change degrades
 * the harvest instead of failing the run.
 */
async function harvest(page: Page): Promise<MapsBusiness[]> {
  return page.evaluate(() => {
    const out: any[] = [];
    const cards = document.querySelectorAll('div[role="feed"] > div > div[jsaction]');
    cards.forEach((card) => {
      const nameEl = card.querySelector('div.fontHeadlineSmall, a[aria-label]');
      const name =
        nameEl?.textContent?.trim() ||
        (card.querySelector('a[aria-label]') as HTMLAnchorElement | null)?.getAttribute('aria-label') ||
        '';
      if (!name) return;

      const listing =
        (card.querySelector('a[href*="/maps/place/"]') as HTMLAnchorElement | null)?.href ?? null;

      // "4.8(123)" or "4.8 stars 123 reviews" depending on locale.
      const ratingText = card.querySelector('span[role="img"]')?.getAttribute('aria-label') ?? '';
      const ratingMatch = ratingText.match(/([\d.,]+)/);
      const reviewsMatch = ratingText.match(/([\d,]+)\s*(reviews|avis|bewertungen)/i);

      const website =
        (card.querySelector('a[data-value="Website"], a[aria-label*="Website"]') as HTMLAnchorElement | null)
          ?.href ?? null;

      const lines = Array.from(card.querySelectorAll('div.fontBodyMedium > div'))
        .map((n) => n.textContent?.trim() ?? '')
        .filter(Boolean);

      const phone = lines.map((l) => l.match(/\+?[\d][\d\s().-]{7,}/)?.[0] ?? '').find(Boolean) ?? null;

      out.push({
        name,
        listing_url: listing,
        rating: ratingMatch ? Number(ratingMatch[1].replace(',', '.')) : null,
        review_count: reviewsMatch ? Number(reviewsMatch[1].replace(/,/g, '')) : null,
        category: lines[0]?.split('·')[0]?.trim() || null,
        address: lines[0]?.split('·')[1]?.trim() || lines[1] || null,
        phone,
        has_website: Boolean(website),
        website,
      });
    });
    return out;
  }) as Promise<MapsBusiness[]>;
}

export async function discoverOnMaps(opts: {
  query: string;
  userAgent: string;
  timeoutSeconds: number;
  maxResults: number;
  /** Language and region, so results match the market being targeted. */
  hl?: string;
}): Promise<MapsDiscoveryResult> {
  const started = Date.now();
  const query = opts.query.trim();
  const result: MapsDiscoveryResult = {
    status: 'failed',
    query,
    blocked_reason: null,
    robots_allowed: null,
    businesses: [],
    scrolls: 0,
    duration_ms: 0,
    worker_version: WORKER_VERSION,
  };
  if (!query) {
    result.blocked_reason = 'empty_query';
    result.duration_ms = Date.now() - started;
    return result;
  }

  // Built here, never accepted from the caller. `/maps/search/` is the one
  // path robots allows; a place URL cannot be reached through this function.
  const path = `/maps/search/${encodeURIComponent(query)}`;
  const target = `${SEARCH_ORIGIN}${path}?hl=${encodeURIComponent(opts.hl ?? 'en')}`;

  const allowed = await robotsAllowsSearch(opts.userAgent, path);
  result.robots_allowed = allowed;
  if (allowed !== true) {
    result.status = 'blocked';
    result.blocked_reason =
      allowed === null ? 'robots_unreadable' : 'robots_disallows_maps_search';
    result.duration_ms = Date.now() - started;
    return result;
  }

  const deadline = started + Math.max(10, opts.timeoutSeconds) * 1000;
  let browser: Browser | null = null;
  let context: BrowserContext | null = null;

  try {
    const launchOptions: any = {
      args: ['--disable-dev-shm-usage', '--no-zygote', '--disable-extensions'],
    };
    if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH) {
      launchOptions.executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
    }
    browser = await chromium.launch(launchOptions);
    context = await browser.newContext({
      userAgent: opts.userAgent,
      viewport: { width: 1440, height: 1200 },
      serviceWorkers: 'block',
      bypassCSP: false,
    });
    const page = await context.newPage();
    await page.goto(target, { waitUntil: 'domcontentloaded', timeout: 30_000 });
    await passConsent(page);

    const blocked = await looksBlocked(page);
    if (blocked) {
      // Declining is an answer. Nothing here tries again in a different coat.
      result.status = 'blocked';
      result.blocked_reason = blocked;
      result.duration_ms = Date.now() - started;
      return result;
    }

    await page.waitForSelector('div[role="feed"]', { timeout: 20_000 }).catch(() => undefined);

    const seen = new Map<string, MapsBusiness>();
    let stable = 0;
    while (Date.now() < deadline && seen.size < opts.maxResults && stable < STABLE_PASSES_BEFORE_STOP) {
      for (const b of await harvest(page)) {
        // Listing URL is the stable identity; the name is not, because two
        // branches of one chain share it.
        const key = b.listing_url ?? `${b.name}|${b.address ?? ''}`;
        if (!seen.has(key)) seen.set(key, b);
      }
      const before = seen.size;
      await page
        .evaluate(() => {
          const feed = document.querySelector('div[role="feed"]');
          if (feed) feed.scrollTop = feed.scrollHeight;
        })
        .catch(() => undefined);
      result.scrolls += 1;
      await page.waitForTimeout(SCROLL_PAUSE_MS);
      stable = seen.size === before ? stable + 1 : 0;
    }

    result.businesses = Array.from(seen.values()).slice(0, opts.maxResults);
    result.status = 'completed';
  } catch (err) {
    result.status = 'failed';
    result.blocked_reason = `error: ${(err as Error).message}`.slice(0, 200);
  } finally {
    if (context) await context.close().catch(() => undefined);
    if (browser) await browser.close().catch(() => undefined);
  }

  result.duration_ms = Date.now() - started;
  return result;
}
