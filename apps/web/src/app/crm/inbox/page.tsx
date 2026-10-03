'use client';

/**
 * Inbox health.
 *
 * The answer to "why is nothing going out?" in one place. Since 3 October a
 * mailbox sends cold mail only while its own test emails are reaching the
 * inbox, so a quiet day is usually the gate doing its job -- and a paused
 * estate that does not say why it is paused looks exactly like a broken one.
 *
 * Three parts, in the order they are needed:
 *
 * 1. each mailbox, sending or paused, with the gate's own sentence for why;
 * 2. what the gate needs to see anything at all: test inboxes and warm-up
 *    partners, as a checklist;
 * 3. where the test emails actually landed, per mailbox and provider. A
 *    provider nobody measures is named, never left off the table: an empty
 *    row and a clean row look the same.
 */

import Link from 'next/link';
import React from 'react';
import { Badge, Card, Empty, ErrorNote, Spinner, Table, Time } from '@/components/crm/ui';
import { useApi } from '@/lib/session';
import { api, type MailboxStatus } from '@/lib/titan';

const WHY: Record<string, string> = {
  placement_unmeasured: 'paused: no recent inbox reading',
  placement_below_floor: 'paused: test emails landing outside the inbox',
  placement_domain_resting: 'paused: domain resting',
};

function pct(value: number | null): string {
  return value === null ? 'not measured' : `${Math.round(value * 100)}%`;
}

function MailboxRow({ mailbox }: { mailbox: MailboxStatus }) {
  return (
    <tr>
      <td className="px-3 py-2 font-medium text-slate-900">{mailbox.from_email}</td>
      <td className="px-3 py-2">
        {mailbox.sending ? (
          <Badge tone="good">sending</Badge>
        ) : (
          <Badge tone="warn">{WHY[mailbox.code ?? ''] ?? 'paused'}</Badge>
        )}
      </td>
      <td className="px-3 py-2 tabular-nums text-slate-700">
        {pct(mailbox.reach)}
        {mailbox.measured ? (
          <span className="text-xs text-slate-500"> of {mailbox.measured}</span>
        ) : null}
      </td>
      <td className="px-3 py-2 text-xs text-slate-600">
        {mailbox.detail}
        {mailbox.rest_until ? (
          <>
            {' '}
            · until <Time value={mailbox.rest_until} />
          </>
        ) : null}
      </td>
    </tr>
  );
}

function Check({ done, children }: { done: boolean; children: React.ReactNode }) {
  return (
    <li className="flex gap-2 text-sm">
      <span
        aria-hidden
        className={`mt-0.5 inline-block h-4 w-4 flex-none rounded-full ${done ? 'bg-emerald-500' : 'border-2 border-slate-300'}`}
      />
      <span className={done ? 'text-slate-500' : 'text-slate-800'}>{children}</span>
    </li>
  );
}

export default function InboxHealthPage() {
  const health = useApi((t) => api.health(t), []);
  const placement = useApi((t) => api.placement(t, 14), []);

  if (health.loading && !health.data) return <Spinner label="Checking the mailboxes" />;
  if (health.error) return <ErrorNote error={health.error} onRetry={health.reload} />;
  const h = health.data;
  if (!h) return null;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Inbox health</h1>
        <p className="mt-1 text-sm text-slate-500">
          A mailbox sends cold mail only while its own test emails are reaching the
          inbox. This page says, for each one, whether it is sending and why not.
        </p>
      </div>

      <div
        className={`rounded-xl border px-4 py-3 text-sm ${h.cold_mail_moving ? 'border-emerald-200 bg-emerald-50 text-emerald-900' : 'border-amber-200 bg-amber-50 text-amber-900'}`}
      >
        {h.cold_mail}
      </div>

      <Card title="Mailboxes" subtitle="The inbox gate's verdict, read live">
        {h.mailboxes.length === 0 ? (
          <Empty>No active sending mailbox.</Empty>
        ) : (
          <Table head={['Mailbox', 'Status', 'Inbox rate (48h)', 'Why']}>
            {h.mailboxes.map((m) => (
              <MailboxRow key={m.from_email} mailbox={m} />
            ))}
          </Table>
        )}
        {!h.gate_enabled ? (
          <p className="mt-3 text-xs text-amber-700">
            The gate is switched off: mail goes out without checking where it lands.
          </p>
        ) : null}
      </Card>

      <Card title="What the gate needs" subtitle="Free accounts; steps in docs/cold-domains.md, Part A">
        <ul className="space-y-2">
          <Check done={h.seeds_configured >= 3}>
            Test inboxes: 2 Gmail and 1 Yahoo, each with an app password ({h.seeds_configured}{' '}
            set up). Not Outlook: Microsoft refuses password logins, so Outlook is checked by
            hand.
          </Check>
          <Check done={h.warmup_partners >= 3}>
            Warm-up partners: 3 to 5 separate Gmail and Yahoo accounts ({h.warmup_partners} set
            up). Without them the daily warm-up skips.
          </Check>
          <Check done={h.warmup_enabled}>Daily warm-up switched on.</Check>
          <Check done={h.message_form === 'brief'}>
            Short first emails: one link, no attachment.
          </Check>
        </ul>
      </Card>

      <Card title="Where test emails landed" subtitle="Last 14 days, per mailbox and provider">
        {placement.error ? (
          <ErrorNote error={placement.error} onRetry={placement.reload} />
        ) : !placement.data ? (
          <Spinner />
        ) : placement.data.never_measured ? (
          <Empty>
            No test email has ever been read. Set up the test inboxes above and the
            first reading arrives the next morning.
          </Empty>
        ) : (
          <Table head={['Mailbox', 'Provider', 'Inbox', 'Promotions', 'Spam', 'Not read yet', 'Verdict']}>
            {placement.data.by_mailbox.map((row) => (
              <tr key={`${row.from_email}-${row.provider}`}>
                <td className="px-3 py-2">{row.from_email}</td>
                <td className="px-3 py-2 text-slate-600">{row.provider}</td>
                <td className="px-3 py-2 tabular-nums">{row.inbox}</td>
                <td className="px-3 py-2 tabular-nums">{row.promotions}</td>
                <td className="px-3 py-2 tabular-nums">{row.spam}</td>
                <td className="px-3 py-2 tabular-nums text-slate-500">{row.unchecked}</td>
                <td className="px-3 py-2">
                  <Badge
                    tone={
                      row.verdict === 'landing' ? 'good' : row.verdict === 'patchy' ? 'warn' : row.verdict === 'not measured' ? 'neutral' : 'bad'
                    }
                  >
                    {row.verdict}
                  </Badge>
                </td>
              </tr>
            ))}
          </Table>
        )}
        {placement.data?.unmeasured_note ? (
          <p className="mt-3 text-xs text-slate-500">
            Not covered by any test inbox: {placement.data.unmeasured_note.replace('no seed covers: ', '')}
          </p>
        ) : null}
      </Card>

      <Card title="Seen this week" subtitle="Every open and visit, graded by what it proves">
        <div className="flex flex-wrap gap-2">
          {['confirmed', 'likely', 'delivered', 'machine'].map((grade) => (
            <Badge
              key={grade}
              tone={grade === 'confirmed' ? 'good' : grade === 'likely' ? 'info' : 'neutral'}
            >
              {grade}: {h.seen_7d[grade] ?? 0}
            </Badge>
          ))}
        </div>
        {h.recent_seen.length === 0 ? (
          <p className="mt-3 text-sm text-slate-500">
            Nobody has opened or visited anything yet this week.
          </p>
        ) : (
          <ul className="mt-3 space-y-1.5 text-sm">
            {h.recent_seen.map((s, i) => (
              <li key={i} className="flex flex-wrap items-center gap-2">
                <Time value={s.occurred_at} />
                <Badge tone={s.grade === 'confirmed' ? 'good' : 'info'}>{s.grade}</Badge>
                {s.lead_id ? (
                  <Link href={`/crm/leads/${s.lead_id}`} className="font-medium text-slate-900 hover:text-indigo-600">
                    {s.business_name ?? 'a lead'}
                  </Link>
                ) : (
                  <span>{s.business_name ?? 'unknown'}</span>
                )}
                <span className="text-xs text-slate-500">{s.reason}</span>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-3 text-xs text-slate-500">
          &ldquo;Delivered&rdquo; is Apple&rsquo;s privacy proxy loading every image on arrival;
          &ldquo;machine&rdquo; is a scanner or a script. Neither counts as somebody reading.
        </p>
      </Card>
    </div>
  );
}
