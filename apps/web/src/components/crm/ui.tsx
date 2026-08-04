'use client';

/**
 * CRM primitives.
 *
 * One rule runs through all of these: a component never invents a value to
 * fill a gap. `Value` renders an explicit em-dash for null rather than "0" or
 * "N/A", because a CRM whose blanks look like data is worse than one with
 * visible blanks.
 */

import Link from 'next/link';
import React from 'react';

// --------------------------------------------------------------------------
// Vocabulary -> colour. Kept here so a status means the same thing everywhere.
// --------------------------------------------------------------------------
const TONES: Record<string, string> = {
  neutral: 'bg-slate-100 text-slate-700 ring-slate-200',
  info: 'bg-sky-50 text-sky-700 ring-sky-200',
  good: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  warn: 'bg-amber-50 text-amber-800 ring-amber-200',
  bad: 'bg-rose-50 text-rose-700 ring-rose-200',
  strong: 'bg-indigo-50 text-indigo-700 ring-indigo-200',
};

const STATUS_TONE: Record<string, keyof typeof TONES> = {
  // lead
  discovered: 'neutral',
  researching: 'info',
  researched: 'info',
  qualified: 'good',
  manual_review: 'warn',
  rejected: 'bad',
  drafted: 'info',
  awaiting_approval: 'warn',
  queued: 'info',
  contacted: 'strong',
  replied: 'good',
  meeting_booked: 'good',
  disqualified: 'bad',
  suppressed: 'bad',
  archived: 'neutral',
  // score band
  high_priority: 'good',
  reject: 'bad',
  // severity
  critical: 'bad',
  high: 'bad',
  medium: 'warn',
  low: 'info',
  info: 'neutral',
  // delivery
  sent: 'info',
  delivered: 'good',
  deferred: 'warn',
  opened: 'good',
  clicked: 'good',
  bounced: 'bad',
  complained: 'bad',
  unsubscribed: 'bad',
  failed: 'bad',
  // draft / campaign
  approved: 'good',
  changes_requested: 'warn',
  expired: 'neutral',
  draft: 'neutral',
  active: 'good',
  paused: 'warn',
  completed: 'neutral',
  running: 'info',
};

export function Badge({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone?: keyof typeof TONES;
}) {
  const key = typeof children === 'string' ? children.toLowerCase() : '';
  const resolved = tone ?? STATUS_TONE[key] ?? 'neutral';
  return (
    <span
      className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${TONES[resolved]}`}
    >
      {typeof children === 'string' ? children.replace(/_/g, ' ') : children}
    </span>
  );
}

/** A value that may genuinely be unknown. Never substitutes a placeholder. */
export function Value({
  children,
  mono,
}: {
  children: React.ReactNode;
  mono?: boolean;
}) {
  const empty =
    children === null || children === undefined || children === '' || children === false;
  if (empty) return <span className="text-slate-400" title="not recorded">—</span>;
  return <span className={mono ? 'font-mono text-[13px]' : undefined}>{children}</span>;
}

export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: React.ReactNode;
  hint?: string;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-500" title={hint}>
        {label}
      </dt>
      <dd className="mt-0.5 truncate text-sm text-slate-900">{children}</dd>
    </div>
  );
}

export function Card({
  title,
  subtitle,
  action,
  children,
  className = '',
}: {
  title?: string;
  subtitle?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-xl border border-slate-200 bg-white shadow-sm ${className}`}
    >
      {(title || action) && (
        <header className="flex items-start justify-between gap-4 border-b border-slate-100 px-5 py-3.5">
          <div className="min-w-0">
            {title && <h2 className="text-sm font-semibold text-slate-900">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p>}
          </div>
          {action}
        </header>
      )}
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone = 'neutral',
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
  tone?: keyof typeof TONES;
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p
        className={`mt-1 text-2xl font-semibold tabular-nums ${
          tone === 'bad' ? 'text-rose-600' : tone === 'good' ? 'text-emerald-600' : 'text-slate-900'
        }`}
      >
        {value}
      </p>
      {hint && <p className="mt-0.5 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-8 text-sm text-slate-500">
      <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-slate-300 border-t-slate-600" />
      {label}…
    </div>
  );
}

/**
 * An error is shown verbatim. Paraphrasing an API failure into "something went
 * wrong" costs the operator the one detail that would let them fix it.
 */
export function ErrorNote({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
      <p className="font-medium">Request failed</p>
      <p className="mt-1 font-mono text-xs break-words">{error}</p>
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-2 rounded-md bg-rose-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-rose-700"
        >
          Retry
        </button>
      )}
    </div>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="py-8 text-center text-sm text-slate-500">{children}</p>
  );
}

export function Button({
  children,
  onClick,
  variant = 'secondary',
  disabled,
  type = 'button',
  title,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
  disabled?: boolean;
  type?: 'button' | 'submit';
  title?: string;
}) {
  const styles = {
    primary: 'bg-slate-900 text-white hover:bg-slate-800 disabled:bg-slate-300',
    secondary:
      'border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 disabled:text-slate-400',
    danger: 'bg-rose-600 text-white hover:bg-rose-700 disabled:bg-rose-300',
    ghost: 'text-slate-600 hover:bg-slate-100 disabled:text-slate-300',
  }[variant];
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed ${styles}`}
    >
      {children}
    </button>
  );
}

export function Table({
  head,
  children,
}: {
  head: React.ReactNode[];
  children: React.ReactNode;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-left text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
            {head.map((cell, i) => (
              <th key={i} className="whitespace-nowrap px-3 py-2 font-medium">
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">{children}</tbody>
      </table>
    </div>
  );
}

export function ExternalLink({ href, children }: { href: string; children?: React.ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer nofollow"
      className="text-indigo-600 underline-offset-2 hover:underline"
    >
      {children ?? href}
    </a>
  );
}

export function LeadLink({ id, children }: { id: string; children: React.ReactNode }) {
  return (
    <Link href={`/crm/leads/${id}`} className="font-medium text-slate-900 hover:text-indigo-600">
      {children}
    </Link>
  );
}

/** Absolute UTC, because a relative time is ambiguous in an audit context. */
export function when(value: string | null | undefined): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toISOString().replace('T', ' ').slice(0, 16) + 'Z';
}

export function Time({ value }: { value: string | null | undefined }) {
  const text = when(value);
  return <Value mono>{text}</Value>;
}

export function ScoreBadge({ score }: { score: number | null }) {
  if (score === null) return <Value>{null}</Value>;
  const tone = score >= 85 ? 'good' : score >= 70 ? 'info' : score >= 55 ? 'warn' : 'bad';
  return (
    <span
      className={`inline-flex w-10 justify-center rounded-md px-1.5 py-0.5 text-xs font-semibold tabular-nums ring-1 ring-inset ${TONES[tone]}`}
    >
      {score}
    </span>
  );
}
