'use client';

/**
 * Campaigns and their policy.
 *
 * A campaign's policy is the thing that decides what Titan may do on its
 * behalf, so this screen shows every field rather than the two or three that
 * fit neatly. Sending authorization is deliberately awkward: a separate
 * control, a typed acknowledgement, and no way to flip it as a side effect of
 * editing anything else.
 */

import React, { useState } from 'react';
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  Field,
  Spinner,
  Time,
  Value,
} from '@/components/crm/ui';
import { useApi, useSession } from '@/lib/session';
import { api, type Campaign, type CampaignPolicy } from '@/lib/titan';

const REQUIRED_ACK = 'I authorize production sending for this campaign';

const INDUSTRIES = [
  'general',
  'law_firm',
  'gym_fitness',
  'restaurant',
  'real_estate',
  'hvac_home_services',
  'med_spa',
  'dentist',
];

function PolicyPanel({ campaign }: { campaign: Campaign }) {
  const { token, can } = useSession();
  const { data, error, loading, reload } = useApi<CampaignPolicy>(
    (t) => api.policy(t, campaign.id),
    [campaign.id],
  );
  const [draft, setDraft] = useState<Partial<CampaignPolicy>>({});
  const [ack, setAck] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (loading) return <Spinner />;
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data) return null;

  const value = <K extends keyof CampaignPolicy>(key: K): CampaignPolicy[K] =>
    (draft[key] ?? data[key]) as CampaignPolicy[K];

  const numberInput = (
    key: 'min_lead_score' | 'daily_send_limit' | 'recipient_domain_daily_limit' | 'max_followups',
    label: string,
    hint: string,
  ) => (
    <label className="block">
      <span className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</span>
      <input
        type="number"
        min={0}
        value={String(value(key))}
        disabled={!can('campaign:write')}
        onChange={(e) => setDraft({ ...draft, [key]: Number(e.target.value) })}
        className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-50 disabled:text-slate-500"
      />
      <span className="mt-0.5 block text-xs text-slate-500">{hint}</span>
    </label>
  );

  const dirty = Object.keys(draft).length > 0;

  return (
    <div className="space-y-5">
      <dl className="grid gap-4 sm:grid-cols-3">
        <Field label="Operating mode" hint="The effective mode is the most restrictive of process, workspace, and campaign">
          <Badge>{data.operating_mode}</Badge>
        </Field>
        <Field label="Sending authorized">
          <Badge tone={data.sending_authorized ? 'good' : 'warn'}>
            {data.sending_authorized ? 'yes' : 'no'}
          </Badge>
        </Field>
        <Field label="Verified email required">
          <Badge tone={data.require_verified_email ? 'good' : 'warn'}>
            {data.require_verified_email ? 'yes' : 'no'}
          </Badge>
        </Field>
      </dl>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {numberInput('min_lead_score', 'Minimum lead score', 'Below this, no draft is generated')}
        {numberInput('daily_send_limit', 'Daily send limit', 'Per campaign, per UTC day')}
        {numberInput(
          'recipient_domain_daily_limit',
          'Per-domain daily limit',
          'Caps messages to one recipient domain',
        )}
        {numberInput('max_followups', 'Maximum follow-ups', 'A reply stops the sequence regardless')}
      </div>

      <div>
        <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
          Permitted contact provenance
        </p>
        <div className="mt-1 flex flex-wrap gap-1.5">
          {data.allowed_contact_sources.map((source) => (
            <Badge key={source}>{source}</Badge>
          ))}
        </div>
        <p className="mt-1 text-xs text-slate-500">
          A pattern-guessed address is never eligible, whatever this list says.
        </p>
      </div>

      {can('campaign:write') && (
        <div className="flex items-center gap-2 border-t border-slate-100 pt-4">
          <Button
            variant="primary"
            disabled={!dirty || busy}
            onClick={async () => {
              if (!token) return;
              setBusy(true);
              setNotice(null);
              try {
                await api.updatePolicy(token, campaign.id, draft);
                setDraft({});
                setNotice('Policy saved.');
                reload();
              } catch (e) {
                setNotice(e instanceof Error ? e.message : 'save failed');
              } finally {
                setBusy(false);
              }
            }}
          >
            Save policy
          </Button>
          {dirty && (
            <Button variant="ghost" onClick={() => setDraft({})}>
              Discard
            </Button>
          )}
          {notice && <span className="text-xs text-slate-600">{notice}</span>}
        </div>
      )}

      {can('sending:enable') && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-4">
          <p className="text-sm font-medium text-amber-900">Sending authorization</p>
          <p className="mt-1 text-xs text-amber-800">
            Authorizing here is one of two switches. The process-level kill
            switch must also be on, and every send is still evaluated against
            suppression, quotas, and message validation.
          </p>
          {data.sending_authorized ? (
            <div className="mt-3">
              <Button
                variant="danger"
                disabled={busy}
                onClick={async () => {
                  if (!token) return;
                  setBusy(true);
                  try {
                    await api.authorizeSending(token, campaign.id, false, '');
                    setNotice('Sending disabled.');
                    reload();
                  } catch (e) {
                    setNotice(e instanceof Error ? e.message : 'failed');
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                Revoke authorization
              </Button>
            </div>
          ) : (
            <div className="mt-3 space-y-2">
              <label className="block text-xs text-amber-900">
                Type exactly: <code className="font-mono">{REQUIRED_ACK}</code>
                <input
                  value={ack}
                  onChange={(e) => setAck(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-sm"
                />
              </label>
              <Button
                variant="primary"
                disabled={busy || ack.trim() !== REQUIRED_ACK}
                onClick={async () => {
                  if (!token) return;
                  setBusy(true);
                  try {
                    await api.authorizeSending(token, campaign.id, true, ack.trim());
                    setAck('');
                    setNotice('Sending authorized for this campaign.');
                    reload();
                  } catch (e) {
                    setNotice(e instanceof Error ? e.message : 'failed');
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                Authorize sending
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function NewCampaign({ onCreated }: { onCreated: () => void }) {
  const { token, can } = useSession();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [industry, setIndustry] = useState('general');
  const [minScore, setMinScore] = useState(70);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!can('campaign:write')) return null;
  if (!open) {
    return (
      <Button variant="primary" onClick={() => setOpen(true)}>
        New campaign
      </Button>
    );
  }

  return (
    <Card title="New campaign" subtitle="Created in research-only mode with sending off">
      <form
        className="grid gap-3 sm:grid-cols-2"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!token) return;
          setBusy(true);
          setError(null);
          try {
            await api.createCampaign(token, {
              name: name.trim(),
              slug: slug.trim(),
              industry,
              min_lead_score: minScore,
            });
            setOpen(false);
            setName('');
            setSlug('');
            onCreated();
          } catch (err) {
            setError(err instanceof Error ? err.message : 'create failed');
          } finally {
            setBusy(false);
          }
        }}
      >
        <label className="block">
          <span className="text-xs font-medium uppercase tracking-wide text-slate-500">Name</span>
          <input
            required
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              if (!slug) {
                setSlug(
                  e.target.value
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, '-')
                    .replace(/^-|-$/g, ''),
                );
              }
            }}
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium uppercase tracking-wide text-slate-500">Slug</span>
          <input
            required
            pattern="[a-z0-9\-]+"
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-1.5 font-mono text-sm"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Industry
          </span>
          <select
            value={industry}
            onChange={(e) => setIndustry(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm"
          >
            {INDUSTRIES.map((i) => (
              <option key={i} value={i}>
                {i.replace(/_/g, ' ')}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Minimum lead score
          </span>
          <input
            type="number"
            min={0}
            max={100}
            value={minScore}
            onChange={(e) => setMinScore(Number(e.target.value))}
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm"
          />
        </label>

        {error && (
          <p className="sm:col-span-2 rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-800">
            {error}
          </p>
        )}

        <div className="flex gap-2 sm:col-span-2">
          <Button type="submit" variant="primary" disabled={busy}>
            Create
          </Button>
          <Button variant="ghost" onClick={() => setOpen(false)}>
            Cancel
          </Button>
        </div>
      </form>
    </Card>
  );
}

export default function CampaignsPage() {
  const { data, error, loading, reload } = useApi((t) => api.campaigns(t), []);
  const [selected, setSelected] = useState<string | null>(null);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Campaigns</h1>
          <p className="mt-1 text-sm text-slate-500">
            A campaign carries its own policy. The effective operating mode is
            the most restrictive of process, workspace, and campaign.
          </p>
        </div>
        <NewCampaign onCreated={reload} />
      </div>

      {error ? (
        <ErrorNote error={error} onRetry={reload} />
      ) : loading && !data ? (
        <Spinner />
      ) : !data || data.items.length === 0 ? (
        <Card>
          <Empty>No campaigns yet.</Empty>
        </Card>
      ) : (
        <div className="space-y-3">
          {data.items.map((campaign) => (
            <Card
              key={campaign.id}
              title={campaign.name}
              subtitle={`${campaign.slug} · ${campaign.industry.replace(/_/g, ' ')}`}
              action={
                <div className="flex items-center gap-2">
                  <Badge>{campaign.status}</Badge>
                  <Button
                    variant="ghost"
                    onClick={() => setSelected(selected === campaign.id ? null : campaign.id)}
                  >
                    {selected === campaign.id ? 'Hide policy' : 'Policy'}
                  </Button>
                </div>
              }
            >
              <dl className="grid gap-4 sm:grid-cols-3">
                <Field label="Created">
                  <Time value={campaign.created_at} />
                </Field>
                <Field label="Slug">
                  <Value mono>{campaign.slug}</Value>
                </Field>
                <Field label="Industry">
                  <Badge>{campaign.industry}</Badge>
                </Field>
              </dl>
              {selected === campaign.id && (
                <div className="mt-5 border-t border-slate-100 pt-5">
                  <PolicyPanel campaign={campaign} />
                </div>
              )}
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
