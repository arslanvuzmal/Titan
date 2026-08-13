'use client';

/**
 * What the evidence supports selling, and what it does not.
 *
 * Opportunities are derived from findings that were *measured* -- a model
 * cannot create one, for the same reason it cannot create a finding. Each row
 * names how many findings back it.
 *
 * Two things this page deliberately does not show. There is no pipeline total
 * on the deliverable value, because a sum of catalogue prices for work nobody
 * has agreed to buy is not a pipeline and displaying it as one is the failure
 * the pre-0.2 dashboard shipped. And gaps carry no price at all: attaching a
 * number to work the owner does not do would put revenue in a figure nobody
 * can deliver.
 */

import React from 'react';
import {
  Badge,
  Card,
  Empty,
  ErrorNote,
  LeadLink,
  Spinner,
  Stat,
  Table,
  Time,
} from '@/components/crm/ui';
import { useApi } from '@/lib/session';
import { api } from '@/lib/titan';

type Filter = 'all' | 'deliverable' | 'gaps';

const money = (value: number | null) =>
  value === null
    ? '—'
    : value.toLocaleString(undefined, {
        style: 'currency',
        currency: 'USD',
        maximumFractionDigits: 0,
      });

export default function OpportunitiesPage() {
  const [filter, setFilter] = React.useState<Filter>('all');
  const deliverable =
    filter === 'all' ? undefined : filter === 'deliverable' ? true : false;

  const { data, error, loading, reload } = useApi(
    (t) => api.opportunities(t, deliverable),
    [filter],
  );
  const stats = useApi((t) => api.stats(t), []);

  const rows = data ?? [];
  const sellable = stats.data?.opportunities_deliverable ?? 0;
  const gaps = stats.data?.opportunities_unserved ?? 0;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Opportunities</h1>
        <p className="mt-1 text-sm text-slate-500">
          Derived from evidenced findings and the offers this workspace sells.
          A problem no offer covers is recorded as a gap rather than dropped, so
          the audit does not look like the site was sound in that respect.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Deliverable" value={sellable} tone={sellable > 0 ? 'good' : 'neutral'} />
        <Stat label="Gaps" value={gaps} hint="Evidenced, but nothing to sell against" />
        <Stat label="Showing" value={rows.length} />
      </div>

      <div className="flex flex-wrap gap-2">
        {(
          [
            ['all', 'All'],
            ['deliverable', 'Deliverable'],
            ['gaps', 'Gaps'],
          ] as [Filter, string][]
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setFilter(key)}
            className={
              'rounded-md px-3 py-1.5 text-sm font-medium transition-colors ' +
              (filter === key
                ? 'bg-slate-900 text-white'
                : 'bg-white text-slate-600 ring-1 ring-slate-200 hover:bg-slate-50')
            }
          >
            {label}
          </button>
        ))}
      </div>

      <Card title="Opportunities" subtitle="Highest priority first">
        {loading ? (
          <Spinner />
        ) : error ? (
          <ErrorNote error={error} onRetry={reload} />
        ) : rows.length === 0 ? (
          <Empty>
            Nothing yet. Opportunities appear once the research pipeline has
            analysed a lead and found something an offer covers.
          </Empty>
        ) : (
          <Table head={['Business', 'Opportunity', 'Evidence', 'Value', 'Found']}>
            {rows.map((row) => (
              <tr key={row.id} className="border-t border-slate-100">
                <td className="px-3 py-2">
                  <LeadLink id={row.lead_id}>
                    {row.organization_name ?? 'Unknown business'}
                  </LeadLink>
                </td>
                <td className="px-3 py-2">
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-slate-900">{row.title}</span>
                    {!row.deliverable && <Badge tone="warn">gap</Badge>}
                  </div>
                  {row.rationale && (
                    <p className="mt-0.5 text-xs text-slate-500">{row.rationale}</p>
                  )}
                </td>
                <td className="px-3 py-2 text-sm tabular-nums text-slate-600">
                  {row.supporting_finding_count}
                </td>
                <td className="px-3 py-2 text-sm tabular-nums text-slate-900">
                  {money(row.estimated_value_usd)}
                </td>
                <td className="px-3 py-2 text-sm text-slate-500">
                  <Time value={row.created_at} />
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <p className="text-xs text-slate-400">
        Value is the offer&rsquo;s catalogue price for work of this kind. It is
        not a forecast, not weighted by likelihood, and is deliberately not
        totalled: nobody has agreed to buy any of it.
      </p>
    </div>
  );
}
