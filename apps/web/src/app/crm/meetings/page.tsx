'use client';

/**
 * Calls somebody asked for.
 *
 * Every meeting here opens with no time on it, and that is deliberate rather
 * than unfinished. Replies name times in every form English allows -- "Tuesday
 * afternoon", "after the bank holiday" -- each relative to a timezone and a
 * working week Titan cannot see. A wrong time does not read as a parsing
 * failure; it reads as a confirmed appointment, and the cost lands on the
 * operator who misses it and the prospect who was stood up.
 *
 * So this page is a queue. The request is quoted verbatim, and a person sets
 * the time.
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

/** How long a request has been waiting, in whole days. */
function waitingDays(since: string): number {
  const ms = Date.now() - new Date(since).getTime();
  return Math.max(0, Math.floor(ms / 86_400_000));
}

export default function MeetingsPage() {
  const [unscheduledOnly, setUnscheduledOnly] = React.useState(false);
  const { data, error, loading, reload } = useApi(
    (t) => api.meetings(t, unscheduledOnly),
    [unscheduledOnly],
  );
  const stats = useApi((t) => api.stats(t), []);

  const rows = data ?? [];
  const total = stats.data?.meetings_total ?? 0;
  const waiting = stats.data?.meetings_unscheduled ?? 0;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Meetings</h1>
        <p className="mt-1 text-sm text-slate-500">
          Opened when a reply asks to talk. Titan does not guess a time out of a
          reply, so each one waits for a person to confirm the slot.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Requested" value={total} tone={total > 0 ? 'good' : 'neutral'} />
        <Stat
          label="Awaiting a time"
          value={waiting}
          tone={waiting > 0 ? 'warn' : 'neutral'}
          hint={waiting > 0 ? 'Somebody is waiting on a reply' : undefined}
        />
        <Stat label="Showing" value={rows.length} />
      </div>

      <label className="flex items-center gap-2 text-sm text-slate-600">
        <input
          type="checkbox"
          checked={unscheduledOnly}
          onChange={(e) => setUnscheduledOnly(e.target.checked)}
          className="h-4 w-4 rounded border-slate-300"
        />
        Only those still without a time
      </label>

      <Card title="Requests" subtitle="Unscheduled first">
        {loading ? (
          <Spinner />
        ) : error ? (
          <ErrorNote error={error} onRetry={reload} />
        ) : rows.length === 0 ? (
          <Empty>
            No calls requested yet. One opens automatically when a reply asks to
            talk.
          </Empty>
        ) : (
          <Table head={['Business', 'What they asked', 'When', 'Waiting', 'Status']}>
            {rows.map((row) => {
              const days = waitingDays(row.created_at);
              return (
                <tr key={row.id} className="border-t border-slate-100">
                  <td className="px-3 py-2">
                    <LeadLink id={row.lead_id}>
                      {row.organization_name ?? 'Unknown business'}
                    </LeadLink>
                  </td>
                  <td className="max-w-md px-3 py-2">
                    <p className="whitespace-pre-line text-xs text-slate-600">
                      {row.notes ?? '—'}
                    </p>
                  </td>
                  <td className="px-3 py-2 text-sm text-slate-600">
                    {row.scheduled_at ? (
                      <Time value={row.scheduled_at} />
                    ) : (
                      <span className="text-amber-700">not set</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-sm tabular-nums text-slate-600">
                    {row.scheduled_at ? '—' : `${days}d`}
                  </td>
                  <td className="px-3 py-2">
                    <Badge tone={row.scheduled_at ? 'good' : 'warn'}>{row.status}</Badge>
                  </td>
                </tr>
              );
            })}
          </Table>
        )}
      </Card>
    </div>
  );
}
