import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fingerprintOf, purgeOld, saveShot, storageKeyFor } from '../src/shots.js';

test('a shot is named by its content, so a re-crawl reuses the file', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'shots-'));
  const bytes = Buffer.from('jpeg bytes');
  const first = await saveShot(dir, bytes);
  const second = await saveShot(dir, bytes);
  assert.equal(first, second);
  assert.equal(first, storageKeyFor(fingerprintOf(bytes)));
  assert.deepEqual(await fs.readFile(path.join(dir, first)), bytes);
});

test('a storage key cannot be steered outside the directory', () => {
  assert.throws(() => storageKeyFor('../../etc/passwd'));
  assert.throws(() => storageKeyFor('ABC'));
});

test('only files older than the window are purged', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'shots-'));
  const fresh = await saveShot(dir, Buffer.from('fresh'));
  const stale = await saveShot(dir, Buffer.from('stale'));
  const old = new Date(Date.now() - 30 * 24 * 3600 * 1000);
  await fs.utimes(path.join(dir, stale), old, old);
  assert.equal(await purgeOld(dir, 21), 1);
  await fs.access(path.join(dir, fresh));
  await assert.rejects(fs.access(path.join(dir, stale)));
});

test('a missing directory is nothing to purge, not an error', async () => {
  assert.equal(await purgeOld(path.join(os.tmpdir(), 'no-such-dir-xyz'), 21), 0);
});
