/**
 * The browser worker's HTTP surface.
 *
 * Deliberately tiny: one research endpoint, one health endpoint, one readiness
 * endpoint. This service is the only place in ColdOps that fetches arbitrary
 * URLs, and it holds no credentials for email, models, or the database.
 */

import http from 'node:http';
import crypto from 'node:crypto';
import { recheckUrl, runCrawl } from './crawler.js';
import { discoverOnMaps } from './mapsDiscovery.js';
import type { ResearchRequest } from './contract.js';
import { WORKER_VERSION } from './contract.js';
import { artifactDir, purgeOld, retentionDays } from './shots.js';
import { chromium } from 'playwright';
import { renderPdf } from './pdf.js';

const PORT = Number(process.env.BROWSER_WORKER_PORT ?? 8800);
const TOKEN = process.env.BROWSER_WORKER_TOKEN ?? '';
const MAX_BODY = 256 * 1024;
const MAX_CONCURRENT = Number(process.env.BROWSER_WORKER_CONCURRENCY ?? 2);

let inFlight = 0;

function timingSafeEqual(a: string, b: string): boolean {
  const ab = Buffer.from(a);
  const bb = Buffer.from(b);
  if (ab.length !== bb.length) return false;
  return crypto.timingSafeEqual(ab, bb);
}

function send(res: http.ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'content-type': 'application/json',
    'x-content-type-options': 'nosniff',
    'content-length': Buffer.byteLength(payload),
  });
  res.end(payload);
}

async function readBody(req: http.IncomingMessage): Promise<string> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > MAX_BODY) throw new Error('request body too large');
    chunks.push(chunk as Buffer);
  }
  return Buffer.concat(chunks).toString('utf8');
}

function validateRequest(raw: unknown): ResearchRequest {
  const r = raw as Partial<ResearchRequest>;
  if (typeof r?.request_id !== 'string' || !r.request_id) throw new Error('request_id required');
  if (typeof r?.seed_url !== 'string' || !r.seed_url) throw new Error('seed_url required');
  // Clamp rather than trust: even the control plane's limits are bounded here,
  // so a compromised caller cannot ask for an unbounded crawl.
  const clamp = (v: unknown, lo: number, hi: number, dflt: number): number => {
    const n = typeof v === 'number' && Number.isFinite(v) ? v : dflt;
    return Math.min(hi, Math.max(lo, Math.trunc(n)));
  };
  return {
    request_id: r.request_id.slice(0, 128),
    seed_url: r.seed_url.slice(0, 2048),
    max_pages: clamp(r.max_pages, 1, 50, 12),
    max_depth: clamp(r.max_depth, 0, 3, 2),
    timeout_seconds: clamp(r.timeout_seconds, 5, 300, 120),
    max_response_bytes: clamp(r.max_response_bytes, 10_000, 20_000_000, 5_000_000),
    max_redirects: clamp(r.max_redirects, 0, 10, 5),
    user_agent: (r.user_agent ?? 'Mozilla/5.0 (compatible; SiteCheck/1.0; +https://arslanvuzmallone.com/bot)').slice(0, 300),
    respect_robots: r.respect_robots !== false,
    capture_screenshots: r.capture_screenshots !== false,
    run_lighthouse: r.run_lighthouse === true,
    run_axe: r.run_axe !== false,
    priority_paths: Array.isArray(r.priority_paths) ? r.priority_paths.slice(0, 20).map(String) : [],
    pinned_ips: Array.isArray(r.pinned_ips) ? r.pinned_ips.slice(0, 16).map(String) : [],
  };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', `http://localhost:${PORT}`);

  if (req.method === 'GET' && url.pathname === '/health') {
    return send(res, 200, { status: 'ok', worker_version: WORKER_VERSION });
  }
  if (req.method === 'GET' && url.pathname === '/ready') {
    // Readiness is separate from liveness: a saturated worker is alive but not
    // ready, so the load balancer stops sending it work instead of killing it.
    return inFlight < MAX_CONCURRENT
      ? send(res, 200, { status: 'ready', in_flight: inFlight })
      : send(res, 503, { status: 'saturated', in_flight: inFlight });
  }

  const isResearch = req.method === 'POST' && url.pathname === '/research';
  // One URL, no crawl. Used to ask whether a claim ColdOps is about to send is
  // still true -- see recheckUrl in crawler.ts for why it lives on this side
  // of the credential boundary.
  const isRecheck = req.method === 'POST' && url.pathname === '/recheck';
  // Maps search, which robots.txt allows, as a supplement to the Places API
  // rather than a replacement for it. Places is cheaper and licensed; what it
  // cannot do is return more than sixty results for a query. See
  // mapsDiscovery.ts for the robots boundary this is held inside.
  const isDiscover = req.method === 'POST' && url.pathname === '/discover';
  // The personal PDF attached to a first email. No URL is fetched: the HTML
  // arrives whole, images come from this worker's own screenshot volume, and
  // every network request the page makes is refused. See pdf.ts.
  const isRenderPdf = req.method === 'POST' && url.pathname === '/render-pdf';

  if (!isResearch && !isRecheck && !isDiscover && !isRenderPdf) {
    return send(res, 404, { error: 'not_found' });
  }

  if (TOKEN) {
    const header = req.headers.authorization ?? '';
    const presented = header.startsWith('Bearer ') ? header.slice(7) : '';
    if (!presented || !timingSafeEqual(presented, TOKEN)) {
      return send(res, 401, { error: 'unauthorized' });
    }
  }

  if (inFlight >= MAX_CONCURRENT) {
    return send(res, 503, { error: 'worker_saturated', retry_after_seconds: 10 });
  }

  inFlight += 1;
  try {
    const body = JSON.parse(await readBody(req));
    if (isRenderPdf) {
      const draftId = typeof body?.draft_id === 'string' ? body.draft_id : '';
      const html = typeof body?.html === 'string' ? body.html : '';
      if (!draftId || !html) throw new Error('draft_id and html are required');
      const launchOptions: any = { args: ['--disable-dev-shm-usage', '--no-zygote'] };
      if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH) {
        launchOptions.executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
      }
      const browser = await chromium.launch(launchOptions);
      try {
        const out = await renderPdf(
          browser,
          {
            draftId,
            html,
            maxBytes: typeof body?.max_bytes === 'number' ? body.max_bytes : undefined,
            maxPages: typeof body?.max_pages === 'number' ? body.max_pages : undefined,
          },
          // A preview is returned, never saved where the outbox worker looks.
          body?.save === false ? null : artifactDir(),
        );
        send(res, 200, {
          key: out.key,
          refused: out.refused,
          bytes: out.bytes,
          pages: out.pages,
          // Only on request: the API reads the file from the shared volume.
          pdf_base64: body?.return_pdf === true ? out.pdf.toString('base64') : undefined,
        });
      } finally {
        await browser.close();
      }
      return;
    }
    if (isRecheck) {
      const target = typeof body?.url === 'string' ? body.url : '';
      if (!target) throw new Error('url is required');
      const result = await recheckUrl(target, {
        userAgent:
          typeof body?.user_agent === 'string' && body.user_agent
            ? body.user_agent
            : 'Mozilla/5.0 (compatible; SiteCheck/1.0; +https://arslanvuzmallone.com/bot)',
        timeoutSeconds:
          typeof body?.timeout_seconds === 'number' ? body.timeout_seconds : 25,
      });
      send(res, 200, result);
      return;
    }
    if (isDiscover) {
      const query = typeof body?.query === 'string' ? body.query : '';
      if (!query) throw new Error('query is required');
      const result = await discoverOnMaps({
        query,
        userAgent:
          typeof body?.user_agent === 'string' && body.user_agent
            ? body.user_agent
            : 'Mozilla/5.0 (compatible; SiteCheck/1.0; +https://arslanvuzmallone.com/bot)',
        timeoutSeconds:
          typeof body?.timeout_seconds === 'number' ? body.timeout_seconds : 90,
        // Bounded here as well as by the caller: an unbounded scroll is a
        // request that never ends and a worker lane that never frees.
        maxResults: Math.max(
          1,
          Math.min(typeof body?.max_results === 'number' ? body.max_results : 120, 400),
        ),
        hl: typeof body?.hl === 'string' ? body.hl : 'en',
      });
      send(res, 200, result);
      return;
    }
    const parsed = validateRequest(body);
    const result = await runCrawl(parsed);
    send(res, 200, result);
  } catch (err) {
    send(res, 400, { error: 'bad_request', detail: (err as Error).message.slice(0, 300) });
  } finally {
    inFlight -= 1;
  }
});

server.headersTimeout = 10_000;
server.requestTimeout = 320_000;

function shutdown(signal: string): void {
  process.stderr.write(`browser-worker: ${signal} received, draining\n`);
  server.close(() => process.exit(0));
  // Force exit if in-flight crawls do not finish in time.
  setTimeout(() => process.exit(0), 30_000).unref();
}
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

server.listen(PORT, () => {
  process.stderr.write(`browser-worker ${WORKER_VERSION} listening on :${PORT}\n`);
});

// Screenshot retention. Hourly, and once at start so a worker that was down for
// a week does not wait an hour to catch up.
async function sweepShots(): Promise<void> {
  const dir = artifactDir();
  if (!dir) return;
  try {
    const removed = await purgeOld(dir, retentionDays());
    if (removed > 0) console.log(JSON.stringify({ event: 'shots_purged', removed }));
    const pdfs = await purgeOld(dir, retentionDays(), Date.now(), 'pdfs');
    if (pdfs > 0) console.log(JSON.stringify({ event: 'pdfs_purged', removed: pdfs }));
  } catch (err) {
    console.error(JSON.stringify({ event: 'shots_purge_failed', error: String(err) }));
  }
}
void sweepShots();
setInterval(() => void sweepShots(), 3600 * 1000).unref();
