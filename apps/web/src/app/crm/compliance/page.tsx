'use client';

/**
 * Compliance: the do-not-contact list.
 *
 * Suppression is checked twice per send — once when a draft is queued, and
 * again inside the transaction that reserves quota, because a recipient can
 * unsubscribe in the seconds between. Adding an entry here takes effect on the
 * next check, not on the next batch.
 *
 * Suppressing a plus-tagged address also suppresses its base, because it is the
 * same person; that second row is written at suppression time and shows up in
 * this list.
 */

import React, { useState } from 'react';
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  Spinner,
  Stat,
  Table,
  Time,
  Value,
} from '@/components/crm/ui';
import { useApi, useSession } from '@/lib/session';
import { api } from '@/lib/titan';

const REASONS = [
  'manual',
  'unsubscribe',
  'complaint',
  'hard_bounce',
  'repeated_soft_bounce',
  'role_address_policy',
  'legal_request',
  'not_interested',
];

const PERMANENT = new Set(['unsubscribe', 'complaint', 'hard_bounce', 'legal_request']);

function AddSuppression({ onAdded }: { onAdded: () => void }) {
  const { token, can } = useSession();
  const [value, setValue] = useState('');
  const [scope, setScope] = useState('email');
  const [reason, setReason] = useState('manual');
  const [note, setNote] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!can('suppression:write')) {
    return (
      <Card title="Add a suppression">
        <p className="text-sm text-slate-500">
          Your role cannot modify the suppression list.
        </p>
      </Card>
    );
  }

  return (
    <Card
      title="Add a suppression"
      subtitle="Takes effect at the next send check, including for messages already queued"
    >
      <form
        className="grid gap-3 sm:grid-cols-2"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!token) return;
          setBusy(true);
          setError(null);
          try {
            await api.suppress(token, {
              value: value.trim(),
              scope,
              reason,
              note: note.trim() || undefined,
            });
            setValue('');
            setNote('');
            onAdded();
          } catch (err) {
            setError(err instanceof Error ? err.message : 'failed');
          } finally {
            setBusy(false);
          }
        }}
      >
        <label className="block sm:col-span-2">
          <span className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Email address or domain
          </span>
          <input
            required
            minLength={3}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={scope === 'domain' ? 'example.com' : 'someone@example.com'}
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-1.5 font-mono text-sm"
          />
        </label>

        <label className="block">
          <span className="text-xs font-medium uppercase tracking-wide text-slate-500">Scope</span>
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm"
          >
            <option value="email">This address</option>
            <option value="domain">The whole domain</option>
          </select>
        </label>

        <label className="block">
          <span className="text-xs font-medium uppercase tracking-wide text-slate-500">Reason</span>
          <select
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm"
          >
            {REASONS.map((r) => (
              <option key={r} value={r}>
                {r.replace(/_/g, ' ')}
                {PERMANENT.has(r) ? ' (permanent)' : ''}
              </option>
            ))}
          </select>
        </label>

        <label className="block sm:col-span-2">
          <span className="text-xs font-medium uppercase tracking-wide text-slate-500">Note</span>
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm"
          />
        </label>

        {error && (
          <p className="rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-800 sm:col-span-2">
            {error}
          </p>
        )}

        <div className="sm:col-span-2">
          <Button type="submit" variant="primary" disabled={busy}>
            Suppress
          </Button>
        </div>
      </form>
      {PERMANENT.has(reason) && (
        <p className="mt-3 text-xs text-amber-800">
          This reason is permanent and cannot be given an expiry. That is
          deliberate — an unsubscribe does not lapse.
        </p>
      )}
    </Card>
  );
}

export default function CompliancePage() {
  const { data, error, loading, reload } = useApi((t) => api.suppressions(t, 200), []);
  const sources = useApi((t) => api.contactSources(t), []);

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Compliance</h1>
        <p className="mt-1 text-sm text-slate-500">
          The do-not-contact list, and the provenance rules that decide whether
          an address may be contacted at all.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Suppression entries" value={data?.total ?? '—'} />
        <Stat
          label="Eligible provenance"
          value={sources.data?.eligible.length ?? '—'}
          hint="sources an address may come from"
        />
        <Stat
          label="Never eligible"
          value={sources.data?.never_eligible.length ?? '—'}
          hint="excluded regardless of campaign policy"
        />
      </div>

      <AddSuppression onAdded={reload} />

      {sources.data && (
        <Card title="Contact provenance rules" subtitle="Enforced in code, not by convention">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <p className="text-xs font-medium uppercase tracking-wide text-emerald-700">
                May be contacted
              </p>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {sources.data.eligible.map((s) => (
                  <Badge key={s} tone="good">
                    {s}
                  </Badge>
                ))}
              </div>
            </div>
            <div>
              <p className="text-xs font-medium uppercase tracking-wide text-rose-700">
                Never contacted
              </p>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {sources.data.never_eligible.map((s) => (
                  <Badge key={s} tone="bad">
                    {s}
                  </Badge>
                ))}
              </div>
              <p className="mt-1 text-xs text-slate-500">
                A guessed address is stored so Titan remembers not to guess it
                again — never so that it can be used.
              </p>
            </div>
          </div>
        </Card>
      )}

      {error ? (
        <ErrorNote error={error} onRetry={reload} />
      ) : loading && !data ? (
        <Spinner />
      ) : !data || data.items.length === 0 ? (
        <Card>
          <Empty>The suppression list is empty.</Empty>
        </Card>
      ) : (
        <Card title="Suppression list">
          <Table head={['Value', 'Scope', 'Reason', 'Source', 'Suppressed', 'Expires']}>
            {data.items.map((entry) => (
              <tr key={entry.id}>
                <td className="px-3 py-2 font-mono text-xs">{entry.normalized_value}</td>
                <td className="px-3 py-2">
                  <Badge>{entry.scope}</Badge>
                </td>
                <td className="px-3 py-2">
                  <Badge tone={PERMANENT.has(entry.reason) ? 'bad' : 'warn'}>{entry.reason}</Badge>
                </td>
                <td className="px-3 py-2 text-xs text-slate-600">
                  <Value>{entry.source}</Value>
                </td>
                <td className="px-3 py-2 text-xs">
                  <Time value={entry.suppressed_at} />
                </td>
                <td className="px-3 py-2 text-xs">
                  {entry.expires_at ? <Time value={entry.expires_at} /> : <Badge>never</Badge>}
                </td>
              </tr>
            ))}
          </Table>
        </Card>
      )}
    </div>
  );
}
