'use client';

/**
 * The Titan-OS CRM.
 *
 * Every figure on this page comes from /api/v1. Nothing is sampled, seeded, or
 * padded — if the backend is unreachable the page says so rather than showing
 * plausible numbers, which is the failure mode the pre-0.2 dashboard had.
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  api,
  login,
  type Campaign,
  type Draft,
  type Finding,
  type Lead,
  type Score,
  type SendingPreflight,
  type Workspace,
} from '@/lib/titan';

const OWNER_EMAIL = 'arslan@arslanvuzmallone.dev';
const WORKSPACE_SLUG = 'titan';

interface OrgLike {
  id: string;
  name: string;
  domain: string | null;
  rating: number | null;
  reviews: number | null;
}

export default function CrmPage() {
  const [token, setToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [preflight, setPreflight] = useState<SendingPreflight | null>(null);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [selected, setSelected] = useState<Lead | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [scores, setScores] = useState<Score[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const t = await login(OWNER_EMAIL, WORKSPACE_SLUG);
      setToken(t);
      const [ws, pf, cs, ls, ds] = await Promise.all([
        api.workspace(t),
        api.preflight(),
        api.campaigns(t),
        api.leads(t),
        api.drafts(t),
      ]);
      setWorkspace(ws);
      setPreflight(pf);
      setCampaigns(cs.items);
      setLeads(ls.items);
      setDrafts(ds.items);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'unknown error');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openLead = useCallback(
    async (lead: Lead) => {
      if (!token) return;
      setSelected(lead);
      setFindings([]);
      setScores([]);
      try {
        const [f, s] = await Promise.all([
          api.findings(token, lead.id),
          api.scores(token, lead.id),
        ]);
        setFindings(f);
        setScores(s);
      } catch (e) {
        setError(e instanceof Error ? e.message : 'failed to load lead');
      }
    },
    [token],
  );

  if (loading) {
    return <Shell><p className="text-slate-500">Loading from the Titan API…</p></Shell>;
  }

  if (error) {
    return (
      <Shell>
        <div className="rounded border-l-4 border-red-500 bg-red-50 p-4">
          <p className="font-semibold text-red-900">Cannot reach the Titan API</p>
          <p className="mt-1 font-mono text-sm text-red-800">{error}</p>
          <p className="mt-3 text-sm text-red-800">
            This page shows no data rather than sample data. Start the stack with{' '}
            <code className="rounded bg-red-100 px-1">docker compose up -d</code>.
          </p>
          <button
            onClick={() => void load()}
            className="mt-3 rounded bg-red-600 px-3 py-1.5 text-sm font-medium text-white"
          >
            Retry
          </button>
        </div>
      </Shell>
    );
  }

  const qualified = leads.filter((l) => l.latest_score !== null && l.latest_score >= 70);
  const scored = leads.filter((l) => l.latest_score !== null);

  return (
    <Shell>
      {/* The safety posture is the first thing an operator should see. */}
      <section
        className={`mb-6 rounded border-l-4 p-4 ${
          preflight?.would_send
            ? 'border-amber-500 bg-amber-50'
            : 'border-emerald-600 bg-emerald-50'
        }`}
      >
        <div className="flex items-baseline justify-between">
          <h2 className="font-semibold text-slate-900">
            Sending: {preflight?.would_send ? 'ARMED' : 'disabled'}
          </h2>
          <span className="text-xs uppercase tracking-wide text-slate-600">
            mode: {workspace?.operating_mode}
          </span>
        </div>
        {preflight && preflight.blockers.length > 0 && (
          <ul className="mt-2 space-y-1 text-sm text-slate-700">
            {preflight.blockers.map((b) => (
              <li key={b}>• {b}</li>
            ))}
          </ul>
        )}
      </section>

      <section className="mb-8 grid grid-cols-2 gap-4 md:grid-cols-5">
        <Stat label="Campaigns" value={campaigns.length} />
        <Stat label="Leads" value={leads.length} />
        <Stat label="Scored" value={scored.length} />
        <Stat label="Qualified (≥70)" value={qualified.length} />
        <Stat label="Drafts" value={drafts.length} />
      </section>

      <section className="mb-8">
        <SectionTitle>Campaigns</SectionTitle>
        {campaigns.length === 0 ? (
          <Empty>No campaigns yet.</Empty>
        ) : (
          <div className="overflow-x-auto rounded border border-slate-200">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-left text-slate-600">
                <tr>
                  <Th>Name</Th>
                  <Th>Industry</Th>
                  <Th>Status</Th>
                  <Th>Leads</Th>
                </tr>
              </thead>
              <tbody>
                {campaigns.map((c) => (
                  <tr key={c.id} className="border-t border-slate-100">
                    <Td className="font-medium text-slate-900">{c.name}</Td>
                    <Td>{c.industry}</Td>
                    <Td>{c.status}</Td>
                    <Td>{leads.filter((l) => l.campaign_id === c.id).length}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="mb-8">
        <SectionTitle>Leads</SectionTitle>
        {leads.length === 0 ? (
          <Empty>
            No leads. Run{' '}
            <code className="rounded bg-slate-100 px-1">
              python -m titan.seed --query &quot;dentists in Manchester UK&quot;
            </code>
          </Empty>
        ) : (
          <div className="overflow-x-auto rounded border border-slate-200">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-left text-slate-600">
                <tr>
                  <Th>Lead</Th>
                  <Th>Status</Th>
                  <Th>Score</Th>
                  <Th>Contacted</Th>
                  <Th>Replied</Th>
                  <Th />
                </tr>
              </thead>
              <tbody>
                {leads.map((l) => (
                  <tr
                    key={l.id}
                    className={`border-t border-slate-100 ${
                      selected?.id === l.id ? 'bg-slate-50' : ''
                    }`}
                  >
                    <Td className="font-mono text-xs text-slate-500">
                      {l.organization_id.slice(0, 8)}
                    </Td>
                    <Td>{l.status}</Td>
                    <Td>
                      {l.latest_score === null ? (
                        <span className="text-slate-400">not scored</span>
                      ) : (
                        <span className="font-semibold">{l.latest_score}</span>
                      )}
                    </Td>
                    <Td>{l.last_contacted_at ? 'yes' : '—'}</Td>
                    <Td>{l.replied_at ? 'yes' : '—'}</Td>
                    <Td>
                      <button
                        onClick={() => void openLead(l)}
                        className="text-sm font-medium text-blue-700 hover:underline"
                      >
                        Open
                      </button>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {selected && (
        <section className="mb-8 rounded border border-slate-200 p-4">
          <SectionTitle>Lead workspace</SectionTitle>
          <p className="mb-3 font-mono text-xs text-slate-500">{selected.id}</p>

          <h4 className="mt-4 font-semibold text-slate-800">Score</h4>
          {scores.length === 0 ? (
            <Empty>
              Not scored yet. Scoring runs as part of research; this lead has only
              been discovered.
            </Empty>
          ) : (
            <div className="mt-2 text-sm">
              <p>
                <span className="text-2xl font-bold">{scores[0].total}</span>{' '}
                <span className="text-slate-600">
                  ({scores[0].band}, threshold {scores[0].threshold_applied})
                </span>
              </p>
              <ul className="mt-2 space-y-1 text-slate-700">
                {scores[0].reasons.map((r) => (
                  <li key={r}>• {r}</li>
                ))}
              </ul>
            </div>
          )}

          <h4 className="mt-5 font-semibold text-slate-800">
            Findings ({findings.length})
          </h4>
          {findings.length === 0 ? (
            <Empty>
              No findings. Titan only records what it measured — a lead that has
              not been crawled has none, and that is the correct display.
            </Empty>
          ) : (
            <ul className="mt-2 space-y-3">
              {findings.map((f) => (
                <li key={f.id} className="rounded border border-slate-200 p-3 text-sm">
                  <div className="flex items-baseline justify-between">
                    <span className="font-medium text-slate-900">{f.title}</span>
                    <span className="text-xs uppercase text-slate-500">
                      {f.severity} · {(f.confidence * 100).toFixed(0)}%
                    </span>
                  </div>
                  {f.observed_value && (
                    <p className="mt-1 font-mono text-xs text-slate-600">
                      observed: {f.observed_value}
                    </p>
                  )}
                  {f.business_impact && (
                    <p className="mt-1 text-slate-700">{f.business_impact}</p>
                  )}
                  <p className="mt-1 text-xs text-slate-500">
                    verified by {f.verification_method}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      <section>
        <SectionTitle>Drafts</SectionTitle>
        {drafts.length === 0 ? (
          <Empty>
            No drafts. A draft is only generated once a lead has evidence-backed
            findings and an eligible contact.
          </Empty>
        ) : (
          <ul className="space-y-3">
            {drafts.map((d) => (
              <li key={d.id} className="rounded border border-slate-200 p-4">
                <div className="flex items-baseline justify-between">
                  <span className="font-medium text-slate-900">{d.subject}</span>
                  <span
                    className={`text-xs font-semibold ${
                      d.validation_passed ? 'text-emerald-700' : 'text-red-700'
                    }`}
                  >
                    {d.validation_passed ? 'validated' : 'validation failed'}
                  </span>
                </div>
                <pre className="mt-2 whitespace-pre-wrap font-sans text-sm text-slate-700">
                  {d.body_text}
                </pre>
                {d.claim_map.length > 0 && (
                  <div className="mt-3 border-t border-slate-100 pt-2">
                    <p className="text-xs font-semibold uppercase text-slate-500">
                      Claim → evidence
                    </p>
                    {d.claim_map.map((c, i) => (
                      <p key={i} className="mt-1 text-xs text-slate-600">
                        “{c.sentence.slice(0, 90)}…” → finding{' '}
                        <code>{c.finding_id.slice(0, 8)}</code> ·{' '}
                        {c.evidence_ids.length} evidence row(s)
                      </p>
                    ))}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto max-w-6xl p-6">
      <header className="mb-6">
        <h1 className="text-2xl font-bold text-slate-900">Titan-OS</h1>
        <p className="text-sm text-slate-600">
          Live data from <code>/api/v1</code>. Nothing on this page is sampled.
        </p>
      </header>
      {children}
    </main>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded border border-slate-200 p-3">
      <p className="text-2xl font-bold text-slate-900">{value}</p>
      <p className="text-xs uppercase tracking-wide text-slate-500">{label}</p>
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h3 className="mb-2 text-lg font-semibold text-slate-900">{children}</h3>;
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
      {children}
    </p>
  );
}

function Th({ children }: { children?: React.ReactNode }) {
  return <th className="px-3 py-2 font-medium">{children}</th>;
}

function Td({ children, className = '' }: { children?: React.ReactNode; className?: string }) {
  return <td className={`px-3 py-2 ${className}`}>{children}</td>;
}
