import type { Metadata } from 'next';
import React from 'react';
import { Shell } from '@/components/crm/Shell';
import { SessionProvider } from '@/lib/session';

export const metadata: Metadata = {
  title: 'Titan-OS CRM',
  description:
    'Evidence-first lead research, qualification, and controlled outreach.',
};

export default function CrmLayout({ children }: { children: React.ReactNode }) {
  return (
    <SessionProvider>
      <Shell>{children}</Shell>
    </SessionProvider>
  );
}
