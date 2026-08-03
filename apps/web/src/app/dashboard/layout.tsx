import React from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import DemoDataBanner from '@/components/layout/DemoDataBanner';

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <>
      {/* Gap analysis H-20. These screens still render fabricated data; the
          Phase 8 rebuild against /api/v1 is not in this build, so the demo
          status is labelled rather than disguised. */}
      <DemoDataBanner />
      <DashboardLayout>{children}</DashboardLayout>
    </>
  );
}
