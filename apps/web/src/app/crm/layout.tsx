import type { Metadata } from 'next';
import React from 'react';
import { Shell } from '@/components/crm/Shell';
import { ClerkSessionProvider } from '@/lib/clerk-session';
import { CLERK_ENABLED } from '@/lib/authMode';
import { LocalSessionProvider } from '@/lib/session';

export const metadata: Metadata = {
  title: 'Titan-OS CRM',
  description:
    'Evidence-first lead research, qualification, and controlled outreach.',
};

// Chosen at build time from the presence of a Clerk publishable key, matching
// the API's own TITAN_AUTH_MODE. Both providers fill the same context, so no
// screen below here knows which one it is running under.
//
// CLERK_ENABLED must come from a module without 'use client': an export of a
// client module reaches a server component as a truthy proxy, which made this
// ternary always pick Clerk.
const SessionProvider = CLERK_ENABLED ? ClerkSessionProvider : LocalSessionProvider;

export default function CrmLayout({ children }: { children: React.ReactNode }) {
  return (
    <SessionProvider>
      <Shell>{children}</Shell>
    </SessionProvider>
  );
}
