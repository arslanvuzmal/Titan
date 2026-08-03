import React from 'react';

/**
 * Unmissable notice that the dashboard is showing fabricated data.
 *
 * Gap analysis H-20: the pre-0.2 dashboard rendered invented activity
 * ("Acme Corp ($150k ARR Potential)", "FinanceBot requested wire transfer
 * $45,000") indistinguishably from real data. The Phase 8 rebuild that replaces
 * these screens with live API data is not in this build, so the honest interim
 * fix is to make the demo status impossible to miss rather than to leave a
 * convincing-looking fake in place.
 *
 * Remove this component when the dashboard reads from /api/v1.
 */
export default function DemoDataBanner() {
  return (
    <div
      role="alert"
      className="sticky top-0 z-50 border-b-2 border-amber-500 bg-amber-100 px-4 py-2 text-center text-sm font-semibold text-amber-900"
    >
      <span aria-hidden="true">⚠ </span>
      DEMONSTRATION DATA — every figure, company, and activity on these screens is
      fabricated. This dashboard is not connected to Titan-OS. See{' '}
      <code className="rounded bg-amber-200 px-1 py-0.5 font-mono text-xs">
        docs/audits/FINAL-PRODUCTION-VERIFICATION.md
      </code>
      .
    </div>
  );
}
