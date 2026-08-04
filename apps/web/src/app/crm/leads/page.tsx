'use client';

/**
 * The lead list.
 *
 * Filters live in the URL so a working view is linkable and survives a reload.
 * Server-side filtering and paging: the client never receives rows it is not
 * showing, which also means the displayed total is the server's total for the
 * same filters rather than a count of what happened to arrive.
 */

import { useRouter, useSearchParams } from 'next/navigation';
import React, { Suspense, useCallback, useMemo, useState } from 'react';
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  ExternalLink,
  LeadLink,
  ScoreBadge,
  Spinner,
  Table,
  Time,
  Value,
} from '@/components/crm/ui';
import { useApi } from '@/lib/session';
import { api, type LeadFilters } from '@/lib/titan';

const STATUSES = [
  'discovered',
  'researching',
  'researched',
  'qualified',
  'manual_review',
  'rejected',
  'drafted',
  'awaiting_approval',
  'queued',
  'contacted',
  'replied',
  'meeting_booked',
  'disqualified',
  'suppressed',
  'archived',
];

const PAGE_SIZE = 50;

function LeadsView() {
  const router = useRouter();
  const params = useSearchParams();

  const filters: LeadFilters = useMemo(
    () => ({
      q: params.get('q') ?? undefined,
      status: params.get('status') ?? undefined,
      min_score: params.get('min_score') ? Number(params.get('min_score')) : undefined,
      campaign_id: params.get('campaign_id') ?? undefined,
      has_reply: params.get('has_reply') === 'true' ? true : undefined,
      contacted: params.get('contacted') === 'false' ? false : undefined,
      sort: (params.get('sort') as LeadFilters['sort']) ?? 'score',
      direction: (params.get('direction') as LeadFilters['direction']) ?? 'desc',
      limit: PAGE_SIZE,
      offset: Number(params.get('offset') ?? 0),
    }),
    [params],
  );

  const key = params.toString();
  const { data, error, loading, reload } = useApi((t) => api.leads(t, filters), [key]);
  const campaigns = useApi((t) => api.campaigns(t), []);
  const [search, setSearch] = useState(filters.q ?? '');

  const setParam = useCallback(
    (updates: Record<string, string | undefined>) => {
      const next = new URLSearchParams(params.toString());
      for (const [k, v] of Object.entries(updates)) {
        if (v === undefined || v === '') next.delete(k);
        else next.set(k, v);
      }
      // Any filter change invalidates the current page position.
      if (!('offset' in updates)) next.delete('offset');
      router.push(`/crm/leads?${next.toString()}`);
    },
    [params, router],
  );

  const offset = filters.offset ?? 0;
  const total = data?.total ?? 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Leads</h1>
          <p className="mt-1 text-sm text-slate-500">
            {loading ? 'Loading…' : `${total} matching ${total === 1 ? 'lead' : 'leads'}`}
          </p>
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setParam({ q: search.trim() || undefined });
          }}
          className="flex gap-2"
        >
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search business or domain"
            className="w-64 rounded-lg border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-slate-500"
          />
          <Button type="submit">Search</Button>
        </form>
      </div>

      <Card>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <select
            value={filters.status ?? ''}
            onChange={(e) => setParam({ status: e.target.value || undefined })}
            className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-sm"
          >
            <option value="">Any status</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.replace(/_/g, ' ')}
              </option>
            ))}
          </select>

          <select
            value={filters.campaign_id ?? ''}
            onChange={(e) => setParam({ campaign_id: e.target.value || undefined })}
            className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-sm"
          >
            <option value="">Any campaign</option>
            {(campaigns.data?.items ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>

          <select
            value={filters.min_score?.toString() ?? ''}
            onChange={(e) => setParam({ min_score: e.target.value || undefined })}
            className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-sm"
          >
            <option value="">Any score</option>
            <option value="85">85+ (high priority)</option>
            <option value="70">70+ (qualified)</option>
            <option value="55">55+ (manual review)</option>
          </select>

          <select
            value={`${filters.sort}:${filters.direction}`}
            onChange={(e) => {
              const [sort, direction] = e.target.value.split(':');
              setParam({ sort, direction });
            }}
            className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-sm"
          >
            <option value="score:desc">Score, highest first</option>
            <option value="score:asc">Score, lowest first</option>
            <option value="created:desc">Newest first</option>
            <option value="created:asc">Oldest first</option>
            <option value="contacted:desc">Recently contacted</option>
            <option value="next_action:asc">Next action due</option>
          </select>

          <label className="flex items-center gap-1.5 text-slate-600">
            <input
              type="checkbox"
              checked={filters.has_reply === true}
              onChange={(e) => setParam({ has_reply: e.target.checked ? 'true' : undefined })}
            />
            Replied
          </label>

          <label className="flex items-center gap-1.5 text-slate-600">
            <input
              type="checkbox"
              checked={filters.contacted === false}
              onChange={(e) => setParam({ contacted: e.target.checked ? 'false' : undefined })}
            />
            Never contacted
          </label>

          {key && (
            <Button variant="ghost" onClick={() => router.push('/crm/leads')}>
              Clear
            </Button>
          )}
        </div>
      </Card>

      {error ? (
        <ErrorNote error={error} onRetry={reload} />
      ) : loading && !data ? (
        <Spinner />
      ) : !data || data.items.length === 0 ? (
        <Card>
          <Empty>
            No lead matches these filters. Discovery populates this list; nothing
            here is sample data.
          </Empty>
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <Table
            head={[
              'Business',
              'Score',
              'Status',
              'Campaign',
              'Findings',
              'Contactable',
              'Last contacted',
              'Rating',
            ]}
          >
            {data.items.map((lead) => (
              <tr key={lead.id} className="hover:bg-slate-50">
                <td className="px-3 py-2">
                  <LeadLink id={lead.id}>
                    {lead.organization?.display_name ?? 'Unnamed organization'}
                  </LeadLink>
                  <div className="text-xs text-slate-500">
                    {lead.organization?.canonical_domain ? (
                      <ExternalLink href={lead.organization.website_url ?? `https://${lead.organization.canonical_domain}`}>
                        {lead.organization.canonical_domain}
                      </ExternalLink>
                    ) : (
                      <Value>{null}</Value>
                    )}
                    {lead.organization?.locality && ` · ${lead.organization.locality}`}
                  </div>
                </td>
                <td className="px-3 py-2">
                  <ScoreBadge score={lead.latest_score} />
                </td>
                <td className="px-3 py-2">
                  <Badge>{lead.status}</Badge>
                </td>
                <td className="px-3 py-2 text-slate-600">
                  <Value>{lead.campaign_name}</Value>
                </td>
                <td className="px-3 py-2 tabular-nums text-slate-600">
                  {lead.finding_count}
                  <span className="text-slate-400"> / {lead.evidence_count} ev.</span>
                </td>
                <td className="px-3 py-2">
                  <Badge tone={lead.has_eligible_contact ? 'good' : 'warn'}>
                    {lead.has_eligible_contact ? 'yes' : 'no'}
                  </Badge>
                </td>
                <td className="px-3 py-2 text-xs">
                  <Time value={lead.last_contacted_at} />
                </td>
                <td className="px-3 py-2 text-slate-600">
                  <Value>
                    {lead.organization?.rating
                      ? `${lead.organization.rating} (${lead.organization.review_count ?? 0})`
                      : null}
                  </Value>
                </td>
              </tr>
            ))}
          </Table>

          <div className="mt-3 flex items-center justify-between text-sm text-slate-600">
            <span>
              {offset + 1}–{Math.min(offset + data.items.length, total)} of {total}
            </span>
            <div className="flex gap-2">
              <Button
                disabled={offset === 0}
                onClick={() => setParam({ offset: String(Math.max(0, offset - PAGE_SIZE)) })}
              >
                Previous
              </Button>
              <Button
                disabled={offset + PAGE_SIZE >= total}
                onClick={() => setParam({ offset: String(offset + PAGE_SIZE) })}
              >
                Next
              </Button>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}

export default function LeadsPage() {
  return (
    <Suspense fallback={<Spinner />}>
      <LeadsView />
    </Suspense>
  );
}
