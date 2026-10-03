/**
 * Where screenshots go once they are taken.
 *
 * Until October the crawler captured two screenshots of every homepage and
 * kept only their fingerprint: the row in browser_artifacts named a file that
 * was never written. The evidence page a prospect opens needs the picture, so
 * this writes it to a volume the API can read.
 *
 * **Named by content, not by crawl.** browser_artifacts is immutable and
 * dedupes on (content fingerprint, kind), so an unchanged homepage re-crawled
 * a week later keeps the *first* row and its storage key. A file named after
 * the crawl would be the one the purge deletes; a file named after its own
 * hash is the same file every time, and rewriting it refreshes its age.
 *
 * **Bounded.** JPEG at quality 60 is roughly a tenth of the PNG, and anything
 * not rewritten for ARTIFACT_RETENTION_DAYS is deleted -- evidence that old is
 * re-researched before it is shown to anyone, so nothing older is ever needed.
 */

import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';

export const SHOT_QUALITY = 60;

/** The configured directory, or null when screenshots are not kept. */
export function artifactDir(): string | null {
  const dir = (process.env.ARTIFACT_DIR ?? '').trim();
  return dir ? dir : null;
}

export function retentionDays(): number {
  const days = Number(process.env.ARTIFACT_RETENTION_DAYS ?? 21);
  return Number.isFinite(days) && days > 0 ? days : 21;
}

/** `shots/ab/abcdef….jpg` -- two-character fan-out keeps directories small. */
export function storageKeyFor(fingerprint: string): string {
  if (!/^[0-9a-f]{64}$/.test(fingerprint)) {
    throw new Error('fingerprint must be a sha256 hex digest');
  }
  return `shots/${fingerprint.slice(0, 2)}/${fingerprint}.jpg`;
}

export function fingerprintOf(bytes: Buffer): string {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

/** Write (or refresh) one screenshot. Returns its storage key. */
export async function saveShot(dir: string, bytes: Buffer): Promise<string> {
  const key = storageKeyFor(fingerprintOf(bytes));
  const target = path.join(dir, key);
  await fs.mkdir(path.dirname(target), { recursive: true });
  // Write-then-rename, so a reader never sees half a file.
  const temp = `${target}.${process.pid}.tmp`;
  await fs.writeFile(temp, bytes);
  await fs.rename(temp, target);
  return key;
}

/** Delete screenshots not rewritten within the retention window. Returns the count. */
export async function purgeOld(dir: string, days: number, now = Date.now()): Promise<number> {
  const root = path.join(dir, 'shots');
  const cutoff = now - days * 24 * 3600 * 1000;
  let removed = 0;
  let buckets: string[];
  try {
    buckets = await fs.readdir(root);
  } catch {
    return 0;
  }
  for (const bucket of buckets) {
    const folder = path.join(root, bucket);
    let files: string[];
    try {
      files = await fs.readdir(folder);
    } catch {
      continue;
    }
    for (const file of files) {
      const full = path.join(folder, file);
      try {
        const stat = await fs.stat(full);
        if (stat.mtimeMs < cutoff) {
          await fs.unlink(full);
          removed += 1;
        }
      } catch {
        // Gone already, or unreadable: neither is worth failing the sweep over.
      }
    }
  }
  return removed;
}
