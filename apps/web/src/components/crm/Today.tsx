'use client';

/**
 * How much has gone out today, refreshing itself.
 *
 * This section exists because of a specific morning. On 31 August the estate
 * sat at nine sends against a ceiling of twenty-six; nothing in the CRM said
 * so, and the shortfall was found by hand in psql after the day was half gone.
 * Every other view here answers a question about the whole history. This one
 * answers the question actually asked first thing in the morning and again at
 * four: *is it sending?*
 *
 * **The ceiling is not a target.** A message also needs an open window in the
 * recipient's timezone and a queue to draw from, so falling short is a question
 * rather than a fault. Which is why the deferral reasons sit directly beneath
 * the number instead of on a separate screen -- "9 of 26" prompts exactly one
 * question, and the answer must not be a click away.
 *
 * **Nothing here is a rate that could be mistaken for a verdict.** Today's
 * bounce count is shown; today's bounce *rate* only once enough has been sent
 * for the fraction to mean anything. One bounce in three sends is not a 33%
 * bounce rate, and a mailbox is never paused on today's numbers -- the
 * thirty-day window on the performance page governs that.
 */

import Link from 'next/link';
import React from 'react';
import { Badge, Card, ErrorNote, Spinner } from '@/components/crm/ui';
import { useLiveApi } from '@/lib/session';
import { api, type Deferral, type MailboxDay, type Today } from '@/lib/titan';

/**
 * Half a minute. The outbox worker's own cadence is the floor worth matching --
 * polling faster produces the same number more often, and slower is long enough
 * for an operator watching a run start to wonder whether the page is broken.
 */
const REFRESH_MS = 30_000;

/**
 * Below this, today's bounce rate is a fraction of a small number rather than a
 * measurement. Deliberately far under the 50 the reputation window uses: this
 * is a same-day tripwire, not the figure anything is throttled on.
 */
const RATE_FLOOR = 20;

function ago(then: Date, now: number): string {
  const seconds = Math.max(0, Math.round((now - then.getTime()) / 1000));
  if (seconds < 45) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.round(minutes / 60)}h ago`;
}

function clock(iso: string | null): string | null {
  if (!iso) return null;
  const at = new Date(iso);
  return Number.isNaN(at.getTime())
    ? null
    : at.toLocaleString(undefined, {
        weekday: 'short',
        hour: '2-digit',
        minute: '2-digit',
      });
}

/** A bar that reads honestly when the denominator is zero. */
function Bar({ value, of, tone }: { value: number; of: number; tone: string }) {
  const pct = of > 0 ? Math.min(100, (value / of) * 100) : 0;
  return (
    <div className="h-1.5 overflow-hidden rounded-full bg-slate-100">
      <div className={`h-full rounded-full ${tone}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

/**
 * The day's shape, hour by hour in UTC.
 *
 * A run that stopped at 10:00 and one still going at 17:00 can carry the same
 * total, and only one of them is a problem. The total alone cannot tell them
 * apart; twenty-four bars can, in the space of a line.
 */
function Hours({ hourly }: { hourly: number[] }) {
  const peak = Math.max(1, ...hourly);
  const nowHour = new Date().getUTCHours();
  return (
    <div>
      <div className="flex h-10 items-end gap-[2px]">
        {hourly.map((count, hour) => (
          <div
            key={hour}
            className="group relative flex-1"
            title={`${String(hour).padStart(2, '0')}:00 UTC — ${count} sent`}
          >
            <div
              className={`w-full rounded-sm ${
                count > 0
                  ? 'bg-slate-800'
                  : hour <= nowHour
                    ? 'bg-slate-200'
                    : 'bg-slate-100'
              }`}
              // A minimum height so an hour that sent one message is visible
              // rather than rounding away into the axis.
              style={{ height: count > 0 ? `${Math.max(12, (count / peak) * 100)}%` : '2px' }}
            />
          </div>
        ))}
      </div>
      <div className="mt-1 flex justify-between text-[10px] text-slate-400">
        <span>00:00 UTC</span>
        <span>12:00</span>
        <span>23:00</span>
      </div>
    </div>
  );
}

function Mailbox({ box }: { box: MailboxDay }) {
  const stale = box.health_as_of !== null && box.health_as_of < todayUtc();
  return (
    <li className="py-2.5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="font-mono text-xs text-slate-900">{box.from_email}</span>
        <span className="flex items-center gap-2">
          {box.warmup_day !== null && (
            <span className="text-[11px] text-slate-500">
              warm-up day {box.warmup_day} of {box.warmup_days}
            </span>
          )}
          {/* No tone override: the health word carries its own colour from the
              shared vocabulary, so `blocked` reads the same here as it does on
              any other screen.

              `recovering` is the one substitution, and it replaces a word that
              was actively misleading. A mailbox on probation is BLOCKED by the
              classifier and sending five a day at the same time; showing only
              the first read as "stopped" and sent the operator looking for an
              outage that was not there. The verdict itself is unchanged --
              `title` still carries it, and softening the classifier would have
              changed the allowance rather than the wording. */}
          <Badge
            title={
              box.probation
                ? `classifier says ${box.health}; sending on the probation allowance`
                : undefined
            }
          >
            {box.probation ? 'recovering' : box.health}
          </Badge>
          <span className="text-sm font-semibold tabular-nums text-slate-900">
            {box.sent}
            <span className="font-normal text-slate-400"> / {box.allowed}</span>
          </span>
        </span>
      </div>
      <div className="mt-1.5">
        <Bar
          value={box.sent}
          of={box.allowed}
          tone={box.allowed === 0 ? 'bg-rose-300' : 'bg-slate-800'}
        />
      </div>
      <p className="mt-1 text-[11px] text-slate-500">
        {box.note}
        {box.queued > 0 && ` · ${box.queued} queued`}
        {stale && ` · health last measured ${box.health_as_of}`}
      </p>
    </li>
  );
}

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

function Deferrals({ deferrals }: { deferrals: Deferral[] }) {
  if (deferrals.length === 0) {
    return (
      <p className="text-xs text-slate-500">
        Nothing is waiting. Every approved message has either gone or been resolved.
      </p>
    );
  }
  return (
    <ul className="space-y-1.5">
      {deferrals.map((item) => (
        <li key={item.reason} className="flex items-start gap-2.5 text-xs">
          <span className="mt-px min-w-[2.25rem] rounded bg-slate-100 px-1.5 py-0.5 text-center font-semibold tabular-nums text-slate-700">
            {item.count}
          </span>
          <span className="min-w-0 flex-1">
            <span className="break-words text-slate-700">{item.reason}</span>
            {clock(item.next_attempt_at) && (
              <span className="text-slate-400"> · retries {clock(item.next_attempt_at)}</span>
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function TodaySection() {
  const { data, error, loading, fetchedAt, stale, reload } = useLiveApi<Today>(
    (t) => api.today(t),
    REFRESH_MS,
  );

  // Re-rendered on a second timer purely so "12s ago" keeps counting between
  // polls. Without it the label freezes at whatever it said when the last
  // response landed, which is precisely the impression this panel must avoid.
  const [tick, setTick] = React.useState(() => Date.now());
  React.useEffect(() => {
    const timer = window.setInterval(() => setTick(Date.now()), 5_000);
    return () => window.clearInterval(timer);
  }, []);

  if (loading) {
    return (
      <Card title="Today">
        <Spinner label="Reading today's sending" />
      </Card>
    );
  }
  if (!data) {
    return (
      <Card title="Today">
        <ErrorNote error={error ?? 'no data'} onRetry={reload} />
      </Card>
    );
  }

  const rate =
    data.bounce_rate !== null && data.sent >= RATE_FLOOR
      ? `${(data.bounce_rate * 100).toFixed(1)}%`
      : null;
  const sending = data.mailboxes.filter((m) => m.allowed > 0).length;

  return (
    <Card
      title="Today"
      subtitle={`Sending on ${data.window_date} (UTC). Refreshes every ${
        REFRESH_MS / 1000
      } seconds.`}
      action={
        <button
          type="button"
          onClick={reload}
          className="shrink-0 rounded-full px-2.5 py-1 text-xs text-slate-500 transition hover:bg-slate-100 hover:text-slate-800"
          title="Refresh now"
        >
          <span
            className={`mr-1.5 inline-block h-1.5 w-1.5 rounded-full align-middle ${
              stale ? 'bg-amber-500' : 'bg-emerald-500'
            }`}
          />
          {fetchedAt ? ago(fetchedAt, tick) : 'updating'}
        </button>
      }
    >
      {stale && (
        <p className="mb-3 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900">
          These numbers could not be refreshed ({error}). They are the last that arrived,
          not the current ones.
        </p>
      )}

      {/*
        The sentence before the number, not after it. A bare "0" at seven in
        the morning and a bare "0" at six in the evening are opposite
        situations, and rendering them identically is the failure this whole
        panel exists to prevent -- reproduced inside the panel itself. The
        operator opened it before any window had opened and read a fault,
        every day, because nothing said otherwise.
      */}
      {data.state && (
        <p className="mb-3 text-sm font-medium text-slate-700">{data.state}</p>
      )}

      <div className="flex flex-wrap items-end gap-x-8 gap-y-3">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Sent today
          </p>
          <p className="mt-0.5 flex items-baseline gap-1.5">
            <span className="text-4xl font-semibold tabular-nums text-slate-900">
              {data.sent}
            </span>
            <span className="text-lg font-normal tabular-nums text-slate-400">
              / {data.ceiling}
            </span>
          </p>
        </div>
        <dl className="flex flex-wrap gap-x-7 gap-y-2 text-sm">
          {/*
            Yesterday, always. It is what makes an empty morning legible: a
            screen of nothing but zeroes reads as a broken system whatever the
            hour, and until the first window opens every figure here is
            honestly nought.
          */}
          {data.previous_date && (
            <div>
              <dt className="text-xs uppercase tracking-wide text-slate-500">
                Yesterday
              </dt>
              <dd className="font-semibold tabular-nums text-slate-900">
                {data.previous_sent}
                {data.previous_bounced > 0 && (
                  <span className="ml-1 font-normal text-slate-500">
                    ({data.previous_bounced} bounced)
                  </span>
                )}
              </dd>
            </div>
          )}
          <div>
            <dt className="text-xs uppercase tracking-wide text-slate-500">Room left</dt>
            <dd className="font-semibold tabular-nums text-slate-900">{data.remaining}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-slate-500">Bounced</dt>
            <dd
              className={`font-semibold tabular-nums ${
                data.bounced > 0 ? 'text-rose-600' : 'text-slate-900'
              }`}
            >
              {data.bounced}
              {rate && <span className="ml-1 text-xs font-normal text-slate-500">{rate}</span>}
            </dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-slate-500">Failed</dt>
            <dd
              className={`font-semibold tabular-nums ${
                data.failed > 0 ? 'text-rose-600' : 'text-slate-900'
              }`}
            >
              {data.failed}
            </dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-slate-500">Waiting</dt>
            <dd className="font-semibold tabular-nums text-slate-900">{data.queued}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-slate-500">Mailboxes</dt>
            <dd className="font-semibold tabular-nums text-slate-900">
              {sending}
              <span className="font-normal text-slate-400"> / {data.mailboxes.length}</span>
            </dd>
          </div>
        </dl>
      </div>

      <div className="mt-3">
        <Bar value={data.sent} of={data.ceiling} tone="bg-slate-800" />
      </div>

      <p className="mt-2 text-xs text-slate-500">
        The ceiling is what the send gate would permit today, not a target &mdash; a
        message also needs an open window in the recipient&apos;s timezone and something
        approved to send.{' '}
        <Link href="/crm/performance" className="underline hover:text-slate-800">
          Thirty-day outcomes
        </Link>{' '}
        are what any decision should rest on.
      </p>

      <div className="mt-4 grid gap-5 lg:grid-cols-2">
        <div>
          <h3 className="text-xs font-medium uppercase tracking-wide text-slate-500">
            By mailbox
          </h3>
          <ul className="mt-1 divide-y divide-slate-100">
            {data.mailboxes.map((box) => (
              <Mailbox key={box.sender_identity_id} box={box} />
            ))}
          </ul>
        </div>

        <div>
          <h3 className="text-xs font-medium uppercase tracking-wide text-slate-500">
            {data.queued > 0 ? `Why ${data.queued} are waiting` : 'Nothing waiting'}
          </h3>
          <div className="mt-2">
            <Deferrals deferrals={data.deferrals} />
          </div>

          <h3 className="mt-5 text-xs font-medium uppercase tracking-wide text-slate-500">
            Through the day
          </h3>
          <div className="mt-2">
            <Hours hourly={data.hourly} />
          </div>
        </div>
      </div>
    </Card>
  );
}
