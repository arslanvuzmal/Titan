'use client';

/**
 * Activity: everything that happened, newest first.
 *
 * Read from the event stream -- one row for every outcome the system records,
 * projected every fifteen minutes from the tables that own the facts. So this
 * is what the system did, not what it planned: a crawl that finished, a draft
 * written, an email sent or bounced, a reply classified, a call logged.
 *
 * Filter by kind; click a business to open its lead page and full timeline.
 */

import React, { useEffect, useState } from 'react';
import { Badge, Button, Card, Empty, ErrorNote, LeadLink, Spinner, Time } from '@/components/crm/ui';
import { useSession } from '@/lib/session';
import { api, type ActivityEvent } from '@/lib/coldops';

/** What each kind of event is called, in words an owner would use. */
const KINDS: Record<string, { label: string; tone: 'good' | 'warn' | 'bad' | 'info' | 'neutral' | 'strong' }> = {
  'lead.discovered': { label: 'found on Maps', tone: 'neutral' },
  'research.finished': { label: 'website checked', tone: 'info' },
  'lead.scored': { label: 'scored', tone: 'neutral' },
  'draft.created': { label: 'email drafted', tone: 'info' },
  'draft.decided': { label: 'draft decided', tone: 'info' },
  'message.sent': { label: 'email sent', tone: 'strong' },
  'message.delivered': { label: 'delivered', tone: 'good' },
  'message.bounced': { label: 'bounced', tone: 'bad' },
  'message.complained': { label: 'spam complaint', tone: 'bad' },
  'seen.confirmed': { label: 'seen by a person', tone: 'good' },
  'seen.likely': { label: 'probably seen', tone: 'good' },
  'seen.delivered': { label: 'reached the mailbox', tone: 'neutral' },
  'seen.machine': { label: 'opened by a scanner', tone: 'neutral' },
  'reply.received': { label: 'reply arrived', tone: 'strong' },
  'reply.classified': { label: 'reply read', tone: 'info' },
  'call.logged': { label: 'call logged', tone: 'strong' },
  'meeting.recorded': { label: 'meeting', tone: 'good' },
  'suppression.added': { label: 'do-not-contact', tone: 'warn' },
  'manager.decided': { label: 'manager decision', tone: 'neutral' },
  'placement.sent': { label: 'inbox test sent', tone: 'neutral' },
  'placement.checked': { label: 'inbox test read', tone: 'info' },
};

const FILTERS: { value: string; label: string }[] = [
  { value: '', label: 'Everything' },
  { value: 'message.', label: 'Emails' },
  { value: 'reply.', label: 'Replies' },
  { value: 'seen.', label: 'Seen' },
  { value: 'call.', label: 'Calls' },
  { value: 'draft.', label: 'Drafts' },
  { value: 'research.', label: 'Research' },
  { value: 'lead.', label: 'Leads' },
  { value: 'placement.', label: 'Inbox tests' },
  { value: 'manager.', label: 'Manager' },
];

/** One short line of detail from the payload, where there is something worth saying. */
function detail(event: ActivityEvent): string | null {
  const p = event.payload as Record<string, unknown>;
  const parts: string[] = [];
  if (typeof p.to_domain === 'string') parts.push(p.to_domain);
  if (typeof p.reply_class === 'string') parts.push(p.reply_class.replace(/_/g, ' '));
  if (typeof p.outcome === 'string') parts.push(p.outcome.replace(/_/g, ' '));
  if (typeof p.decision === 'string') parts.push(p.decision);
  if (typeof p.status === 'string' && event.kind === 'research.finished') parts.push(p.status);
  if (typeof p.findings_count === 'number') parts.push(`${p.findings_count} findings`);
  if (typeof p.total === 'number') parts.push(`score ${p.total}`);
  if (typeof p.folder === 'string') parts.push(p.folder);
  if (typeof p.provider === 'string' && event.kind.startsWith('placement.')) parts.push(p.provider);
  if (typeof p.bounce_kind === 'string') parts.push(`${p.bounce_kind} bounce`);
  if (typeof p.reason === 'string' && event.kind === 'manager.decided') parts.push(p.reason);
  return parts.length ? parts.join(' · ') : null;
}

export default function ActivityPage() {
  const { token } = useSession();
  const [filter, setFilter] = useState('');
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [exhausted, setExhausted] = useState(false);

  // First page for the current filter, refreshed every minute.
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    const load = () =>
      api
        .activity(token, filter || undefined)
        .then((rows) => {
          if (cancelled) return;
          setEvents(rows);
          setExhausted(rows.length < 100);
          setError(null);
        })
        .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
        .finally(() => !cancelled && setLoading(false));
    setLoading(true);
    void load();
    const timer = setInterval(load, 60_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [token, filter]);

  const loadOlder = () => {
    if (!token || events.length === 0) return;
    const oldest = events[events.length - 1].occurred_at;
    api
      .activity(token, filter || undefined, oldest)
      .then((rows) => {
        setEvents((current) => [...current, ...rows]);
        setExhausted(rows.length < 100);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  };

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Activity</h1>
        <p className="mt-1 text-sm text-slate-500">
          Everything the system did, newest first: research, emails, replies, calls and inbox
          tests. Updated every minute; the stream itself catches up every fifteen.
        </p>
      </div>

      <div className="flex flex-wrap gap-2" role="group" aria-label="Filter activity">
        {FILTERS.map((f) => (
          <button
            key={f.value}
            type="button"
            onClick={() => setFilter(f.value)}
            aria-pressed={filter === f.value}
            className={`rounded-full px-3 py-1 text-sm ring-1 transition ${
              filter === f.value
                ? 'bg-slate-900 text-white ring-slate-900'
                : 'bg-white text-slate-700 ring-slate-200 hover:bg-slate-50'
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      <Card>
        {error ? (
          <ErrorNote error={error} />
        ) : loading && events.length === 0 ? (
          <Spinner />
        ) : events.length === 0 ? (
          <Empty>Nothing of this kind has happened yet.</Empty>
        ) : (
          <ol className="divide-y divide-slate-100">
            {events.map((event) => {
              const kind = KINDS[event.kind] ?? { label: event.kind, tone: 'neutral' as const };
              const extra = detail(event);
              return (
                <li key={event.id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-2">
                  <span className="w-36 shrink-0 text-xs tabular-nums text-slate-500">
                    <Time value={event.occurred_at} />
                  </span>
                  <Badge tone={kind.tone}>{kind.label}</Badge>
                  <span className="min-w-0 text-sm text-slate-800">
                    {event.lead_id ? (
                      <LeadLink id={event.lead_id}>{event.business_name ?? 'a business'}</LeadLink>
                    ) : (
                      <span className="text-slate-500">system</span>
                    )}
                    {extra ? <span className="text-slate-500"> · {extra}</span> : null}
                  </span>
                </li>
              );
            })}
          </ol>
        )}
        {!exhausted && events.length > 0 ? (
          <div className="mt-3 border-t border-slate-100 pt-3">
            <Button variant="secondary" onClick={loadOlder}>
              Load older
            </Button>
          </div>
        ) : null}
      </Card>
    </div>
  );
}
