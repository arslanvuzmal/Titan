'use client';

/**
 * Delivery log.
 *
 * State comes from provider webhooks, collapsed so a duplicate delivery
 * notification cannot advance a message twice and a late "sent" cannot
 * overwrite a recorded bounce. What is shown here is what the provider
 * actually reported, not what Titan hoped would happen.
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

export default function MessagesPage() {
  const { data, error, loading, reload } = useApi((t) => api.messages(t, 200), []);
  const stats = useApi((t) => api.stats(t), []);

  const byState = stats.data?.messages_by_state ?? {};
  const delivered = byState.delivered ?? 0;
  const bounced = byState.bounced ?? 0;
  const complained = byState.complained ?? 0;
  const totalSent = Object.values(byState).reduce((sum, n) => sum + n, 0);

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Delivery</h1>
        <p className="mt-1 text-sm text-slate-500">
          Provider-reported outcomes. A bounce or complaint suppresses the
          address automatically; nothing here needs manual cleanup to stay
          compliant.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Messages" value={totalSent} />
        <Stat label="Delivered" value={delivered} tone={delivered > 0 ? 'good' : 'neutral'} />
        <Stat
          label="Bounced"
          value={bounced}
          tone={bounced > 0 ? 'bad' : 'neutral'}
          hint={totalSent ? `${((bounced / totalSent) * 100).toFixed(2)}% of all messages` : undefined}
        />
        <Stat
          label="Complaints"
          value={complained}
          tone={complained > 0 ? 'bad' : 'neutral'}
          hint="delivery pauses above 0.1%"
        />
      </div>

      {error ? (
        <ErrorNote error={error} onRetry={reload} />
      ) : loading && !data ? (
        <Spinner />
      ) : !data || data.items.length === 0 ? (
        <Card>
          <Empty>
            Nothing has been queued or sent. Delivery requires an approved draft
            and both sending switches on.
          </Empty>
        </Card>
      ) : (
        <Card>
          <Table
            head={['Subject', 'Recipient', 'State', 'Sent', 'Delivered', 'Bounced', 'Complaint', 'Lead']}
          >
            {data.items.map((message) => (
              <tr key={message.id} className="hover:bg-slate-50">
                <td className="px-3 py-2">{message.subject}</td>
                <td className="px-3 py-2 font-mono text-xs">{message.to_email_normalized}</td>
                <td className="px-3 py-2">
                  <Badge>{message.state}</Badge>
                </td>
                <td className="px-3 py-2 text-xs">
                  <Time value={message.sent_at} />
                </td>
                <td className="px-3 py-2 text-xs">
                  <Time value={message.delivered_at} />
                </td>
                <td className="px-3 py-2 text-xs">
                  <Time value={message.bounced_at} />
                </td>
                <td className="px-3 py-2 text-xs">
                  <Time value={message.complained_at} />
                </td>
                <td className="px-3 py-2 text-xs">
                  <LeadLink id={message.lead_id}>open</LeadLink>
                </td>
              </tr>
            ))}
          </Table>
        </Card>
      )}
    </div>
  );
}
