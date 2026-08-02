/**
 * SSRF guard, worker-side.
 *
 * This mirrors `titan/security/url_guard.py`. Both exist deliberately: the
 * control plane vets the seed URL before dispatch, and the worker re-vets every
 * link it discovers and every redirect it follows -- because those URLs come
 * from the page, which is hostile input the control plane never saw.
 *
 * Neither side trusts the other. The control plane re-validates the redirect
 * chain the worker reports, so a compromised worker cannot smuggle a
 * private-origin capture into the evidence store.
 */

import { promises as dns } from 'node:dns';
import net from 'node:net';

export const ALLOWED_SCHEMES = new Set(['http:', 'https:']);
export const ALLOWED_PORTS = new Set([80, 443]);

const BLOCKED_HOSTNAMES = new Set([
  'localhost',
  'localhost.localdomain',
  'ip6-localhost',
  'ip6-loopback',
  'metadata.google.internal',
  'metadata.goog',
  'metadata',
  'instance-data',
  'instance-data.ec2.internal',
  'metadata.packet.net',
  'metadata.platformequinix.com',
]);

const BLOCKED_SUFFIXES = [
  '.local',
  '.localdomain',
  '.internal',
  '.intranet',
  '.corp',
  '.home',
  '.lan',
  '.onion',
  '.test',
  '.example',
  '.invalid',
  '.localhost',
];

export interface UrlVerdict {
  allowed: boolean;
  url: string;
  reason?: string;
  hostname?: string;
  resolvedIps?: string[];
}

/**
 * Test-only escape hatch for the local fixture sites.
 *
 * The fixture server necessarily binds loopback, which the guard exists to
 * refuse. Rather than weaken the guard, this opens a narrow hole that:
 *
 *   - is off unless TITAN_UNSAFE_ALLOW_LOOPBACK is exactly "1";
 *   - permits ONLY 127.0.0.1/::1 on the configured fixture port;
 *   - is asserted absent from every production config by an invariant test.
 *
 * It is read once at module load so a later mutation of process.env cannot
 * enable it mid-run.
 */
const ALLOW_LOOPBACK = process.env.TITAN_UNSAFE_ALLOW_LOOPBACK === '1';
const LOOPBACK_TEST_PORT = Number(process.env.TITAN_FIXTURE_PORT ?? 8899);

function loopbackFixtureAllowed(host: string, port: number): boolean {
  if (!ALLOW_LOOPBACK) return false;
  const isLoopbackHost = host === '127.0.0.1' || host === '::1' || host === 'localhost';
  return isLoopbackHost && port === LOOPBACK_TEST_PORT;
}

function ipv4ToInt(ip: string): number {
  return ip.split('.').reduce((acc, part) => (acc << 8) + Number(part), 0) >>> 0;
}

function inCidr(ip: string, cidr: string): boolean {
  const [range, bitsRaw] = cidr.split('/');
  const bits = Number(bitsRaw);
  const mask = bits === 0 ? 0 : (~0 << (32 - bits)) >>> 0;
  return (ipv4ToInt(ip) & mask) === (ipv4ToInt(range) & mask);
}

const PRIVATE_V4 = [
  '0.0.0.0/8',
  '10.0.0.0/8',
  '100.64.0.0/10',
  '127.0.0.0/8',
  '169.254.0.0/16',
  '172.16.0.0/12',
  '192.0.0.0/24',
  '192.0.2.0/24',
  '192.168.0.0/16',
  '198.18.0.0/15',
  '198.51.100.0/24',
  '203.0.113.0/24',
  '224.0.0.0/4',
  '240.0.0.0/4',
];

/**
 * Unwrap IPv4 addresses embedded in IPv6 forms. `::ffff:169.254.169.254` is the
 * cloud metadata service wearing a v6 costume, and a naive check misses it.
 */
function embeddedIpv4(ip: string): string | null {
  const lower = ip.toLowerCase();
  const mapped = lower.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/);
  if (mapped) return mapped[1];
  const hexMapped = lower.match(/^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/);
  if (hexMapped) {
    const hi = parseInt(hexMapped[1], 16);
    const lo = parseInt(hexMapped[2], 16);
    return `${(hi >> 8) & 255}.${hi & 255}.${(lo >> 8) & 255}.${lo & 255}`;
  }
  const sixToFour = lower.match(/^2002:([0-9a-f]{1,4}):([0-9a-f]{1,4}):/);
  if (sixToFour) {
    const hi = parseInt(sixToFour[1], 16);
    const lo = parseInt(sixToFour[2], 16);
    return `${(hi >> 8) & 255}.${hi & 255}.${(lo >> 8) & 255}.${lo & 255}`;
  }
  return null;
}

export function isPublicAddress(ip: string): boolean {
  const version = net.isIP(ip);
  if (version === 0) return false;

  if (version === 4) {
    return !PRIVATE_V4.some((cidr) => inCidr(ip, cidr));
  }

  const lower = ip.toLowerCase();
  if (lower === '::1' || lower === '::' || lower.startsWith('fe80:')) return false;
  // Unique-local fc00::/7 covers fc.. and fd..
  if (/^f[cd][0-9a-f]{2}:/.test(lower)) return false;
  if (/^ff[0-9a-f]{2}:/.test(lower)) return false; // multicast

  const embedded = embeddedIpv4(lower);
  if (embedded !== null) return isPublicAddress(embedded);
  return true;
}

export async function validateUrl(
  rawUrl: string,
  opts: { resolver?: (host: string) => Promise<string[]> } = {},
): Promise<UrlVerdict> {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return { allowed: false, url: rawUrl, reason: 'malformed_url' };
  }

  if (!ALLOWED_SCHEMES.has(parsed.protocol)) {
    return { allowed: false, url: rawUrl, reason: `scheme_not_allowed:${parsed.protocol}` };
  }
  if (parsed.username || parsed.password) {
    return { allowed: false, url: rawUrl, reason: 'credentials_in_url' };
  }

  const port = parsed.port ? Number(parsed.port) : parsed.protocol === 'https:' ? 443 : 80;
  const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '');
  if (!host) return { allowed: false, url: rawUrl, reason: 'missing_host' };

  // The fixture hatch is evaluated before the port allowlist because the
  // fixture server necessarily listens on a non-standard port. It still
  // requires TITAN_UNSAFE_ALLOW_LOOPBACK=1 *and* an exact host/port match.
  if (loopbackFixtureAllowed(host, port)) {
    return { allowed: true, url: rawUrl, hostname: host, resolvedIps: ['127.0.0.1'] };
  }

  if (!ALLOWED_PORTS.has(port)) {
    return { allowed: false, url: rawUrl, reason: `port_not_allowed:${port}` };
  }

  if (BLOCKED_HOSTNAMES.has(host)) {
    return { allowed: false, url: rawUrl, reason: `blocked_hostname:${host}` };
  }
  if (BLOCKED_SUFFIXES.some((s) => host.endsWith(s))) {
    return { allowed: false, url: rawUrl, reason: `blocked_suffix:${host}` };
  }

  if (net.isIP(host) !== 0) {
    return isPublicAddress(host)
      ? { allowed: true, url: rawUrl, hostname: host, resolvedIps: [host] }
      : { allowed: false, url: rawUrl, reason: `private_address:${host}` };
  }

  let addresses: string[];
  try {
    addresses = opts.resolver
      ? await opts.resolver(host)
      : (await dns.lookup(host, { all: true, verbatim: true })).map((a) => a.address);
  } catch (err) {
    return { allowed: false, url: rawUrl, reason: `dns_failure:${(err as Error).message}` };
  }

  if (addresses.length === 0) {
    return { allowed: false, url: rawUrl, reason: 'no_addresses' };
  }
  // EVERY address must pass. One public and one private record is a rebinding
  // attempt, not a misconfiguration.
  const bad = addresses.filter((a) => !isPublicAddress(a));
  if (bad.length > 0) {
    return { allowed: false, url: rawUrl, reason: `private_address:${bad.join(',')}` };
  }

  return { allowed: true, url: rawUrl, hostname: host, resolvedIps: addresses };
}
