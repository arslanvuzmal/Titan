/**
 * robots.txt compliance.
 *
 * Every case here failed against the hand-rolled parser these tests were
 * written for. Two of them let Titan fetch a path the site had explicitly
 * refused, which is the failure that matters: robots is the entire consent
 * mechanism available to a site that never asked to be crawled.
 *
 * The parser is exercised through robots-parser directly rather than through
 * runCrawl, so the rules can be checked without a browser or a network.
 */

import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import robotsParser from 'robots-parser';

const ORIGIN = 'https://bellrose-dental.test';
const UA = 'TitanBot/0.2 (+https://arslanvuzmallone.com)';

function allows(body: string, path: string, userAgent = UA): boolean {
  const robots = robotsParser(`${ORIGIN}/robots.txt`, body);
  return robots.isAllowed(`${ORIGIN}${path}`, userAgent) ?? true;
}

describe('robots.txt', () => {
  test('a plain Disallow is refused', () => {
    assert.equal(allows('User-agent: *\nDisallow: /admin\n', '/admin'), false);
  });

  test('a path no rule mentions is allowed', () => {
    assert.equal(allows('User-agent: *\nDisallow: /admin\n', '/contact'), true);
  });

  test('an empty robots.txt allows everything', () => {
    assert.equal(allows('', '/anything'), true);
  });

  test('a suffix wildcard is honoured', () => {
    // startsWith('/*.pdf$') never matched, so this was crawled.
    assert.equal(allows('User-agent: *\nDisallow: /*.pdf$\n', '/brochure.pdf'), false);
  });

  test('a mid-path wildcard is honoured', () => {
    assert.equal(
      allows('User-agent: *\nDisallow: /private/*/secret\n', '/private/a/secret'),
      false,
    );
  });

  test('Allow overrides a broader Disallow', () => {
    assert.equal(allows('User-agent: *\nDisallow: /\nAllow: /contact\n', '/contact'), true);
  });

  test('the more specific rule wins regardless of order', () => {
    assert.equal(
      allows('User-agent: *\nAllow: /private/public\nDisallow: /private\n', '/private/public'),
      true,
    );
  });

  test('an empty User-agent value does not capture us', () => {
    // '' matched via includes(), so this blocked the whole site.
    assert.equal(allows('User-agent:\nDisallow: /\n', '/anything'), true);
  });

  test('a group named for another crawler does not capture us', () => {
    // 'bot' is a substring of every plausible crawler name, ours included.
    assert.equal(allows('User-agent: bot\nDisallow: /\n', '/contact'), true);
  });

  test('a group naming us specifically is obeyed', () => {
    assert.equal(allows('User-agent: TitanBot\nDisallow: /\n', '/contact'), false);
  });

  test('a specific group replaces the wildcard group rather than adding to it', () => {
    const body = 'User-agent: *\nDisallow: /\n\nUser-agent: TitanBot\nDisallow: /admin\n';
    assert.equal(allows(body, '/contact'), true);
    assert.equal(allows(body, '/admin'), false);
  });

  test('comments and blank lines are ignored', () => {
    const body = '# a comment\n\nUser-agent: *   # trailing\nDisallow: /admin\n';
    assert.equal(allows(body, '/admin'), false);
  });
});
