'use client';

/**
 * Mailboxes and warm-up.
 *
 * Every sending mailbox in one table: which carrier it sends through, whether
 * it is allowed to send cold mail at all, where it is on its warm-up ramp and
 * how many warm-up messages it sends today, what it sent cold this week, and
 * where its last inbox tests landed. Then the outside accounts warm-up trades
 * mail with, and the test inboxes the inbox tests are read from.
 *
 * "Inbox tests" are the placement probes: one email from each mailbox to a
 * test inbox we own, read an hour later to see whether it landed in the inbox
 * or in spam. That, not a delivery receipt, is what says mail is getting
 * through.
 */

import React from 'react';
import { Badge, Card, Empty, ErrorNote, Spinner, Stat, Table, Time } from '@/components/crm/ui';
import { useLiveApi } from '@/lib/session';
import { api, type DashboardMailbox } from '@/lib/coldops';

function carrierName(host: string | null): string {
  if (!host) return '—';
  if (host.includes('gmail')) return 'Google Workspace';
  if (host.includes('spacemail')) return 'Spacemail';
  return host;
}

function landingBadge(box: DashboardMailbox) {
  if (box.inbox_tests_7d === 0) return <Badge tone="neutral">not tested</Badge>;
  const rate = box.inbox_landed_7d / box.inbox_tests_7d;
  const tone = rate >= 0.8 ? 'good' : rate >= 0.5 ? 'warn' : 'bad';
  return (
    <Badge tone={tone}>
      {box.inbox_landed_7d}/{box.inbox_tests_7d} in inbox
    </Badge>
  );
}

function FolderBadge({ folder }: { folder: string | null }) {
  if (!folder) return <Badge tone="neutral">waiting</Badge>;
  if (folder === 'inbox') return <Badge tone="good">inbox</Badge>;
  if (folder === 'spam') return <Badge tone="bad">spam</Badge>;
  return <Badge tone="warn">{folder}</Badge>;
}

export default function MailboxesPage() {
  const { data, error, loading, reload } = useLiveApi((t) => api.dashboardMailboxes(t), 60_000);

  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (loading && !data) return <Spinner />;
  if (!data) return null;

  const live = data.mailboxes.filter((m) => m.enabled);
  const senders = data.mailboxes.filter((m) => m.sender_active).length;
  const tested = live.reduce((n, m) => n + m.inbox_tests_7d, 0);
  const landed = live.reduce((n, m) => n + m.inbox_landed_7d, 0);
  const warmupToday = live.reduce((n, m) => n + m.warmup_today, 0);
  const hours = data.warmup_hours_utc;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Mailboxes & warm-up</h1>
        <p className="mt-1 text-sm text-slate-500">
          Each mailbox, its warm-up, and where its test emails land. Cold mail only goes out from a
          mailbox that is switched on to send and whose test emails reach the inbox.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Mailboxes working" value={`${live.length}`} hint="connected and switched on" />
        <Stat
          label="Allowed to send cold mail"
          value={`${senders}`}
          tone={senders === 0 ? 'warn' : 'good'}
          hint={senders === 0 ? 'all paused while warming up' : 'sender switched on'}
        />
        <Stat
          label="Inbox tests, last 7 days"
          value={tested ? `${landed}/${tested}` : '—'}
          tone={tested === 0 ? 'neutral' : landed / tested >= 0.8 ? 'good' : 'warn'}
          hint="landed in the inbox"
        />
        <Stat
          label="Warm-up emails today"
          value={data.warmup_enabled ? `${warmupToday}` : 'off'}
          tone={data.warmup_enabled ? 'good' : 'warn'}
          hint={
            hours.length ? `spread ${hours[0]}:00–${hours[hours.length - 1] + 1}:00 UTC` : undefined
          }
        />
      </div>

      <Card title="Sending mailboxes">
        {data.mailboxes.length === 0 ? (
          <Empty>No mailboxes are configured.</Empty>
        ) : (
          <Table
            head={['Mailbox', 'Carrier', 'Cold mail', 'Warm-up', 'Cold sent (7d)', 'Inbox tests (7d)', 'Latest tests']}
          >
            {data.mailboxes.map((box) => (
              <tr key={box.address} className={box.enabled ? '' : 'opacity-50'}>
                <td className="px-3 py-2 font-medium text-slate-900">
                  {box.address}
                  {!box.enabled ? <span className="ml-2 text-xs text-slate-500">(switched off)</span> : null}
                </td>
                <td className="px-3 py-2 text-slate-600">{carrierName(box.carrier)}</td>
                <td className="px-3 py-2">
                  {box.sender_active ? <Badge tone="good">allowed</Badge> : <Badge tone="warn">paused</Badge>}
                </td>
                <td className="px-3 py-2 tabular-nums text-slate-700">
                  day {box.warmup_day} · {box.warmup_today}/day
                </td>
                <td className="px-3 py-2 tabular-nums text-slate-700">{box.cold_sent_7d}</td>
                <td className="px-3 py-2">{landingBadge(box)}</td>
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-1">
                    {box.latest_tests.length === 0 ? (
                      <span className="text-xs text-slate-400">none</span>
                    ) : (
                      box.latest_tests.map((t) => (
                        <span key={t.sent_at + t.provider} title={`${t.provider}, sent ${new Date(t.sent_at).toLocaleString()}`}>
                          <FolderBadge folder={t.folder} />
                        </span>
                      ))
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card
          title="Warm-up partners"
          subtitle="Outside accounts that receive, open, star, reply and rescue from spam"
        >
          {data.partners.length === 0 ? (
            <Empty>No partners: warm-up cannot reach Gmail without at least one.</Empty>
          ) : (
            <ul className="space-y-1 text-sm text-slate-800">
              {data.partners.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Test inboxes" subtitle="Where the inbox tests are read; never used to send">
          {data.seeds.length === 0 ? (
            <Empty>No test inboxes: nothing measures where mail lands.</Empty>
          ) : (
            <ul className="space-y-1 text-sm text-slate-800">
              {data.seeds.map((s) => (
                <li key={s}>{s}</li>
              ))}
            </ul>
          )}
        </Card>
      </div>
      <p className="text-xs text-slate-500">
        Updated every minute. Times are shown in your browser&apos;s time zone; <Time value={new Date().toISOString()} /> now.
      </p>
    </div>
  );
}
