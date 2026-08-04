'use client';

/**
 * The approval queue.
 *
 * This is the human gate. It exists so that no message leaves Titan without a
 * named person having read the claim, seen the evidence behind it, and said
 * yes to that exact version.
 *
 * Two details are load-bearing:
 *
 * * The decision carries the version the reviewer actually saw. If the draft
 *   was regenerated in the meantime the API returns 409 rather than applying
 *   an approval to text nobody read.
 * * A draft that failed validation shows its violations and offers no approve
 *   button. The API refuses it too; the UI simply does not pretend otherwise.
 */

import Link from 'next/link';
import React, { useState } from 'react';
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  ExternalLink,
  Spinner,
} from '@/components/crm/ui';
import { useApi, useSession } from '@/lib/session';
import { api, type Draft } from '@/lib/titan';

function DraftCard({ draft, onDecided }: { draft: Draft; onDecided: () => void }) {
  const { token, can } = useSession();
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const decide = async (decision: 'approved' | 'rejected' | 'changes_requested') => {
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      await api.decide(token, draft.id, decision, draft.version, reason.trim() || undefined);
      onDecided();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'decision failed');
    } finally {
      setBusy(false);
    }
  };

  const violations = draft.validation_report?.violations ?? [];
  const canDecide = can('approval:decide');

  return (
    <Card
      title={draft.subject}
      subtitle={`v${draft.version} · created ${draft.created_at.slice(0, 16).replace('T', ' ')}Z`}
      action={
        <div className="flex items-center gap-2">
          <Badge>{draft.status}</Badge>
          <Badge tone={draft.validation_passed ? 'good' : 'bad'}>
            {draft.validation_passed ? 'validated' : 'validation failed'}
          </Badge>
        </div>
      }
    >
      <div className="grid gap-4 lg:grid-cols-2">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">Message</p>
          <pre className="mt-1 whitespace-pre-wrap rounded-lg bg-slate-50 px-3 py-2 font-sans text-sm text-slate-800">
            {draft.body_text}
          </pre>
          <p className="mt-2 text-xs text-slate-500">
            {draft.validation_report?.word_count ?? '—'} words ·{' '}
            <Link
              href={`/crm/leads/${draft.lead_id}`}
              className="text-indigo-600 hover:underline"
            >
              open the lead
            </Link>
          </p>
        </div>

        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Every factual sentence, and what backs it
          </p>
          {draft.claim_map.length === 0 ? (
            <p className="mt-1 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">
              This draft makes no traceable factual claim about the recipient.
            </p>
          ) : (
            <ul className="mt-1 space-y-1.5">
              {draft.claim_map.map((entry, i) => (
                <li key={i} className="rounded-lg border border-slate-200 px-3 py-2">
                  <p className="text-sm text-slate-800">“{entry.sentence}”</p>
                  <p className="mt-1 text-xs text-slate-500">
                    claim: {entry.claim} · finding{' '}
                    <code className="font-mono">{entry.finding_id.slice(0, 8)}</code> ·{' '}
                    {entry.evidence_ids.length} evidence row
                    {entry.evidence_ids.length === 1 ? '' : 's'}
                    {entry.source_url && (
                      <>
                        {' · '}
                        <ExternalLink href={entry.source_url}>source</ExternalLink>
                      </>
                    )}
                  </p>
                </li>
              ))}
            </ul>
          )}

          {violations.length > 0 && (
            <div className="mt-3">
              <p className="text-xs font-medium uppercase tracking-wide text-rose-600">
                Blocking violations
              </p>
              <ul className="mt-1 space-y-1">
                {violations.map((v, i) => (
                  <li key={i} className="rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-800">
                    <code className="font-mono font-medium">{v.code}</code> — {v.detail}
                    {v.excerpt && <p className="mt-1 italic">“{v.excerpt}”</p>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>

      {error && (
        <p className="mt-3 rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-800">{error}</p>
      )}

      {canDecide ? (
        <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-4">
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Reason (recorded with the decision)"
            className="min-w-[16rem] flex-1 rounded-lg border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-slate-500"
          />
          <Button
            variant="primary"
            disabled={busy || !draft.validation_passed}
            onClick={() => decide('approved')}
            title={
              draft.validation_passed
                ? 'Approve this exact version'
                : 'A draft that failed validation cannot be approved'
            }
          >
            Approve v{draft.version}
          </Button>
          <Button variant="secondary" disabled={busy} onClick={() => decide('changes_requested')}>
            Request changes
          </Button>
          <Button variant="danger" disabled={busy} onClick={() => decide('rejected')}>
            Reject
          </Button>
        </div>
      ) : (
        <p className="mt-4 border-t border-slate-100 pt-4 text-xs text-slate-500">
          Your role cannot decide on drafts. Reviewing is read-only here.
        </p>
      )}
    </Card>
  );
}

export default function ApprovalsPage() {
  const { data, error, loading, reload } = useApi(
    (t) => api.drafts(t, 'awaiting_approval'),
    [],
  );

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Approvals</h1>
        <p className="mt-1 text-sm text-slate-500">
          Nothing is sent without a decision here. Approving records the version
          you read; if the draft changes before you decide, the API refuses the
          stale approval.
        </p>
      </div>

      {error ? (
        <ErrorNote error={error} onRetry={reload} />
      ) : loading && !data ? (
        <Spinner />
      ) : !data || data.items.length === 0 ? (
        <Card>
          <Empty>Nothing is awaiting approval.</Empty>
        </Card>
      ) : (
        <div className="space-y-4">
          <p className="text-sm text-slate-600">
            {data.total} draft{data.total === 1 ? '' : 's'} awaiting a decision.
          </p>
          {data.items.map((draft) => (
            <DraftCard key={draft.id} draft={draft} onDecided={reload} />
          ))}
        </div>
      )}
    </div>
  );
}
