/**
 * Typed client for the Titan-OS API.
 *
 * Replaces the pre-0.2 `demoMode.ts`, which served hardcoded fabrications
 * ("Acme Corp ($150k ARR Potential)") indistinguishably from real data. Every
 * value the CRM renders now comes from /api/v1 or is absent.
 *
 * There is no fallback to sample data on error. A failed request renders an
 * error state, because a CRM that invents numbers when the backend is down is
 * worse than one that says so.
 */

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, '') ?? 'http://localhost:8000';

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  operating_mode: string;
  sending_authorized: boolean;
  daily_send_limit: number;
}

export interface Principal {
  user_id: string;
  email: string;
  workspace_id: string;
  role: string;
  capabilities: string[];
}

export interface Campaign {
  id: string;
  name: string;
  slug: string;
  status: string;
  industry: string;
  created_at: string;
}

export interface CampaignPolicy {
  operating_mode: string;
  sending_authorized: boolean;
  min_lead_score: number;
  require_verified_email: boolean;
  require_evidence_backed_claims: boolean;
  daily_send_limit: number;
  recipient_domain_daily_limit: number;
  max_followups: number;
  allowed_contact_sources: string[];
}

export interface OrganizationSummary {
  id: string;
  display_name: string;
  canonical_domain: string | null;
  website_url: string | null;
  industry: string;
  phone_e164: string | null;
  rating: number | null;
  review_count: number | null;
  business_status: string | null;
  locality: string | null;
  region: string | null;
  country_code: string | null;
}

export interface OrganizationLocation {
  id: string;
  formatted_address: string | null;
  locality: string | null;
  region: string | null;
  postal_code: string | null;
  country_code: string | null;
  timezone: string | null;
  is_primary: boolean;
}

export interface Organization extends OrganizationSummary {
  legal_name: string | null;
  normalized_name: string;
  google_place_id: string | null;
  employee_estimate: number | null;
  provenance: Array<Record<string, unknown>>;
  locations: OrganizationLocation[];
  domains: string[];
  created_at: string | null;
}

export interface Lead {
  id: string;
  campaign_id: string;
  organization_id: string;
  status: string;
  latest_score: number | null;
  replied_at: string | null;
  last_contacted_at: string | null;
  followups_sent: number;
  status_reason: string | null;
  next_action_at: string | null;
  created_at: string | null;
  organization: OrganizationSummary | null;
  campaign_name: string | null;
  finding_count: number;
  draft_count: number;
  message_count: number;
  evidence_count: number;
  has_eligible_contact: boolean;
}

export interface ContactChannel {
  id: string;
  channel_type: string;
  value: string;
  normalized_value: string;
  value_domain: string | null;
  source: string;
  source_url: string | null;
  discovered_at: string;
  verification_status: string;
  confidence: number;
  consent_basis: string | null;
  is_active: boolean;
  eligible_for_outreach: boolean;
  ineligibility_reason: string | null;
  suppressed: boolean;
}

export interface Contact {
  id: string;
  organization_id: string;
  full_name: string | null;
  role_title: string | null;
  is_decision_maker: boolean;
  is_generic_role: boolean;
  notes: string | null;
  channels: ContactChannel[];
}

export interface Finding {
  id: string;
  category: string;
  issue_type: string;
  title: string;
  page_url: string | null;
  selector: string | null;
  observed_value: string | null;
  expected_behavior: string | null;
  severity: string;
  confidence: number;
  business_impact: string | null;
  recommended_solution: string | null;
  verification_method: string;
  contradicted: boolean;
}

export interface Evidence {
  id: string;
  finding_id: string;
  excerpt: string | null;
  source_url: string | null;
  captured_at: string;
}

export interface ScoreComponent {
  raw: number;
  weight: number;
  weighted: number;
  reason: string;
}

export interface Score {
  total: number;
  band: string;
  components: Record<string, ScoreComponent>;
  reasons: string[];
  policy_version: string;
  threshold_applied: number;
  passed_threshold: boolean;
  created_at: string;
}

export interface ClaimMapEntry {
  sentence: string;
  claim: string;
  finding_id: string;
  evidence_ids: string[];
  source_url?: string | null;
}

export interface Violation {
  code: string;
  detail: string;
  excerpt?: string | null;
}

export interface Draft {
  id: string;
  lead_id: string;
  status: string;
  subject: string;
  body_text: string;
  claim_map: ClaimMapEntry[];
  validation_passed: boolean;
  validation_report: {
    passed: boolean;
    word_count?: number;
    violations: Violation[];
    supported_sentences?: string[];
  };
  version: number;
  created_at: string;
}

export interface Message {
  id: string;
  lead_id: string;
  to_email_normalized: string;
  subject: string;
  state: string;
  sent_at: string | null;
  delivered_at: string | null;
  bounced_at: string | null;
  complained_at: string | null;
}

export interface Suppression {
  id: string;
  scope: string;
  normalized_value: string;
  reason: string;
  source: string;
  suppressed_at: string;
  expires_at: string | null;
}

export interface TimelineEvent {
  at: string;
  kind: string;
  title: string;
  detail: string | null;
  reference_id: string | null;
  severity: string | null;
}

export interface WorkflowRun {
  id: string;
  workflow_id: string;
  workflow_type: string;
  status: string;
  started_at: string;
  closed_at: string | null;
  failure_reason: string | null;
}

export interface Usage {
  window_date: string;
  quotas: Array<Record<string, unknown>>;
  spend_usd: number;
  model_calls: number;
}

export interface CrmStats {
  leads_total: number;
  leads_by_status: Record<string, number>;
  leads_by_band: Record<string, number>;
  campaigns_total: number;
  organizations_total: number;
  contacts_total: number;
  eligible_contacts: number;
  findings_total: number;
  evidence_total: number;
  drafts_by_status: Record<string, number>;
  messages_by_state: Record<string, number>;
  suppressions_total: number;
  replied_total: number;
  sending_authorized: boolean;
  operating_mode: string;
}

export interface SendingPreflight {
  process_sending_enabled: boolean;
  email_provider: string;
  blockers: string[];
  would_send: boolean;
  note: string;
}

export class TitanError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = 'TitanError';
  }
}

/**
 * Turn a failed response into something a human can act on.
 *
 * FastAPI reports a 422 as `detail: [{loc, msg, type}, ...]`, so naively
 * assigning `detail` to a string renders "[object Object]" — an error message
 * that tells the operator nothing. Each shape is handled explicitly.
 */
async function describeFailure(response: Response): Promise<string> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return `${response.status} ${response.statusText}`;
  }

  const detail = (body as { detail?: unknown; error?: unknown })?.detail;
  if (typeof detail === 'string') return detail;

  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const entry = item as { loc?: unknown[]; msg?: string };
        const where = Array.isArray(entry.loc) ? entry.loc.join('.') : '';
        return where ? `${where}: ${entry.msg ?? 'invalid'}` : (entry.msg ?? 'invalid');
      })
      .join('; ');
  }

  const error = (body as { error?: unknown })?.error;
  if (typeof error === 'string') return error;

  return `${response.status} ${response.statusText}`;
}

async function call<T>(
  path: string,
  { token, method = 'GET', body }: { token?: string; method?: string; body?: unknown } = {},
): Promise<T> {
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store',
  });

  if (!response.ok) {
    throw new TitanError(response.status, await describeFailure(response));
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

/** Build a querystring, dropping keys the caller left unset. */
function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') {
      search.set(key, String(value));
    }
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : '';
}

export async function login(username: string, passcode: string): Promise<string> {
  const body = await call<{ access_token: string }>('/api/v1/auth/token', {
    method: 'POST',
    body: { username, passcode },
  });
  return body.access_token;
}

export interface LeadFilters {
  campaign_id?: string;
  status?: string;
  min_score?: number;
  max_score?: number;
  q?: string;
  has_reply?: boolean;
  contacted?: boolean;
  sort?: 'score' | 'created' | 'contacted' | 'next_action';
  direction?: 'asc' | 'desc';
  limit?: number;
  offset?: number;
}

export const api = {
  // --- unauthenticated -----------------------------------------------------
  preflight: () => call<SendingPreflight>('/ops/sending-preflight'),

  // --- identity ------------------------------------------------------------
  me: (t: string) => call<Principal>('/api/v1/me', { token: t }),
  workspace: (t: string) => call<Workspace>('/api/v1/workspace', { token: t }),

  // --- overview ------------------------------------------------------------
  stats: (t: string) => call<CrmStats>('/api/v1/stats', { token: t }),

  // --- campaigns -----------------------------------------------------------
  campaigns: (t: string) => call<Page<Campaign>>('/api/v1/campaigns', { token: t }),
  policy: (t: string, id: string) =>
    call<CampaignPolicy>(`/api/v1/campaigns/${id}/policy`, { token: t }),
  updatePolicy: (t: string, id: string, patch: Partial<CampaignPolicy>) =>
    call<CampaignPolicy>(`/api/v1/campaigns/${id}/policy`, {
      token: t,
      method: 'PATCH',
      body: patch,
    }),
  authorizeSending: (t: string, id: string, authorized: boolean, acknowledgement: string) =>
    call<CampaignPolicy>(`/api/v1/campaigns/${id}/sending-authorization`, {
      token: t,
      method: 'POST',
      body: { authorized, acknowledgement },
    }),
  createCampaign: (
    t: string,
    payload: { name: string; slug: string; industry: string; min_lead_score: number },
  ) => call<Campaign>('/api/v1/campaigns', { token: t, method: 'POST', body: payload }),

  // --- leads ---------------------------------------------------------------
  leads: (t: string, filters: LeadFilters = {}) =>
    call<Page<Lead>>(`/api/v1/leads${query({ ...filters })}`, { token: t }),
  lead: (t: string, id: string) => call<Lead>(`/api/v1/leads/${id}`, { token: t }),
  organization: (t: string, id: string) =>
    call<Organization>(`/api/v1/organizations/${id}`, { token: t }),
  contacts: (t: string, leadId: string) =>
    call<Contact[]>(`/api/v1/leads/${leadId}/contacts`, { token: t }),
  findings: (t: string, leadId: string) =>
    call<Finding[]>(`/api/v1/leads/${leadId}/findings`, { token: t }),
  evidence: (t: string, findingId: string) =>
    call<Evidence[]>(`/api/v1/findings/${findingId}/evidence`, { token: t }),
  scores: (t: string, leadId: string) =>
    call<Score[]>(`/api/v1/leads/${leadId}/scores`, { token: t }),
  timeline: (t: string, leadId: string) =>
    call<TimelineEvent[]>(`/api/v1/leads/${leadId}/timeline`, { token: t }),
  leadDrafts: (t: string, leadId: string) =>
    call<Draft[]>(`/api/v1/leads/${leadId}/drafts`, { token: t }),
  leadMessages: (t: string, leadId: string) =>
    call<Message[]>(`/api/v1/leads/${leadId}/messages`, { token: t }),
  startResearch: (t: string, leadId: string, seedUrl?: string) =>
    call<WorkflowRun>('/api/v1/research/runs', {
      token: t,
      method: 'POST',
      body: { lead_id: leadId, seed_url: seedUrl },
    }),

  // --- drafts and review ---------------------------------------------------
  drafts: (t: string, status?: string, limit = 100) =>
    call<Page<Draft>>(`/api/v1/drafts${query({ status, limit })}`, { token: t }),
  draft: (t: string, id: string) => call<Draft>(`/api/v1/drafts/${id}`, { token: t }),
  decide: (
    t: string,
    id: string,
    decision: 'approved' | 'rejected' | 'changes_requested',
    draftVersion: number,
    reason?: string,
  ) =>
    call<{ id: string; decision: string }>(`/api/v1/drafts/${id}/decision`, {
      token: t,
      method: 'POST',
      body: { decision, draft_version: draftVersion, reason },
    }),

  // --- delivery and compliance --------------------------------------------
  messages: (t: string, limit = 100) =>
    call<Page<Message>>(`/api/v1/messages${query({ limit })}`, { token: t }),
  suppressions: (t: string, limit = 200) =>
    call<Page<Suppression>>(`/api/v1/suppressions${query({ limit })}`, { token: t }),
  suppress: (
    t: string,
    payload: { value: string; scope: string; reason: string; note?: string },
  ) => call<Suppression>('/api/v1/suppressions', { token: t, method: 'POST', body: payload }),

  // --- operations ----------------------------------------------------------
  workflows: (t: string, limit = 50) =>
    call<Page<WorkflowRun>>(`/api/v1/workflows${query({ limit })}`, { token: t }),
  usage: (t: string) => call<Usage>('/api/v1/usage', { token: t }),
  contactSources: (t: string) =>
    call<{ eligible: string[]; never_eligible: string[] }>('/api/v1/contact-sources', {
      token: t,
    }),
};
