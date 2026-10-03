'use client';

/**
 * The reply desk.
 *
 * Every person who wrote back, what they said in their own words, and the
 * answer Titan suggested. Nothing here sends on its own: the operator reads
 * the reply, edits the answer, and presses send. The answer goes out in the
 * same thread as their message, from the mailbox they wrote to.
 *
 * Two details carried over from the approval queue:
 *
 * * Every save and send names the version the operator was looking at. If the
 *   answer changed in between, the API refuses and this page reloads it rather
 *   than overwriting somebody else's words.
 * * A suggestion with a blank in it (the drafter always leaves the price as
 *   "[TODO: ...]") cannot be sent. The API refuses it; the page says so before
 *   anyone tries.
 */

import React, { useState } from 'react';
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  LeadLink,
  Spinner,
  Time,
} from '@/components/crm/ui';
import { useApi, useSession } from '@/lib/session';
import { api, type DeskReply } from '@/lib/titan';

/** How each reading of a reply is labelled, in words an owner would use. */
const READING: Record<string, { label: string; tone: 'good' | 'warn' | 'bad' | 'info' | 'neutral' }> = {
  interested: { label: 'interested', tone: 'good' },
  wants_call: { label: 'wants a call', tone: 'good' },
  wants_more_info: { label: 'wants more information', tone: 'info' },
  wants_pricing: { label: 'asked about price', tone: 'info' },
  referral: { label: 'pointed to someone else', tone: 'info' },
  not_now: { label: 'not now', tone: 'warn' },
  objection: { label: 'raised an objection', tone: 'warn' },
  wrong_person: { label: 'wrong person', tone: 'warn' },
  unknown: { label: 'unclear', tone: 'neutral' },
};

const BLANK = '[TODO:';

function ReplyCard({ reply, onDone }: { reply: DeskReply; onDone: () => void }) {
  const { token, can } = useSession();
  const [subject, setSubject] = useState(reply.subject);
  const [body, setBody] = useState(reply.body);
  const [version, setVersion] = useState(reply.draft_version);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);

  const edited = subject !== reply.subject || body !== reply.body;
  const hasBlank = body.includes(BLANK) || subject.includes(BLANK);
  const canAct = can('approval:decide');
  const reading = READING[reply.reply_class] ?? { label: reply.reply_class, tone: 'neutral' as const };

  const run = async (step: () => Promise<void>) => {
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      await step();
    } catch (e) {
      const message = e instanceof Error ? e.message : 'something went wrong';
      setError(message);
      // Somebody else changed it: show them what is there now.
      if (message.includes('changed since you opened it')) onDone();
    } finally {
      setBusy(false);
    }
  };

  const save = () =>
    run(async () => {
      if (!token) return;
      const saved = await api.editReply(token, reply.draft_id, version, subject, body);
      setVersion(saved.draft_version);
    });

  const send = () =>
    run(async () => {
      if (!token) return;
      let current = version;
      // Unsaved changes go first, so what is sent is what is on screen.
      if (edited || !reply.ready_to_send) {
        const saved = await api.editReply(token, reply.draft_id, current, subject, body);
        current = saved.draft_version;
        setVersion(current);
      }
      await api.sendReply(token, reply.draft_id, current);
      setSent(true);
      onDone();
    });

  return (
    <Card
      title={reply.business_name ?? reply.from_email}
      subtitle={reply.from_email}
      action={
        <div className="flex items-center gap-2">
          <Badge tone={reading.tone}>{reading.label}</Badge>
          <span className="text-xs text-slate-500">
            <Time value={reply.received_at} />
          </span>
        </div>
      }
    >
      <div className="grid gap-4 lg:grid-cols-2">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
            What they wrote
          </p>
          {reply.their_subject ? (
            <p className="mt-1 text-sm font-medium text-slate-800">{reply.their_subject}</p>
          ) : null}
          <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-50 px-3 py-2 font-sans text-sm text-slate-800">
            {reply.their_words || '(no text in the message)'}
          </pre>
          <p className="mt-2 text-xs text-slate-500">
            Read as &ldquo;{reading.label}&rdquo; ({Math.round(reply.confidence * 100)}% sure) ·{' '}
            <LeadLink id={reply.lead_id}>open the lead</LeadLink>
          </p>
        </div>

        <div>
          <label
            htmlFor={`subject-${reply.draft_id}`}
            className="text-xs font-medium uppercase tracking-wide text-slate-500"
          >
            Your answer
          </label>
          <input
            id={`subject-${reply.draft_id}`}
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            disabled={!canAct || sent}
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-slate-500"
          />
          <textarea
            id={`body-${reply.draft_id}`}
            aria-label="Answer"
            value={body}
            onChange={(e) => setBody(e.target.value)}
            disabled={!canAct || sent}
            rows={10}
            className="mt-2 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm leading-relaxed outline-none focus:border-slate-500"
          />
          {hasBlank ? (
            <p className="mt-1 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
              Fill in the part marked <code className="font-mono">[TODO: …]</code> before
              sending. Titan never writes a price or a date for you.
            </p>
          ) : (
            <p className="mt-1 text-xs text-slate-500">
              Sent in the same thread as their message. Your name and postal address are
              added if they are missing.
            </p>
          )}
        </div>
      </div>

      {error ? (
        <p className="mt-3 rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-800">{error}</p>
      ) : null}

      {sent ? (
        <p className="mt-4 border-t border-slate-100 pt-4 text-sm text-emerald-700">
          Queued. It goes out with the next send, inside their working hours.
        </p>
      ) : canAct ? (
        <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-4">
          <Button
            variant="primary"
            disabled={busy || hasBlank || !body.trim()}
            onClick={send}
            title={hasBlank ? 'Fill in the [TODO: …] part first' : 'Send this answer'}
          >
            {busy ? 'Working…' : 'Send'}
          </Button>
          <Button variant="secondary" disabled={busy || !edited} onClick={save}>
            Save without sending
          </Button>
          {edited ? (
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() => {
                setSubject(reply.subject);
                setBody(reply.body);
              }}
            >
              Undo my changes
            </Button>
          ) : null}
        </div>
      ) : (
        <p className="mt-4 border-t border-slate-100 pt-4 text-xs text-slate-500">
          Your role can read replies but not send answers.
        </p>
      )}
    </Card>
  );
}

export default function RepliesPage() {
  const { data, error, loading, reload } = useApi((t) => api.replies(t), []);

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Replies</h1>
        <p className="mt-1 text-sm text-slate-500">
          People who wrote back, in their own words, with a suggested answer. Nothing is
          sent until you press Send.
        </p>
      </div>

      {error ? (
        <ErrorNote error={error} onRetry={reload} />
      ) : loading && !data ? (
        <Spinner />
      ) : !data || data.length === 0 ? (
        <Card>
          <Empty>No replies are waiting for an answer.</Empty>
        </Card>
      ) : (
        <div className="space-y-4">
          <p className="text-sm text-slate-600">
            {data.length} repl{data.length === 1 ? 'y' : 'ies'} waiting for an answer.
          </p>
          {data.map((reply) => (
            <ReplyCard key={`${reply.draft_id}-${reply.draft_version}`} reply={reply} onDone={reload} />
          ))}
        </div>
      )}
    </div>
  );
}
