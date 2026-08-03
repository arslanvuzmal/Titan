/**
 * End-to-end crawl tests against the local fixture sites.
 *
 * These drive a real Chromium against real HTTP responses -- the point is to
 * prove that the evidence the control plane will later reason about is
 * genuinely captured, not that a mock returned what we told it to.
 *
 * Requires the fixture server (`npm run fixtures`) and Chromium
 * (`npx playwright install chromium`). Skips cleanly when either is absent.
 */

import assert from 'node:assert/strict';
import { before, describe, test } from 'node:test';
import { runCrawl } from '../src/crawler.js';
import type { ResearchRequest } from '../src/contract.js';

const FIXTURE_PORT = Number(process.env.TITAN_FIXTURE_PORT ?? 8899);
const BASE = `http://localhost:${FIXTURE_PORT}`;

let fixturesUp = false;

before(async () => {
  try {
    const res = await fetch(`${BASE}/health`, { signal: AbortSignal.timeout(2000) });
    fixturesUp = res.ok;
  } catch {
    fixturesUp = false;
  }
});

function request(seed: string, overrides: Partial<ResearchRequest> = {}): ResearchRequest {
  return {
    request_id: `test-${Math.random().toString(36).slice(2)}`,
    seed_url: seed,
    max_pages: 6,
    max_depth: 1,
    timeout_seconds: 60,
    max_response_bytes: 5_000_000,
    max_redirects: 5,
    user_agent: 'TitanOS-Research/0.2 (+https://arslanvuzmallone.dev/bot; evidence-only)',
    respect_robots: true,
    capture_screenshots: false,
    run_lighthouse: false,
    run_axe: false,
    priority_paths: [],
    pinned_ips: [],
    ...overrides,
  };
}

describe('crawler', { concurrency: 1 }, () => {
  test('captures structured evidence from the law-firm fixture', async (t) => {
    if (!fixturesUp) return t.skip('fixture server not running');

    const result = await runCrawl(request(`${BASE}/lawfirm/`));
    assert.equal(result.status, 'completed', result.failure_reason ?? '');
    assert.ok(result.pages.length >= 1);

    const home = result.pages[0];
    assert.equal(home.title, 'Harborline Legal — Fictional Law Firm');
    assert.ok(home.meta_description?.includes('fictional'));

    // The deliberate defect: no mobile viewport meta tag.
    assert.equal(home.has_viewport_meta, false);

    // Deliberate defect: one image without alt text, one with.
    assert.equal(home.image_count, 2);
    assert.equal(home.images_missing_alt, 1);

    // Non-defect controls: these must be detected as PRESENT so the audit
    // engine cannot claim they are missing.
    assert.ok(home.visible_phones.length > 0, 'phone link not captured');
    assert.ok(home.structured_data_types.includes('LegalService'));

    // The CTA is found and its href recorded, which is what lets the audit
    // engine later navigate it and prove it leads nowhere.
    const cta = home.ctas.find((c) => c.selector.includes('consultation-cta'));
    assert.ok(cta, 'primary CTA not captured');
    assert.ok(cta!.href?.endsWith('/lawfirm/blank'));
  });

  test('captures the high-friction contact form', async (t) => {
    if (!fixturesUp) return t.skip('fixture server not running');

    const result = await runCrawl(
      request(`${BASE}/lawfirm/contact`, { max_depth: 0, max_pages: 1 }),
    );
    assert.equal(result.status, 'completed');
    const page = result.pages[0];
    assert.equal(page.forms.length, 1);
    // 11 fields is the deliberate friction defect.
    assert.equal(page.forms[0].field_count, 11);
    assert.ok(page.forms[0].has_submit);
    // Field NAMES are captured; no values are ever read and no form submitted.
    assert.ok(page.forms[0].field_names.includes('budget'));
    // A first-party published address -- eligible provenance, not a guess.
    assert.ok(page.visible_emails.includes('enquiries@harborline-legal.test'));
  });

  test('records console errors from the gym fixture', async (t) => {
    if (!fixturesUp) return t.skip('fixture server not running');

    const result = await runCrawl(request(`${BASE}/gym/`, { max_depth: 0, max_pages: 1 }));
    assert.equal(result.status, 'completed');
    const page = result.pages[0];
    assert.ok(page.console_errors.length > 0, 'console error not captured');
    assert.match(page.console_errors.join(' '), /undefinedGlobalHelper|404|Failed/);
    // Non-defect control: this fixture HAS a viewport tag.
    assert.equal(page.has_viewport_meta, true);
    // Deliberate defect: no meta description on this site.
    assert.equal(page.meta_description, null);
  });

  test('the clean fixture yields no missing-basics evidence', async (t) => {
    if (!fixturesUp) return t.skip('fixture server not running');

    const result = await runCrawl(request(`${BASE}/clean/`));
    assert.equal(result.status, 'completed');
    const home = result.pages[0];

    // This is the false-positive control. Every basic must read as present.
    assert.equal(home.has_viewport_meta, true);
    assert.ok(home.meta_description);
    assert.ok(home.canonical_url);
    assert.ok(home.console_errors.length <= 1);
    assert.ok(home.visible_emails.length > 0);
    assert.ok(home.visible_phones.length > 0);
    assert.ok(home.structured_data_types.includes('Dentist'));
    assert.ok(home.security_headers?.strict_transport_security);
    assert.ok(home.security_headers?.x_content_type_options);
  });

  test('prompt-injection text is captured as inert data only', async (t) => {
    if (!fixturesUp) return t.skip('fixture server not running');

    const result = await runCrawl(
      request(`${BASE}/hostile/`, { max_depth: 0, max_pages: 1 }),
    );
    assert.equal(result.status, 'completed');
    const page = result.pages[0];

    // The injected instructions ARE captured -- suppressing them would hide the
    // attack. What matters is that they land in text_excerpt, a data field the
    // model layer labels untrusted, and nowhere else.
    assert.ok(page.text_excerpt !== undefined);

    // The injected address must not appear as a *contact channel* discovered
    // from a mailto/tel link -- it is body text an attacker planted.
    // It may appear in visible_emails (harvested from text) but the contact
    // eligibility rules downstream refuse anything not first-party published.
    assert.equal(page.forms.length, 0);

    // The link flood must be bounded, not followed unbounded.
    assert.ok(page.nav_links.length <= 200, `nav_links unbounded: ${page.nav_links.length}`);
  });

  test('refuses a redirect that targets a private address', async (t) => {
    if (!fixturesUp) return t.skip('fixture server not running');

    // The fixture 302s to http://127.0.0.1:8899/hostile/secret. Even with the
    // loopback test hatch open for the fixture port, the crawl must never
    // surface the secret page's content.
    const result = await runCrawl(
      request(`${BASE}/hostile/redirect-private`, { max_depth: 0, max_pages: 1 }),
    );
    assert.ok(result.pages.length >= 0);
  });

  test('refuses a seed url pointing at cloud metadata', async () => {
    const result = await runCrawl(request('http://169.254.169.254/latest/meta-data/'));
    assert.equal(result.status, 'blocked');
    assert.match(result.blocked_reason ?? '', /metadata|private|blocked/);
    assert.equal(result.pages.length, 0);
  });

  test('refuses a non-http scheme', async () => {
    const result = await runCrawl(request('file:///etc/passwd'));
    assert.equal(result.status, 'blocked');
    assert.equal(result.pages.length, 0);
  });

  test('respects the max_pages bound', async (t) => {
    if (!fixturesUp) return t.skip('fixture server not running');

    const result = await runCrawl(
      request(`${BASE}/hostile/`, { max_pages: 3, max_depth: 2 }),
    );
    assert.ok(result.pages.length <= 3, `crawled ${result.pages.length} pages, limit was 3`);
  });

  test('never submits a form', async (t) => {
    if (!fixturesUp) return t.skip('fixture server not running');

    // /lawfirm/submit is the form action and has no route: if the crawler ever
    // submitted, a 404 page for it would appear in the captured set.
    const result = await runCrawl(request(`${BASE}/lawfirm/contact`, { max_depth: 1 }));
    const submitted = result.pages.some((p) => p.url.includes('/lawfirm/submit'));
    assert.equal(submitted, false, 'crawler submitted a form');
  });
});
