'use client';

/**
 * Operations: workflow runs, quota consumption, and model spend.
 *
 * The preflight block is the honest answer to "would a send actually go out
 * right now?" — it lists every blocker rather than reducing them to a single
 * red light, because the fix depends on which one is holding.
 */

import React from 'react';
import {
  Badge,
  Card,
  Empty,
  ErrorNote,
  Spinner,
  Stat,
  Table,
  Time,
  Value,
} from '@/components/crm/ui';
import { useApi } from '@/lib/session';
import { api, type SendingPreflight } from '@/lib/titan';

function Preflight() {
  const [data, setData] = React.useState<SendingPreflight | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    api
      .preflight()
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'failed'));
  }, []);

  if (error) return <ErrorNote error={error} />;
  if (!data) return <Spinner label="Checking send preflight" />;

  return (
    <Card
      title="Send preflight"
      subtitle="What the outbox worker would do if a message were ready right now"
      action={
        <Badge tone={data.would_send ? 'good' : 'warn'}>
          {data.would_send ? 'would send' : 'would not send'}
        </Badge>
      }
    >
      <dl className="grid gap-4 sm:grid-cols-2">
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Process kill switch
          </dt>
          <dd className="mt-1">
            <Badge tone={data.process_sending_enabled ? 'good' : 'warn'}>
              {data.process_sending_enabled ? 'enabled' : 'disabled'}
            </Badge>
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Email provider
          </dt>
          <dd className="mt-1 font-mono text-sm">{data.email_provider}</dd>
        </div>
      </dl>

      {data.blockers.length > 0 && (
        <div className="mt-4">
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">Blockers</p>
          <ul className="mt-1 space-y-1">
            {data.blockers.map((blocker, i) => (
              <li key={i} className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900">
                {blocker}
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="mt-3 text-xs text-slate-500">{data.note}</p>
    </Card>
  );
}

export default function OperationsPage() {
  const workflows = useApi((t) => api.workflows(t, 50), []);
  const usage = useApi((t) => api.usage(t), []);

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Operations</h1>
        <p className="mt-1 text-sm text-slate-500">
          Durable workflow runs, quota consumption, and model spend for the
          current UTC window.
        </p>
      </div>

      <Preflight />

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat
          label="Model calls today"
          value={usage.data?.model_calls ?? '—'}
          hint={usage.data ? `window ${usage.data.window_date}` : undefined}
        />
        <Stat
          label="Model spend today"
          value={usage.data ? `$${usage.data.spend_usd.toFixed(4)}` : '—'}
          hint="ledgered per call, not estimated"
        />
        <Stat label="Workflow runs" value={workflows.data?.total ?? '—'} />
      </div>

      {usage.error ? (
        <ErrorNote error={usage.error} onRetry={usage.reload} />
      ) : usage.data && usage.data.quotas.length > 0 ? (
        <Card title="Quota consumption" subtitle="Reserved atomically before any send">
          <Table head={['Scope', 'Key', 'Used', 'Limit', 'Window']}>
            {usage.data.quotas.map((quota, i) => {
              const row = quota as Record<string, unknown>;
              return (
                <tr key={i}>
                  <td className="px-3 py-2">
                    <Badge>{String(row.scope ?? 'unknown')}</Badge>
                  </td>
                  <td className="px-3 py-2 font-mono text-xs">
                    <Value>{String(row.scope_key ?? '')}</Value>
                  </td>
                  <td className="px-3 py-2 tabular-nums font-medium">
                    {String(row.used ?? row.consumed ?? '—')}
                  </td>
                  <td className="px-3 py-2 tabular-nums text-slate-600">
                    {String(row.limit ?? '—')}
                  </td>
                  <td className="px-3 py-2 text-xs">
                    <Value>{String(row.window_date ?? '')}</Value>
                  </td>
                </tr>
              );
            })}
          </Table>
        </Card>
      ) : (
        <Card title="Quota consumption">
          <Empty>No quota has been consumed in this window.</Empty>
        </Card>
      )}

      {workflows.error ? (
        <ErrorNote error={workflows.error} onRetry={workflows.reload} />
      ) : workflows.loading && !workflows.data ? (
        <Spinner />
      ) : !workflows.data || workflows.data.items.length === 0 ? (
        <Card title="Workflow runs">
          <Empty>No workflow has been started.</Empty>
        </Card>
      ) : (
        <Card title="Workflow runs" subtitle="Durable executions recorded by the Temporal worker">
          <Table head={['Workflow', 'Type', 'Status', 'Started', 'Closed', 'Failure']}>
            {workflows.data.items.map((run) => (
              <tr key={run.id}>
                <td className="px-3 py-2 font-mono text-xs">{run.workflow_id}</td>
                <td className="px-3 py-2 text-xs text-slate-600">{run.workflow_type}</td>
                <td className="px-3 py-2">
                  <Badge>{run.status}</Badge>
                </td>
                <td className="px-3 py-2 text-xs">
                  <Time value={run.started_at} />
                </td>
                <td className="px-3 py-2 text-xs">
                  <Time value={run.closed_at} />
                </td>
                <td className="px-3 py-2 text-xs text-rose-700">
                  <Value>{run.failure_reason}</Value>
                </td>
              </tr>
            ))}
          </Table>
        </Card>
      )}
    </div>
  );
}
