'use client';

/**
 * The pipeline, end to end.
 *
 * Thirteen stages from "discovered" to "meeting". Each number is a count of
 * distinct businesses that reached the stage, read from the table that owns
 * the fact -- a research run that finished, a message that left, a reply a
 * person wrote -- never from a status column somebody has to remember to move.
 *
 * Click a stage to see the businesses behind its number, newest first; each
 * one links to its lead page and full history.
 */

import React, { useState } from 'react';
import { Card, Empty, ErrorNote, LeadLink, ScoreBadge, Spinner, Table, Time } from '@/components/crm/ui';
import { useApi, useLiveApi } from '@/lib/session';
import { api, type PipelineStage } from '@/lib/coldops';

function pct(value: number | null): string {
  if (value === null) return '';
  if (value >= 0.995) return '100%';
  return `${(value * 100).toFixed(value < 0.1 ? 1 : 0)}%`;
}

function StageBar({
  stage,
  max,
  selected,
  onSelect,
}: {
  stage: PipelineStage;
  max: number;
  selected: boolean;
  onSelect: () => void;
}) {
  const width = max > 0 ? Math.max((stage.count / max) * 100, stage.count > 0 ? 1.5 : 0) : 0;
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`grid w-full grid-cols-[9rem_1fr_5.5rem] items-center gap-3 rounded-lg px-3 py-2 text-left transition sm:grid-cols-[11rem_1fr_6rem] ${
        selected ? 'bg-slate-100 ring-1 ring-slate-300' : 'hover:bg-slate-50'
      }`}
      aria-pressed={selected}
    >
      <span className="min-w-0">
        <span className="block truncate text-sm font-medium text-slate-900">{stage.label}</span>
        <span className="block truncate text-xs text-slate-500">{stage.hint}</span>
      </span>
      <span className="h-3 overflow-hidden rounded-full bg-slate-100" aria-hidden="true">
        <span
          className="block h-full rounded-full bg-slate-800"
          style={{ width: `${width}%` }}
        />
      </span>
      <span className="text-right tabular-nums">
        <span className="block text-sm font-semibold text-slate-900">
          {stage.count.toLocaleString()}
        </span>
        <span className="block text-xs text-slate-500">
          {stage.from_previous === null ? 'start' : `${pct(stage.from_previous)} of prev`}
        </span>
      </span>
    </button>
  );
}

function StageLeads({ stage }: { stage: PipelineStage }) {
  const { data, error, loading, reload } = useApi(
    (t) => api.pipelineStage(t, stage.key, 100),
    [stage.key],
  );
  return (
    <Card
      title={`${stage.label}: the businesses`}
      subtitle={`${stage.count.toLocaleString()} reached this stage; showing the most recent ${Math.min(stage.count, 100)}`}
    >
      {error ? (
        <ErrorNote error={error} onRetry={reload} />
      ) : loading && !data ? (
        <Spinner />
      ) : !data || data.length === 0 ? (
        <Empty>No business has reached this stage yet.</Empty>
      ) : (
        <Table head={['Business', 'Website', 'Score', 'Reached']}>
          {data.map((row) => (
            <tr key={row.lead_id}>
              <td className="px-3 py-2 font-medium text-slate-900">
                <LeadLink id={row.lead_id}>{row.business_name ?? 'Unnamed business'}</LeadLink>
              </td>
              <td className="px-3 py-2 text-slate-600">{row.domain ?? '—'}</td>
              <td className="px-3 py-2">
                <ScoreBadge score={row.latest_score} />
              </td>
              <td className="px-3 py-2 text-slate-600">
                <Time value={row.at} />
              </td>
            </tr>
          ))}
        </Table>
      )}
    </Card>
  );
}

export default function PipelinePage() {
  const { data, error, loading, reload } = useLiveApi((t) => api.pipeline(t), 60_000);
  const [selected, setSelected] = useState<string | null>(null);
  const stages = data?.stages ?? [];
  const max = stages.reduce((m, s) => Math.max(m, s.count), 0);
  const chosen = stages.find((s) => s.key === selected) ?? null;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Pipeline</h1>
        <p className="mt-1 text-sm text-slate-500">
          Every business from discovery to a meeting. Each number counts distinct businesses
          that reached the stage. Click a stage to see who.
        </p>
      </div>

      {error ? (
        <ErrorNote error={error} onRetry={reload} />
      ) : loading && !data ? (
        <Spinner />
      ) : (
        <Card
          title="From discovered to meeting"
          subtitle={data ? `Updated ${new Date(data.computed_at).toLocaleTimeString()}` : undefined}
        >
          <div className="space-y-1">
            {stages.map((stage) => (
              <StageBar
                key={stage.key}
                stage={stage}
                max={max}
                selected={stage.key === selected}
                onSelect={() => setSelected(stage.key === selected ? null : stage.key)}
              />
            ))}
          </div>
        </Card>
      )}

      {chosen ? <StageLeads stage={chosen} /> : null}
    </div>
  );
}
