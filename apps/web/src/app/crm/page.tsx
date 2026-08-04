'use client';

/**
 * CRM overview.
 *
 * Every number here is a live COUNT from /api/v1/stats. There is no derived
 * "pipeline value", no projected revenue, and no conversion estimate, because
 * Titan measures page facts and delivery outcomes -- it does not measure
 * business results, and a dashboard that displays one it did not measure is
 * the exact failure the pre-0.2 build shipped.
 */

import Link from 'next/link';
import React from 'react';
import { Badge, Card, ErrorNote, Spinner, Stat } from '@/components/crm/ui';
import { useApi } from '@/lib/session';
import { api } from '@/lib/titan';

function Distribution({
  data,
  href,
  emptyLabel,
}: {
  data: Record<string, number>;
  href?: (key: string) => string;
  emptyLabel: string;
}) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((sum, [, n]) => sum + n, 0);
  if (entries.length === 0) {
    return <p className="py-4 text-sm text-slate-500">{emptyLabel}</p>;
  }
  return (
    <ul className="space-y-2">
      {entries.map(([key, count]) => {
        const row = (
          <>
            <div className="flex items-center justify-between gap-3">
              <Badge>{key}</Badge>
              <span className="text-sm font-semibold tabular-nums text-slate-900">{count}</span>
            </div>
            <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-slate-100">
              <div
                className="h-full rounded-full bg-slate-800"
                style={{ width: `${total ? (count / total) * 100 : 0}%` }}
              />
            </div>
          </>
        );
        return (
          <li key={key}>
            {href ? (
              <Link href={href(key)} className="block rounded-lg p-1 hover:bg-slate-50">
                {row}
              </Link>
            ) : (
              row
            )}
          </li>
        );
      })}
    </ul>
  );
}

export default function OverviewPage() {
  const { data, error, loading, reload } = useApi((t) => api.stats(t), []);

  if (loading && !data) return <Spinner label="Loading workspace" />;
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data) return null;

  const awaiting = data.drafts_by_status.awaiting_approval ?? 0;
  const bounced =
    (data.messages_by_state.bounced ?? 0) + (data.messages_by_state.complained ?? 0);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Overview</h1>
        <p className="mt-1 text-sm text-slate-500">
          Live counts from the operational database. Nothing on this page is
          estimated or projected.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Leads" value={data.leads_total} hint={`${data.organizations_total} businesses`} />
        <Stat
          label="Awaiting approval"
          value={awaiting}
          hint="drafts a human has not yet reviewed"
          tone={awaiting > 0 ? 'good' : 'neutral'}
        />
        <Stat
          label="Contactable addresses"
          value={data.eligible_contacts}
          hint={`of ${data.contacts_total} contacts; pattern guesses excluded`}
        />
        <Stat
          label="Bounced or complained"
          value={bounced}
          tone={bounced > 0 ? 'bad' : 'neutral'}
          hint="reputation-affecting outcomes"
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="Pipeline" subtitle="Leads by status">
          <Distribution
            data={data.leads_by_status}
            href={(key) => `/crm/leads?status=${encodeURIComponent(key)}`}
            emptyLabel="No leads yet. Run discovery to populate the workspace."
          />
        </Card>

        <Card title="Qualification" subtitle="Leads by score band">
          <Distribution
            data={data.leads_by_band}
            emptyLabel="No lead has been scored yet."
          />
        </Card>

        <Card title="Delivery" subtitle="Messages by state">
          <Distribution
            data={data.messages_by_state}
            emptyLabel="Nothing has been queued for delivery."
          />
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Evidence" subtitle="What claims can be traced to">
          <dl className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <dt className="text-xs uppercase tracking-wide text-slate-500">Findings</dt>
              <dd className="mt-1 text-2xl font-semibold tabular-nums">{data.findings_total}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-slate-500">Evidence rows</dt>
              <dd className="mt-1 text-2xl font-semibold tabular-nums">{data.evidence_total}</dd>
            </div>
          </dl>
          <p className="mt-3 text-xs text-slate-500">
            A message sentence may only assert something the recipient can check
            if it maps to a finding backed by at least one evidence row.
          </p>
        </Card>

        <Card title="Sending state" subtitle="Both switches must be on">
          <dl className="space-y-2 text-sm">
            <div className="flex items-center justify-between">
              <dt className="text-slate-600">Operating mode</dt>
              <dd>
                <Badge>{data.operating_mode}</Badge>
              </dd>
            </div>
            <div className="flex items-center justify-between">
              <dt className="text-slate-600">Delivery authorized</dt>
              <dd>
                <Badge tone={data.sending_authorized ? 'good' : 'warn'}>
                  {data.sending_authorized ? 'yes' : 'no'}
                </Badge>
              </dd>
            </div>
            <div className="flex items-center justify-between">
              <dt className="text-slate-600">Suppression entries</dt>
              <dd className="font-semibold tabular-nums">{data.suppressions_total}</dd>
            </div>
            <div className="flex items-center justify-between">
              <dt className="text-slate-600">Leads that replied</dt>
              <dd className="font-semibold tabular-nums">{data.replied_total}</dd>
            </div>
          </dl>
          <p className="mt-3 text-xs text-slate-500">
            &quot;Delivery authorized&quot; is the conjunction of the workspace
            flag and the process kill switch. Either one off means nothing
            leaves the building.
          </p>
        </Card>
      </div>
    </div>
  );
}
