'use client';

/**
 * Lead workspace.
 *
 * Everything Titan knows about one business, arranged so a reviewer can answer
 * the only question that matters before a message goes out: *is this claim
 * true, and can I see why it is true?*
 *
 * That is why findings expand into their evidence rows rather than summarising
 * them, why a draft is shown next to its claim map, and why contact channels
 * carry their provenance instead of just an address.
 */

import Link from 'next/link';
import { useParams } from 'next/navigation';
import React, { useState } from 'react';
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  ExternalLink,
  Field,
  ScoreBadge,
  Spinner,
  Table,
  Time,
  Value,
} from '@/components/crm/ui';
import { useApi, useSession } from '@/lib/session';
import { api, type Evidence, type Finding, type Score } from '@/lib/titan';

const TABS = [
  'Findings',
  'Contacts',
  'Scoring',
  'Drafts',
  'Delivery',
  'Timeline',
  'Business',
] as const;
type Tab = (typeof TABS)[number];

// --------------------------------------------------------------------------
// Findings, each expandable into the evidence that supports it
// --------------------------------------------------------------------------
function EvidenceList({ findingId }: { findingId: string }) {
  const { data, error, loading } = useApi<Evidence[]>(
    (t) => api.evidence(t, findingId),
    [findingId],
  );
  if (loading) return <Spinner label="Loading evidence" />;
  if (error) return <ErrorNote error={error} />;
  if (!data || data.length === 0) {
    return (
      <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
        No evidence rows. A finding without evidence can never be cited in a
        message — the validator rejects the draft.
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {data.map((row) => (
        <li key={row.id} className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
          <p className="font-mono text-xs text-slate-700">
            <Value>{row.excerpt}</Value>
          </p>
          <p className="mt-1 text-xs text-slate-500">
            {row.source_url ? <ExternalLink href={row.source_url} /> : <Value>{null}</Value>}
            {' · captured '}
            <Time value={row.captured_at} />
          </p>
        </li>
      ))}
    </ul>
  );
}

function FindingsTab({ leadId }: { leadId: string }) {
  const { data, error, loading, reload } = useApi<Finding[]>(
    (t) => api.findings(t, leadId),
    [leadId],
  );
  const [open, setOpen] = useState<string | null>(null);

  if (loading) return <Spinner />;
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data || data.length === 0) {
    return <Empty>No findings recorded. Run research on this lead to produce some.</Empty>;
  }

  return (
    <ul className="space-y-2">
      {data.map((finding) => (
        <li
          key={finding.id}
          className={`rounded-lg border px-4 py-3 ${
            finding.contradicted ? 'border-slate-200 bg-slate-50 opacity-70' : 'border-slate-200'
          }`}
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-sm font-medium text-slate-900">{finding.title}</p>
              <p className="mt-0.5 text-xs text-slate-500">
                {finding.category} · {finding.issue_type} · verified by{' '}
                <code className="font-mono">{finding.verification_method}</code>
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Badge>{finding.severity}</Badge>
              <span className="text-xs tabular-nums text-slate-500">
                {Math.round(finding.confidence * 100)}% confidence
              </span>
              {finding.contradicted && <Badge tone="bad">contradicted</Badge>}
            </div>
          </div>

          <dl className="mt-3 grid gap-3 sm:grid-cols-2">
            <Field label="Observed">
              <Value mono>{finding.observed_value}</Value>
            </Field>
            <Field label="Expected">
              <Value>{finding.expected_behavior}</Value>
            </Field>
            <Field label="Page">
              {finding.page_url ? <ExternalLink href={finding.page_url} /> : <Value>{null}</Value>}
            </Field>
            <Field label="Selector">
              <Value mono>{finding.selector}</Value>
            </Field>
          </dl>

          {finding.recommended_solution && (
            <p className="mt-3 rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-700">
              {finding.recommended_solution}
            </p>
          )}

          <div className="mt-3">
            <Button
              variant="ghost"
              onClick={() => setOpen(open === finding.id ? null : finding.id)}
            >
              {open === finding.id ? 'Hide evidence' : 'Show evidence'}
            </Button>
          </div>
          {open === finding.id && (
            <div className="mt-2">
              <EvidenceList findingId={finding.id} />
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

// --------------------------------------------------------------------------
// Contacts
// --------------------------------------------------------------------------
function ContactsTab({ leadId }: { leadId: string }) {
  const { data, error, loading, reload } = useApi((t) => api.contacts(t, leadId), [leadId]);

  if (loading) return <Spinner />;
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data || data.length === 0) return <Empty>No contacts discovered for this business.</Empty>;

  return (
    <div className="space-y-4">
      {data.map((contact) => (
        <div key={contact.id} className="rounded-lg border border-slate-200 px-4 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-medium text-slate-900">
              <Value>{contact.full_name}</Value>
            </p>
            <span className="text-xs text-slate-500">
              <Value>{contact.role_title}</Value>
            </span>
            {contact.is_decision_maker && <Badge tone="strong">decision maker</Badge>}
            {contact.is_generic_role && <Badge>role address</Badge>}
          </div>

          <div className="mt-3 space-y-2">
            {contact.channels.map((channel) => (
              <div
                key={channel.id}
                className={`rounded-lg border px-3 py-2 ${
                  channel.eligible_for_outreach
                    ? 'border-emerald-200 bg-emerald-50/40'
                    : 'border-slate-200 bg-slate-50'
                }`}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <code className="text-sm text-slate-900">{channel.value}</code>
                  <div className="flex items-center gap-2">
                    <Badge>{channel.source}</Badge>
                    <Badge>{channel.verification_status}</Badge>
                    {channel.suppressed && <Badge tone="bad">suppressed</Badge>}
                    <Badge tone={channel.eligible_for_outreach ? 'good' : 'warn'}>
                      {channel.eligible_for_outreach ? 'contactable' : 'not contactable'}
                    </Badge>
                  </div>
                </div>
                {channel.ineligibility_reason && (
                  <p className="mt-1 text-xs text-amber-800">{channel.ineligibility_reason}</p>
                )}
                <p className="mt-1 text-xs text-slate-500">
                  found <Time value={channel.discovered_at} />
                  {channel.source_url && (
                    <>
                      {' at '}
                      <ExternalLink href={channel.source_url} />
                    </>
                  )}
                  {' · confidence '}
                  {Math.round(channel.confidence * 100)}%
                </p>
              </div>
            ))}
          </div>
        </div>
      ))}
      <p className="text-xs text-slate-500">
        Addresses Titan guessed from a pattern are shown so you can see what was
        found, and are never contactable — no evidence links a guessed address
        to a real mailbox.
      </p>
    </div>
  );
}

// --------------------------------------------------------------------------
// Scoring
// --------------------------------------------------------------------------
function ScoringTab({ leadId }: { leadId: string }) {
  const { data, error, loading, reload } = useApi<Score[]>(
    (t) => api.scores(t, leadId),
    [leadId],
  );
  if (loading) return <Spinner />;
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data || data.length === 0) return <Empty>This lead has not been scored.</Empty>;

  return (
    <div className="space-y-5">
      {data.map((score, index) => (
        <div key={`${score.created_at}-${index}`} className="rounded-lg border border-slate-200 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <ScoreBadge score={score.total} />
              <Badge>{score.band}</Badge>
              <Badge tone={score.passed_threshold ? 'good' : 'warn'}>
                {score.passed_threshold
                  ? `passed threshold ${score.threshold_applied}`
                  : `below threshold ${score.threshold_applied}`}
              </Badge>
            </div>
            <p className="text-xs text-slate-500">
              policy <code className="font-mono">{score.policy_version}</code> ·{' '}
              <Time value={score.created_at} />
            </p>
          </div>

          <div className="mt-4">
            <Table head={['Dimension', 'Raw', 'Weight', 'Weighted', 'Reason']}>
              {Object.entries(score.components).map(([name, component]) => (
                <tr key={name}>
                  <td className="px-3 py-1.5 font-medium text-slate-700">
                    {name.replace(/_/g, ' ')}
                  </td>
                  <td className="px-3 py-1.5 tabular-nums">{component.raw.toFixed(2)}</td>
                  <td className="px-3 py-1.5 tabular-nums text-slate-500">{component.weight}</td>
                  <td className="px-3 py-1.5 tabular-nums font-medium">
                    {component.weighted.toFixed(1)}
                  </td>
                  <td className="px-3 py-1.5 text-xs text-slate-600">{component.reason}</td>
                </tr>
              ))}
            </Table>
          </div>

          {score.reasons.length > 0 && (
            <ul className="mt-3 list-inside list-disc text-xs text-slate-600">
              {score.reasons.map((reason, i) => (
                <li key={i}>{reason}</li>
              ))}
            </ul>
          )}
        </div>
      ))}
      <p className="text-xs text-slate-500">
        Scores are append-only. The score that justified a send stays
        reconstructable even after the scoring policy changes.
      </p>
    </div>
  );
}

// --------------------------------------------------------------------------
// Drafts
// --------------------------------------------------------------------------
function DraftsTab({ leadId }: { leadId: string }) {
  const { data, error, loading, reload } = useApi((t) => api.leadDrafts(t, leadId), [leadId]);
  if (loading) return <Spinner />;
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data || data.length === 0) return <Empty>No draft has been generated for this lead.</Empty>;

  return (
    <div className="space-y-4">
      {data.map((draft) => (
        <div key={draft.id} className="rounded-lg border border-slate-200 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="text-sm font-medium text-slate-900">{draft.subject}</p>
              <p className="mt-0.5 text-xs text-slate-500">
                v{draft.version} · <Time value={draft.created_at} />
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Badge>{draft.status}</Badge>
              <Badge tone={draft.validation_passed ? 'good' : 'bad'}>
                {draft.validation_passed ? 'validation passed' : 'validation failed'}
              </Badge>
            </div>
          </div>

          <pre className="mt-3 whitespace-pre-wrap rounded-lg bg-slate-50 px-3 py-2 font-sans text-sm text-slate-800">
            {draft.body_text}
          </pre>

          {draft.claim_map.length > 0 && (
            <div className="mt-3">
              <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
                Claim map
              </p>
              <ul className="mt-1 space-y-1">
                {draft.claim_map.map((entry, i) => (
                  <li key={i} className="rounded-lg border border-slate-200 px-3 py-2 text-xs">
                    <p className="text-slate-800">“{entry.sentence}”</p>
                    <p className="mt-1 text-slate-500">
                      finding <code className="font-mono">{entry.finding_id.slice(0, 8)}</code> ·{' '}
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
            </div>
          )}

          {draft.validation_report?.violations?.length > 0 && (
            <ul className="mt-3 space-y-1">
              {draft.validation_report.violations.map((v, i) => (
                <li key={i} className="rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-800">
                  <code className="font-mono font-medium">{v.code}</code> — {v.detail}
                  {v.excerpt && <p className="mt-1 italic">“{v.excerpt}”</p>}
                </li>
              ))}
            </ul>
          )}

          {draft.status === 'awaiting_approval' && (
            <p className="mt-3 text-xs text-slate-500">
              Review and decide on this draft from the{' '}
              <Link href="/crm/approvals" className="text-indigo-600 hover:underline">
                approvals queue
              </Link>
              .
            </p>
          )}
        </div>
      ))}
    </div>
  );
}

// --------------------------------------------------------------------------
// Delivery
// --------------------------------------------------------------------------
function DeliveryTab({ leadId }: { leadId: string }) {
  const { data, error, loading, reload } = useApi((t) => api.leadMessages(t, leadId), [leadId]);
  if (loading) return <Spinner />;
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data || data.length === 0) return <Empty>Nothing has been sent to this lead.</Empty>;

  return (
    <Table head={['Subject', 'Recipient', 'State', 'Sent', 'Delivered', 'Bounced', 'Complaint']}>
      {data.map((message) => (
        <tr key={message.id}>
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
        </tr>
      ))}
    </Table>
  );
}

// --------------------------------------------------------------------------
// Timeline
// --------------------------------------------------------------------------
function TimelineTab({ leadId }: { leadId: string }) {
  const { data, error, loading, reload } = useApi((t) => api.timeline(t, leadId), [leadId]);
  if (loading) return <Spinner />;
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data || data.length === 0) return <Empty>No recorded activity.</Empty>;

  return (
    <ol className="relative space-y-4 border-l border-slate-200 pl-5">
      {data.map((event, i) => (
        <li key={`${event.at}-${i}`} className="relative">
          <span
            className={`absolute -left-[23px] top-1.5 h-2.5 w-2.5 rounded-full ring-2 ring-white ${
              event.severity === 'high' ? 'bg-rose-500' : 'bg-slate-400'
            }`}
          />
          <p className="text-sm font-medium text-slate-900">{event.title}</p>
          <p className="text-xs text-slate-500">
            <code className="font-mono">{event.kind}</code> · <Time value={event.at} />
          </p>
          {event.detail && <p className="mt-0.5 text-xs text-slate-600">{event.detail}</p>}
        </li>
      ))}
    </ol>
  );
}

// --------------------------------------------------------------------------
// Business record
// --------------------------------------------------------------------------
function BusinessTab({ organizationId }: { organizationId: string }) {
  const { data, error, loading, reload } = useApi(
    (t) => api.organization(t, organizationId),
    [organizationId],
  );
  if (loading) return <Spinner />;
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data) return null;

  return (
    <div className="space-y-4">
      <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Field label="Display name">{data.display_name}</Field>
        <Field label="Legal name">
          <Value>{data.legal_name}</Value>
        </Field>
        <Field label="Industry">
          <Badge>{data.industry}</Badge>
        </Field>
        <Field label="Website">
          {data.website_url ? <ExternalLink href={data.website_url} /> : <Value>{null}</Value>}
        </Field>
        <Field label="Canonical domain">
          <Value mono>{data.canonical_domain}</Value>
        </Field>
        <Field label="Phone">
          <Value mono>{data.phone_e164}</Value>
        </Field>
        <Field label="Rating">
          <Value>
            {data.rating ? `${data.rating} from ${data.review_count ?? 0} reviews` : null}
          </Value>
        </Field>
        <Field label="Business status">
          <Value>{data.business_status}</Value>
        </Field>
        <Field label="Google Place ID" hint="The only Places field Titan may cache indefinitely">
          <Value mono>{data.google_place_id}</Value>
        </Field>
      </dl>

      {data.locations.length > 0 && (
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">Locations</p>
          <ul className="mt-1 space-y-1">
            {data.locations.map((location) => (
              <li key={location.id} className="rounded-lg border border-slate-200 px-3 py-2 text-sm">
                <Value>{location.formatted_address}</Value>
                <span className="ml-2 text-xs text-slate-500">
                  {location.timezone && `timezone ${location.timezone}`}
                  {location.is_primary && ' · primary'}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {data.domains.length > 0 && (
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">Domains</p>
          <p className="mt-1 font-mono text-sm text-slate-700">{data.domains.join(', ')}</p>
        </div>
      )}

      <div>
        <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
          Provenance
        </p>
        <p className="mt-0.5 text-xs text-slate-500">
          Append-only. Re-discovering this business through another source adds a
          record rather than overwriting the identity.
        </p>
        {data.provenance.length === 0 ? (
          <p className="mt-1 text-sm text-slate-500">No provenance recorded.</p>
        ) : (
          <pre className="mt-1 overflow-x-auto rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-700">
            {JSON.stringify(data.provenance, null, 2)}
          </pre>
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
export default function LeadDetailPage() {
  const params = useParams<{ leadId: string }>();
  const leadId = params.leadId;
  const { can } = useSession();
  const { data: lead, error, loading, reload } = useApi((t) => api.lead(t, leadId), [leadId]);
  const [tab, setTab] = useState<Tab>('Findings');
  const [research, setResearch] = useState<string | null>(null);
  const { token } = useSession();

  if (loading && !lead) return <Spinner label="Loading lead" />;
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!lead) return null;

  const org = lead.organization;

  return (
    <div className="space-y-5">
      <div>
        <Link href="/crm/leads" className="text-xs text-slate-500 hover:text-slate-900">
          ← All leads
        </Link>
        <div className="mt-1 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-slate-900">
              {org?.display_name ?? 'Unnamed organization'}
            </h1>
            <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-slate-500">
              {org?.website_url ? <ExternalLink href={org.website_url} /> : <Value>{null}</Value>}
              {org?.locality && <span>· {org.locality}</span>}
              {org?.country_code && <span>· {org.country_code}</span>}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <ScoreBadge score={lead.latest_score} />
            <Badge>{lead.status}</Badge>
            {can('research:run') && (
              <Button
                variant="secondary"
                onClick={async () => {
                  if (!token) return;
                  try {
                    const run = await api.startResearch(token, lead.id);
                    setResearch(`Started ${run.workflow_id}`);
                  } catch (e) {
                    setResearch(e instanceof Error ? e.message : 'failed to start research');
                  }
                }}
              >
                Run research
              </Button>
            )}
          </div>
        </div>
        {research && (
          <p className="mt-2 rounded-lg bg-slate-100 px-3 py-1.5 font-mono text-xs text-slate-700">
            {research}
          </p>
        )}
        {lead.status_reason && (
          <p className="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-900">
            {lead.status_reason}
          </p>
        )}
      </div>

      <Card>
        <dl className="grid gap-4 sm:grid-cols-3 lg:grid-cols-6">
          <Field label="Campaign">
            <Value>{lead.campaign_name}</Value>
          </Field>
          <Field label="Findings">
            {lead.finding_count} <span className="text-slate-400">/ {lead.evidence_count} ev.</span>
          </Field>
          <Field label="Contactable">
            <Badge tone={lead.has_eligible_contact ? 'good' : 'warn'}>
              {lead.has_eligible_contact ? 'yes' : 'no'}
            </Badge>
          </Field>
          <Field label="Follow-ups sent">{lead.followups_sent}</Field>
          <Field label="Last contacted">
            <Time value={lead.last_contacted_at} />
          </Field>
          <Field label="Replied">
            <Time value={lead.replied_at} />
          </Field>
        </dl>
        {lead.replied_at && (
          <p className="mt-3 rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-900">
            This lead replied. All further automated outreach is stopped.
          </p>
        )}
      </Card>

      <div className="flex flex-wrap gap-1 border-b border-slate-200">
        {TABS.map((name) => (
          <button
            key={name}
            onClick={() => setTab(name)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm transition ${
              tab === name
                ? 'border-slate-900 font-medium text-slate-900'
                : 'border-transparent text-slate-500 hover:text-slate-800'
            }`}
          >
            {name}
          </button>
        ))}
      </div>

      <Card>
        {tab === 'Findings' && <FindingsTab leadId={lead.id} />}
        {tab === 'Contacts' && <ContactsTab leadId={lead.id} />}
        {tab === 'Scoring' && <ScoringTab leadId={lead.id} />}
        {tab === 'Drafts' && <DraftsTab leadId={lead.id} />}
        {tab === 'Delivery' && <DeliveryTab leadId={lead.id} />}
        {tab === 'Timeline' && <TimelineTab leadId={lead.id} />}
        {tab === 'Business' && <BusinessTab organizationId={lead.organization_id} />}
      </Card>
    </div>
  );
}
