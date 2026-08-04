/**
 * Typed client for the Titan-OS API.
 *
 * Replaces the pre-0.2 `demoMode.ts`, which served hardcoded fabrications
 * ("Acme Corp ($150k ARR Potential)") indistinguishably from real data. Every
 * value the dashboard renders now comes from /api/v1 or is absent.
 *
 * There is no fallback to sample data on error. A failed request renders an
 * error state, because a dashboard that invents numbers when the backend is
 * down is worse than one that says so.
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

export interface Lead {
  id: string;
  campaign_id: string;
  organization_id: string;
  status: string;
  latest_score: number | null;
  replied_at: string | null;
  last_contacted_at: string | null;
  followups_sent: number;
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

export interface Score {
  total: number;
  band: string;
  components: Record<string, { raw: number; weight: number; weighted: number; reason: string }>;
  reasons: string[];
  policy_version: string;
  threshold_applied: number;
  passed_threshold: boolean;
  created_at: string;
}

export interface Draft {
  id: string;
  lead_id: string;
  status: string;
  subject: string;
  body_text: string;
  claim_map: Array<{
    sentence: string;
    claim: string;
    finding_id: string;
    evidence_ids: string[];
    source_url?: string | null;
  }>;
  validation_passed: boolean;
  validation_report: { passed: boolean; violations: Array<{ code: string; detail: string }> };
  version: number;
  created_at: string;
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

async function request<T>(path: string, token?: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    cache: 'no-store',
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* response was not JSON */
    }
    throw new TitanError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export async function login(email: string, workspaceSlug: string): Promise<string> {
  const response = await fetch(`${API_BASE}/api/v1/auth/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, workspace_slug: workspaceSlug }),
    cache: 'no-store',
  });
  if (!response.ok) throw new TitanError(response.status, 'login failed');
  return (await response.json()).access_token as string;
}

export const api = {
  preflight: () => request<SendingPreflight>('/ops/sending-preflight'),
  workspace: (t: string) => request<Workspace>('/api/v1/workspace', t),
  campaigns: (t: string) => request<Page<Campaign>>('/api/v1/campaigns', t),
  policy: (t: string, id: string) =>
    request<CampaignPolicy>(`/api/v1/campaigns/${id}/policy`, t),
  leads: (t: string, limit = 100) =>
    request<Page<Lead>>(`/api/v1/leads?limit=${limit}`, t),
  lead: (t: string, id: string) => request<Lead>(`/api/v1/leads/${id}`, t),
  findings: (t: string, leadId: string) =>
    request<Finding[]>(`/api/v1/leads/${leadId}/findings`, t),
  evidence: (t: string, findingId: string) =>
    request<Evidence[]>(`/api/v1/findings/${findingId}/evidence`, t),
  scores: (t: string, leadId: string) =>
    request<Score[]>(`/api/v1/leads/${leadId}/scores`, t),
  drafts: (t: string) => request<Page<Draft>>('/api/v1/drafts', t),
  suppressions: (t: string) => request<Page<unknown>>('/api/v1/suppressions', t),
  usage: (t: string) =>
    request<{ window_date: string; quotas: unknown[]; spend_usd: number; model_calls: number }>(
      '/api/v1/usage',
      t,
    ),
};
