/**
 * Which auth mode this build talks to.
 *
 * Deliberately a plain module with no `'use client'` directive, because it is
 * read from both a server component (the CRM layout) and client components.
 *
 * This used to live in `session.tsx`. That was a trap: every export of a
 * `'use client'` module becomes a *client reference proxy* when a server
 * component imports it, and a proxy is truthy. `CLERK_ENABLED ? a : b` in the
 * layout therefore always chose Clerk, even with no key configured — the CRM
 * rendered "Clerk auth is selected but no publishable key is set" on a purely
 * local development machine.
 *
 * `NEXT_PUBLIC_` variables are inlined at build time and are readable on both
 * sides, so this file gives the same answer everywhere.
 */

export const CLERK_PUBLISHABLE_KEY =
  process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY ?? '';

/**
 * True when this build authenticates through Clerk.
 *
 * Must match the API's `TITAN_AUTH_MODE`. If they disagree the CRM shows a
 * sign-in form whose tokens the API will not accept, so keep them in step.
 */
export const CLERK_ENABLED = CLERK_PUBLISHABLE_KEY.length > 0;
