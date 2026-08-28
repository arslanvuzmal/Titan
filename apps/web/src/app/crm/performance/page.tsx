'use client';

/**
 * Delivery performance, grouped the ways decisions are actually made.
 *
 * Every counter here comes from `messages` -- what Titan handed to a mail
 * server, what bounced back, what somebody answered. There is no estimate and
 * no projection.
 *
 * **Two columns you will not find: opened and clicked.** Neither is measurable
 * without doing something to the message. An open is a tracking pixel, which
 * is a spam signal in its own right and is blocked by default in Apple Mail
 * and Gmail's image proxy -- so the number is both harmful to collect and
 * wrong once collected. A click is a rewritten link, which puts a different
 * domain in the anchor of a message whose whole argument is that the sender is
 * a real person who looked at your website. Titan sends neither, so it reports
 * neither. What it reports instead is the thing those metrics are a proxy for:
 * replies, and replies that went somewhere.
 *
 * A rate below the sample floor renders as "not enough yet", never as 0%. Four
 * sends and one bounce is not a 25% bounce rate, and any ranking built from
 * such numbers sorts mostly by who has the smallest sample.
 */

import React from 'react';
import { Badge, Card, Empty, ErrorNote, Spinner, Table } from '@/components/crm/ui';
import { useApi } from '@/lib/session';
import { api, type OutcomeRollup, type OutcomeSlice } from '@/lib/titan';

const DIMENSIONS: Array<{ key: string; label: string; blurb: string }> = [
  { key: 'campaign', label: 'Campaign', blurb: 'which campaigns are landing' },
  { key: 'sender', label: 'Mailbox', blurb: 'whether one mailbox is carrying the damage' },
  {
    key: 'recipient_domain',
    label: 'Recipient provider',
    blurb: 'whether one receiving network is filtering us',
  },
  { key: 'lead_source', label: 'Lead source', blurb: 'which lists are worth buying more of' },
  { key: 'local_slot', label: 'Send time', blurb: 'what hour in their day works' },
  { key: 'variant', label: 'Variant', blurb: 'which wording earns answers' },
];

const WINDOWS = [7, 30, 90];

function Rate({ value, warnAbove }: { value: number | null; warnAbove?: number }) {
  if (value === null) {
    return <span className="text-xs text-slate-400">not enough yet</span>;
  }
  const bad = warnAbove !== undefined && value > warnAbove;
  return (
    <span
      className={`text-sm font-semibold tabular-nums ${bad ? 'text-red-600' : 'text-slate-900'}`}
    >
      {(value * 100).toFixed(1)}%
    </span>
  );
}

function Num({ value, muted }: { value: number; muted?: boolean }) {
  return (
    <span
      className={`text-sm tabular-nums ${
        muted && value === 0 ? 'text-slate-300' : 'text-slate-900'
      }`}
    >
      {value}
    </span>
  );
}

function Rollup({ rollup }: { rollup: OutcomeRollup }) {
  const slices = [...rollup.slices].sort((a, b) => b.sent - a.sent);
  const totals = slices.reduce(
    (acc, s) => ({
      sent: acc.sent + s.sent,
      delivered: acc.delivered + s.delivered,
      bounced: acc.bounced + s.bounced,
      replied: acc.replied + s.replied,
      positive: acc.positive + s.positive_replies,
      meetings: acc.meetings + s.meetings,
    }),
    { sent: 0, delivered: 0, bounced: 0, replied: 0, positive: 0, meetings: 0 },
  );

  if (slices.length === 0) {
    return <Empty>Nothing sent in this window yet.</Empty>;
  }

  return (
    <>
      <Table
        head={[
          DIMENSIONS.find((d) => d.key === rollup.dimension)?.label ?? rollup.dimension,
          'Sent',
          'Bounced',
          'Bounce rate',
          'Replied',
          'Reply rate',
          'Positive',
          'Meetings',
        ]}
      >
        {slices.map((s: OutcomeSlice) => (
          <tr key={s.key} className="hover:bg-slate-50">
            <td className="px-3 py-2">
              <span className="text-sm font-medium text-slate-900">{s.label}</span>
              {!s.has_signal && (
                <span className="ml-2 text-xs text-slate-400">
                  below {rollup.sample_floor} sends
                </span>
              )}
            </td>
            <td className="px-3 py-2">
              <Num value={s.sent} />
            </td>
            <td className="px-3 py-2">
              <Num value={s.bounced} muted />
            </td>
            <td className="px-3 py-2">
              {/* 2% is where receiving networks start treating a sender as a
                  list rather than a person. */}
              <Rate value={s.bounce_rate} warnAbove={0.02} />
            </td>
            <td className="px-3 py-2">
              <Num value={s.replied} muted />
            </td>
            <td className="px-3 py-2">
              <Rate value={s.reply_rate} />
            </td>
            <td className="px-3 py-2">
              <Num value={s.positive_replies} muted />
            </td>
            <td className="px-3 py-2">
              <Num value={s.meetings} muted />
            </td>
          </tr>
        ))}
        <tr className="border-t-2 border-slate-300 bg-slate-50 font-semibold">
          <td className="px-3 py-2 text-sm">All</td>
          <td className="px-3 py-2">
            <Num value={totals.sent} />
          </td>
          <td className="px-3 py-2">
            <Num value={totals.bounced} />
          </td>
          <td className="px-3 py-2">
            <Rate
              value={totals.sent >= rollup.sample_floor ? totals.bounced / totals.sent : null}
              warnAbove={0.02}
            />
          </td>
          <td className="px-3 py-2">
            <Num value={totals.replied} />
          </td>
          <td className="px-3 py-2">
            <Rate
              value={totals.sent >= rollup.sample_floor ? totals.replied / totals.sent : null}
            />
          </td>
          <td className="px-3 py-2">
            <Num value={totals.positive} />
          </td>
          <td className="px-3 py-2">
            <Num value={totals.meetings} />
          </td>
        </tr>
      </Table>
    </>
  );
}

export default function PerformancePage() {
  const [dimension, setDimension] = React.useState('campaign');
  const [windowDays, setWindowDays] = React.useState(30);
  const { data, error, loading, reload } = useApi(
    (t) => api.outcomes(t, dimension, windowDays),
    [dimension, windowDays],
  );

  const active = DIMENSIONS.find((d) => d.key === dimension);
  const rollup = data?.find((r) => r.dimension === dimension) ?? data?.[0];

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Delivery performance</h1>
        <p className="mt-1 max-w-2xl text-sm text-slate-600">
          What was sent, what came back, and what somebody answered. Counted from the
          messages themselves &mdash; nothing here is projected.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {DIMENSIONS.map((d) => (
          <button
            key={d.key}
            type="button"
            onClick={() => setDimension(d.key)}
            className={`rounded-full px-3 py-1 text-sm transition ${
              d.key === dimension
                ? 'bg-slate-900 text-white'
                : 'bg-slate-100 text-slate-700 hover:bg-slate-200'
            }`}
          >
            {d.label}
          </button>
        ))}
        <span className="ml-auto flex items-center gap-1">
          {WINDOWS.map((w) => (
            <button
              key={w}
              type="button"
              onClick={() => setWindowDays(w)}
              className={`rounded px-2 py-1 text-xs transition ${
                w === windowDays
                  ? 'bg-slate-900 text-white'
                  : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
              }`}
            >
              {w}d
            </button>
          ))}
        </span>
      </div>

      {active && <p className="text-sm text-slate-500">Grouped to answer: {active.blurb}.</p>}

      <Card title={`${active?.label ?? dimension} · last ${windowDays} days`}>
        {loading && !data ? (
          <Spinner label="Reading delivery outcomes" />
        ) : error ? (
          <ErrorNote error={error} onRetry={reload} />
        ) : rollup ? (
          <Rollup rollup={rollup} />
        ) : (
          <Empty>Nothing sent in this window yet.</Empty>
        )}
      </Card>

      <Card title="Why there is no open or click column">
        <div className="space-y-3 text-sm text-slate-600">
          <p>
            An open is a tracking pixel. It is a spam signal in its own right, and it is
            blocked by default in Apple Mail and proxied by Gmail &mdash; so the number is
            both harmful to collect and wrong once collected.
          </p>
          <p>
            A click is a rewritten link. It puts a different domain in the anchor of a
            message whose entire argument is that a real person looked at your website.
          </p>
          <p>
            Titan sends neither, so it reports neither. What it reports instead is the
            thing those two are a proxy for: <Badge>replies</Badge> and{' '}
            <Badge>replies that went somewhere</Badge>.
          </p>
        </div>
      </Card>
    </div>
  );
}
