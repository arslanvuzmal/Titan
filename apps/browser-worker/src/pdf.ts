/**
 * The personal PDF: one page, about one business, rendered from HTML the API wrote.
 *
 * The API owns the words; this side owns Chromium and the screenshot files, so
 * the API sends HTML whose images are named `artifact:shots/xx/<sha256>.jpg`
 * and this resolves them from its own volume. Nothing else is resolved: the
 * page is rendered with every network request refused, so HTML that somehow
 * carried a remote image or script would render without it rather than fetch
 * it.
 *
 * **Small on purpose.** A full-page desktop screenshot is several hundred
 * kilobytes and Chromium embeds images as they are. Each `<img data-crop>` is
 * cropped to the top of the page (what a visitor sees first) and re-encoded
 * at the width it is shown, in the page itself, before printing -- which is
 * what keeps the file inside the send gate's attachment limit.
 *
 * Saved as `pdfs/<draft-id>.pdf`, write-then-rename, and purged with the
 * screenshots it was made from.
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import type { Browser } from 'playwright';

const SHOT_KEY = /^shots\/[0-9a-f]{2}\/[0-9a-f]{64}\.jpg$/;
const DRAFT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const ARTIFACT_SRC = /src="artifact:([^"]+)"/g;

/** Refuse anything larger than this outright; the send gate's ceiling is lower. */
export const MAX_HTML_BYTES = 200_000;

export function pdfKeyFor(draftId: string): string {
  if (!DRAFT_ID.test(draftId)) throw new Error('draft_id must be a uuid');
  return `pdfs/${draftId}.pdf`;
}

/** Replace every `artifact:` image with a data URI, or drop it if the key is not a screenshot. */
export async function inlineArtifacts(html: string, dir: string | null): Promise<string> {
  const keys = new Set<string>();
  for (const match of html.matchAll(ARTIFACT_SRC)) keys.add(match[1]);
  let out = html;
  for (const key of keys) {
    let replacement = 'src=""';
    if (dir && SHOT_KEY.test(key)) {
      try {
        const bytes = await fs.readFile(path.join(dir, key));
        replacement = `src="data:image/jpeg;base64,${bytes.toString('base64')}"`;
      } catch {
        // Purged or never written: the page renders without that picture.
      }
    }
    out = out.split(`src="artifact:${key}"`).join(replacement);
  }
  return out;
}

/**
 * Crop and shrink every `img[data-crop]` in place. Runs inside the page.
 *
 * A string, not a function: `page.evaluate(fn)` serialises the function's
 * source, and a bundler that rewrites it (tsx adds a `__name` helper to named
 * inner functions) produces source that throws inside the page. A string is
 * what runs, whatever compiled the worker.
 */
const SHRINK_IMAGES = `(async () => {
  const images = Array.from(document.querySelectorAll('img[data-crop]'));
  await Promise.all(images.map((img) => new Promise((resolve) => {
    function shrink() {
      try {
        if (!img.naturalWidth) return resolve();
        const width = Number(img.dataset.width || 600);
        const ratio = Number(img.dataset.crop || 0.625);
        const scale = width / img.naturalWidth;
        const srcHeight = Math.min(img.naturalHeight, Math.round((width * ratio) / scale));
        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = Math.round(srcHeight * scale);
        const ctx = canvas.getContext('2d');
        if (!ctx) return resolve();
        ctx.drawImage(img, 0, 0, img.naturalWidth, srcHeight, 0, 0, canvas.width, canvas.height);
        img.onload = () => resolve();
        img.onerror = () => resolve();
        img.src = canvas.toDataURL('image/jpeg', 0.6);
      } catch (e) {
        resolve();
      }
    }
    if (img.complete) shrink();
    else { img.onload = shrink; img.onerror = () => resolve(); }
  })));
})()`;

export interface RenderedPdf {
  /** Null when nothing was written: no directory, or the document broke a limit. */
  key: string | null;
  /** Why it was not saved, when it was rendered but refused. */
  refused?: string;
  bytes: number;
  pages: number;
  pdf: Buffer;
}

export async function renderPdf(
  browser: Browser,
  {
    draftId,
    html,
    maxBytes,
    maxPages,
  }: { draftId: string; html: string; maxBytes?: number; maxPages?: number },
  dir: string | null,
): Promise<RenderedPdf> {
  if (Buffer.byteLength(html) > MAX_HTML_BYTES) throw new Error('html too large');
  const key = pdfKeyFor(draftId);
  const context = await browser.newContext({ javaScriptEnabled: true, offline: true });
  try {
    const page = await context.newPage();
    await page.route('**/*', (route) => route.abort());
    await page.setContent(await inlineArtifacts(html, dir), { waitUntil: 'load', timeout: 20_000 });
    await page.evaluate(SHRINK_IMAGES);
    const pdf = await page.pdf({
      format: 'A4',
      printBackground: true,
      preferCSSPageSize: true,
      margin: { top: '0', right: '0', bottom: '0', left: '0' },
    });
    const pages = (pdf.toString('latin1').match(/\/Type\s*\/Page[^s]/g) ?? []).length;
    // Refused rather than saved: a two-page "one-page check" or a file the
    // send gate would bounce is a document that should not exist on disk for
    // the outbox worker to find.
    if (maxPages && pages > maxPages) {
      return { key: null, refused: `${pages} pages`, bytes: pdf.length, pages, pdf };
    }
    if (maxBytes && pdf.length > maxBytes) {
      return { key: null, refused: `${pdf.length} bytes`, bytes: pdf.length, pages, pdf };
    }
    if (!dir) return { key: null, bytes: pdf.length, pages, pdf };
    const target = path.join(dir, key);
    await fs.mkdir(path.dirname(target), { recursive: true });
    const temp = `${target}.${process.pid}.tmp`;
    await fs.writeFile(temp, pdf);
    await fs.rename(temp, target);
    return { key, bytes: pdf.length, pages, pdf };
  } finally {
    await context.close();
  }
}
