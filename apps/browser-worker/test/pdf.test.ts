import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { chromium, type Browser } from 'playwright';
import { inlineArtifacts, pdfKeyFor, renderPdf } from '../src/pdf.js';
import { purgeOld, saveShot } from '../src/shots.js';

const DRAFT = '0b9b6c1e-3f0a-4c55-9d1e-7a2f0c4b8e11';
let browser: Browser;

before(async () => {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  browser = await chromium.launch(executablePath ? { executablePath } : {});
});
after(async () => {
  await browser.close();
});

test('a pdf key is only ever pdfs/<uuid>.pdf', () => {
  assert.equal(pdfKeyFor(DRAFT), `pdfs/${DRAFT}.pdf`);
  assert.throws(() => pdfKeyFor('../../etc/passwd'));
  assert.throws(() => pdfKeyFor('x'));
});

test('only a screenshot key is inlined; anything else is dropped', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'pdf-'));
  const key = await saveShot(dir, Buffer.from('not really a jpeg'));
  const html =
    `<img src="artifact:${key}"><img src="artifact:../../secrets/mailboxes.json">` +
    `<img src="artifact:shots/zz/nope.jpg">`;
  const out = await inlineArtifacts(html, dir);
  assert.match(out, /data:image\/jpeg;base64,/);
  assert.equal((out.match(/src=""/g) ?? []).length, 2);
  assert.doesNotMatch(out, /secrets/);
});

test('the page cannot fetch anything from the network', async () => {
  let hits = 0;
  const server = http.createServer((_req, res) => {
    hits += 1;
    res.end('x');
  });
  await new Promise<void>((resolve) => server.listen(0, resolve));
  const port = (server.address() as { port: number }).port;
  try {
    const html =
      `<html><body><h1>Check</h1><img src="http://127.0.0.1:${port}/beacon.png">` +
      `<script src="http://127.0.0.1:${port}/x.js"></script></body></html>`;
    const out = await renderPdf(browser, { draftId: DRAFT, html }, null);
    assert.ok(out.bytes > 0);
    assert.equal(hits, 0);
  } finally {
    server.close();
  }
});

test('a one-page document renders as one page and is written under pdfs/', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'pdf-'));
  const html =
    '<html><head><style>@page{size:A4;margin:0}body{margin:0;font:14px sans-serif}</style></head>' +
    '<body><div style="padding:20mm"><h1>Website check</h1><p>One page.</p></div></body></html>';
  const out = await renderPdf(browser, { draftId: DRAFT, html }, dir);
  assert.equal(out.pages, 1);
  assert.equal(out.key, `pdfs/${DRAFT}.pdf`);
  const written = await fs.readFile(path.join(dir, out.key!));
  assert.equal(written.subarray(0, 5).toString(), '%PDF-');
});

test('old pdfs are purged with the screenshots', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'pdf-'));
  await fs.mkdir(path.join(dir, 'pdfs'));
  const stale = path.join(dir, 'pdfs', `${DRAFT}.pdf`);
  await fs.writeFile(stale, '%PDF-');
  const old = new Date(Date.now() - 30 * 24 * 3600 * 1000);
  await fs.utimes(stale, old, old);
  assert.equal(await purgeOld(dir, 21, Date.now(), 'pdfs'), 1);
});

test('a document over its limits is rendered but not saved', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'pdf-'));
  const html =
    '<html><head><style>@page{size:A4;margin:0}div{height:297mm}</style></head>' +
    '<body><div>one</div><div>two</div></body></html>';
  const out = await renderPdf(browser, { draftId: DRAFT, html, maxPages: 1 }, dir);
  assert.equal(out.key, null);
  assert.ok(out.pages > 1);
  assert.match(out.refused ?? "", /^[0-9]+ pages$/);
  await assert.rejects(fs.access(path.join(dir, 'pdfs', `${DRAFT}.pdf`)));
});
