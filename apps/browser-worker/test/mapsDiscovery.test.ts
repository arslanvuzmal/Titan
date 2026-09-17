/**
 * The boundary this module is held inside.
 *
 * Google's robots.txt allows `/maps/search/` and disallows `/maps/` place
 * pages. Titan attaches a one-pager to every message saying "robots.txt
 * obeyed", so that boundary is not a preference -- it is what keeps a document
 * this system sends to strangers true.
 *
 * These tests guard it structurally rather than by running a browser. A
 * network test would pass today and tell nobody when somebody later edits the
 * URL, which is the only way this gets broken.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { discoverOnMaps } from '../src/mapsDiscovery.js';

const SOURCE = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'mapsDiscovery.ts'),
  'utf8',
);

test('an empty query is refused before anything is launched', async () => {
  const result = await discoverOnMaps({
    query: '   ',
    userAgent: 'TitanBot/1.0',
    timeoutSeconds: 10,
    maxResults: 10,
  });
  assert.equal(result.status, 'failed');
  assert.equal(result.blocked_reason, 'empty_query');
  assert.deepEqual(result.businesses, []);
});

test('the only navigation target is built from /maps/search/', () => {
  // One goto, and it navigates to `target` -- which is assembled from
  // SEARCH_ORIGIN and `/maps/search/` a few lines above it. A caller cannot
  // supply a URL, so a place page is unreachable through this function.
  const gotos = SOURCE.match(/\.goto\(([^,)]+)/g) ?? [];
  assert.equal(gotos.length, 1, `expected exactly one navigation, found ${gotos.length}`);
  assert.match(gotos[0], /\.goto\(target/);
  assert.match(SOURCE, /const path = `\/maps\/search\//);
});

test('robots is consulted on every run, not trusted from a comment', () => {
  assert.match(SOURCE, /robotsAllowsSearch\(/);
  // A refusal and an unreadable robots.txt both stop the run.
  assert.match(SOURCE, /allowed !== true/);
  assert.match(SOURCE, /robots_unreadable/);
});

test('nothing in here tries to get around a block', () => {
  // Naming the absence, because the tempting additions are all one npm
  // install away and each of them would make the one-pager's claim false.
  //
  // Checked against the code with comments stripped, not the whole file: the
  // module's own documentation says it does not spoof fingerprints, and a
  // check that cannot tell an explanation from an implementation would forbid
  // saying so. This test failed on exactly that on first run.
  const code = SOURCE.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  for (const banned of [
    'stealth',
    'puppeteer-extra',
    'solveCaptcha',
    'rotateUserAgent',
    'fingerprint',
    'proxy',
  ]) {
    assert.ok(
      !code.includes(banned),
      `mapsDiscovery must not reference ${banned}: a block is an answer, not an obstacle`,
    );
  }
});

test('a detected block is reported rather than retried', () => {
  assert.match(SOURCE, /status = 'blocked'/);
  assert.match(SOURCE, /google_declined/);
});

test('the scroll loop is bounded three ways', () => {
  // A deadline, a result ceiling, and a stability check. Any one of them
  // missing is a lane that never frees.
  assert.match(SOURCE, /Date\.now\(\) < deadline/);
  assert.match(SOURCE, /seen\.size < opts\.maxResults/);
  assert.match(SOURCE, /stable < STABLE_PASSES_BEFORE_STOP/);
});
