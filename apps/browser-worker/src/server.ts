/**
 * The browser worker's HTTP surface.
 *
 * Deliberately tiny: one research endpoint, one health endpoint, one readiness
 * endpoint. This service is the only place in Titan that fetches arbitrary
 * URLs, and it holds no credentials for email, models, or the database.
 */

import http from 'node:http';
import crypto from 'node:crypto';
import { runCrawl } from './crawler.js';
import type { ResearchRequest } from './contract.js';
import { WORKER_VERSION } from './contract.js';

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
    user_agent: (r.user_agent ?? 'TitanOS-Research/0.2').slice(0, 300),
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

  if (req.method !== 'POST' || url.pathname !== '/research') {
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
    const parsed = validateRequest(JSON.parse(await readBody(req)));
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
