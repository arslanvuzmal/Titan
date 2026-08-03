# Titan-OS — Threat Model & Risk Analysis

**Document Version:** 0.2.0  
**Classification:** Internal Architecture & Security Standard  
**Last Updated:** 2026-08-03  

---

## 1. Overview & Trust Boundaries

Titan-OS is an evidence-first sales intelligence and outreach automation platform. It interacts with external untrusted web resources, third-party API providers, database instances, model gateways, and email dispatchers.

```
       [ Untrusted Web / Target Websites ]
                       │
             (1) HTTP / SSRF Guard
                       ▼
        [ Isolated Browser Worker ] ◄── (2) Credential Boundary
                       │
             (3) Evidence JSON Output
                       ▼
            [ Titan Control Plane ] ◄── (4) RBAC & Auth Gate
                       │
       ┌───────────────┼───────────────┐
       ▼               ▼               ▼
[ PostgreSQL ]   [ Model Gateway ]  [ Transactional Outbox ]
 (Tenant RLS)    (Cost Caps/Schema)  (Atomic Quota / Suppression)
                                               │
                                      (5) Outbound Gate
                                               ▼
                                      [ Resend Provider ]
```

### Trust Zones

1. **Zone 0 — Untrusted Public Internet:** Target company websites, external redirect URLs, scraped HTML content, webhooks from email providers.
2. **Zone 1 — Browser Worker Sandbox:** Isolated TypeScript Playwright execution environment with zero access to email keys, database credentials, or internal APIs.
3. **Zone 2 — Titan Control Plane & Workflows:** Core FastAPI backend, Temporal workers, PostgreSQL data layer, model gateway.
4. **Zone 3 — External Authorized APIs:** Provider endpoints (Google Places, NVIDIA, Gemini, OpenRouter, Cloudflare AI, Resend).

---

## 2. Threat Taxonomy & Mitigation Matrix

### 2.1 Web Crawling & Network Egress

| Threat ID | Threat Description | Severity | Structural Mitigation in Titan-OS | Verification Method |
|---|---|---|---|---|
| **T-01** | **Server-Side Request Forgery (SSRF)**: Crawling seed URLs or redirects targeting internal IPs (e.g. `169.254.169.254`, `127.0.0.1`, `10.0.0.0/8`). | Critical | Dual-layer `url_guard` (Python control plane + TS worker). Enforces HTTP/HTTPS scheme allowlist, port 80/443 restriction, full IP address resolution check, IPv4-mapped IPv6 unwrapping, metadata host denylist, redirect chain re-validation. | `tests/security/test_url_guard.py` (60+ cases) & `apps/browser-worker/test/urlGuard.test.ts`. |
| **T-02** | **DNS Rebinding**: Hostname resolves to public IP during pre-check, but re-resolves to loopback/metadata IP during HTTP fetch. | Critical | Pinned-IP socket connection or immediate IP re-validation on every redirect hop; immediate failure if any IP in the resolved set falls into a reserved CIDR block. | Hermetic resolver test with mixed public/private A records. |
| **T-03** | **Malicious Redirects**: Seed URL redirects through long chains to internal resources or non-http protocols (`file://`, `gopher://`). | High | Max redirect limit (5 hops), scheme allowlist enforced on every redirect destination, strict timeout (120s max total crawl). | Redirect fuzzing tests & fixture site redirect suites. |
| **T-04** | **Browser Escape & Container Compromise**: Malicious JS on target website exploits Playwright/Chromium zero-day to compromise worker host. | High | Browser worker runs in isolated, non-root Docker container with read-only filesystem, dropped Linux capabilities, zero access to database or email provider keys. | Container security linting & credential isolation tests. |
| **T-05** | **Unbounded Crawling / Resource Exhaustion**: Target website creates infinite link loops or massive files to crash worker memory. | Medium | Hard response size limit (5MB), page count cap (12 max), depth cap (2 max), request timeout (60s), origin concurrency limit. | Crawl resource cap unit tests. |

### 2.2 Model Gateway & Prompt Safety

| Threat ID | Threat Description | Severity | Structural Mitigation in Titan-OS | Verification Method |
|---|---|---|---|---|
| **T-06** | **Direct Prompt Injection**: Scraped website contains text instructing Titan to send unauthorized emails, reveal keys, or alter score. | Critical | Strict architectural channel separation: system prompt, policy instructions, evidence data, and untrusted webpage text are segregated into isolated schema fields. Models are never given direct tools to send emails. | `tests/security/test_prompt_injection_fixtures.py`. |
| **T-07** | **Stored Prompt Injection / Evidence Poisoning**: Attacker crafts website content to corrupt stored audit findings and trick human approver into approving a malicious draft. | High | Message evidence validator requires every pitch claim to trace to a verified selector, observed value, and HTTP response fingerprint. Human approval UI displays raw screenshot and selector diff. | Evidence mapping validator tests. |
| **T-08** | **Model Hallucination**: Model invents unsupported claims, metrics, or company names in outreach draft. | High | Deterministic `MessageEvidenceValidator` AST-scans generated text. Rejects unlinked metrics, unverified facts, and un-sourced claims before draft approval. | `test_message_evidence_validator.py`. |
| **T-09** | **Model Cost Exhaustion / Financial Denial of Service**: Runaway research loops trigger massive token spend on expensive models. | High | Per-workspace, per-campaign, and per-lead token & dollar budgets in `usage_ledger`. Automatic circuit breaker trips when budget threshold is hit. Hard stop; no fallback after successful but over-budget call. | `tests/models/test_budget_enforcement.py`. |

### 2.3 Delivery & Compliance Security

| Threat ID | Threat Description | Severity | Structural Mitigation in Titan-OS | Verification Method |
|---|---|---|---|---|
| **T-10** | **Direct Provider Send (Bypass Outbox)**: Model or API handler invokes email provider directly without outbox, suppression, or quota check. | Critical | Invariant #1 & #4: Models possess no email tools. All sends pass through `outbox_messages` table. Outbox worker re-evaluates full authorization chain before delivery. AST test blocks provider imports outside outbox worker. | `tests/invariants/test_repository_invariants.py`. |
| **T-11** | **Suppression Bypass**: Attempt to email an address that has unsubscribed, bounced, complained, or been manually suppressed. | Critical | `suppression_entries` table checked at draft creation AND re-checked in same database transaction as outbox quota reservation. Suppression entries survive contact deletion. | `tests/integration/test_suppression_race.py`. |
| **T-12** | **Duplicate Send / Event Replay**: Worker restart or Temporal activity retry causes double-sending to the same recipient. | Critical | Outbox rows carry `provider_idempotency_key UNIQUE`. Resend API calls pass idempotency key. Database leases outbox rows using `SELECT FOR UPDATE SKIP LOCKED`. | `tests/delivery/test_outbox_delivery.py`. |
| **T-13** | **Quota Overrun**: Concurrent workers send more emails than allowed by campaign or recipient-domain daily caps. | Critical | Atomic quota reservation query: `INSERT ... ON CONFLICT DO UPDATE SET used = used + 1 WHERE used < limit RETURNING used`. Single statement reservation prevents race conditions. | `tests/integration/test_quota_concurrency.py`. |
| **T-14** | **Webhook Forgery & State Regression**: Attacker sends fake provider webhooks to mark bounced mail as delivered or corrupt campaign metrics. | High | Mandatory Svix-compatible webhook signature verification. Raw payload logged. Deduplication by `(provider, provider_event_id)`. Monotonic state machine prevents delayed `sent` from overwriting `bounced`/`complained`. | `tests/delivery/test_webhooks.py`. |
| **T-15** | **Email Address Guessing**: System attempts to contact unverified pattern-guessed addresses (`ceo@domain.com`). | High | Strictly prohibited. Addresses must originate from public first-party site, verified enrichment API, or manual entry, with explicit `contact_verifications` provenance record. | `tests/intelligence/test_contacts.py`. |

### 2.4 Multitenancy & Data Security

| Threat ID | Threat Description | Severity | Structural Mitigation in Titan-OS | Verification Method |
|---|---|---|---|---|
| **T-16** | **Cross-Workspace Data Leakage**: User in Workspace A accesses leads, contacts, or audit findings in Workspace B. | Critical | Invariant #17: Every query enforces `workspace_id`. Database layer utilizes SQLAlchemy workspace-scoped sessions and PostgreSQL Row-Level Security (RLS) policies. | `tests/security/test_cross_workspace_access.py`. |
| **T-17** | **Secret Leakage in Logs/API**: Provider API keys, credentials, or sensitive headers leak into application logs or API responses. | Critical | Centralized redaction filter (`titan.security.redaction`) scrub secrets, auth headers, and tokens from structured JSON logs. API response models explicitly omit credential fields. | Secret scanner in CI & log redaction unit tests. |
| **T-18** | **Unauthorized Autopilot Activation**: User toggles single boolean to turn on live autopilot sending without required preflight setup. | High | Multi-key authorization chain required: global environment switch, workspace active, campaign active, verified sender identity, physical address configured, SPF/DKIM/DMARC acknowledged. Fail-closed by default. | `tests/policy/test_send_authorization.py`. |
| **T-19** | **Artifact Path Traversal**: Scraped page filename or artifact path escapes storage directory (`../../../etc/passwd`). | High | Strict path sanitization on all stored screenshot and HTML artifact filenames; storage paths generated via UUID v7 content addresses. | Path traversal unit tests. |

---

## 3. Incident Response Thresholds

If any of the following occur, Titan-OS automatically triggers an emergency global send halt (`TITAN_OUTBOUND_ENABLED=false`):

1. **Bounce Rate Exceeds 2.0%** over any 24-hour window.
2. **Spam Complaint Rate Exceeds 0.05%** over any 24-hour window.
3. **Outbox Idempotency Key Collision Failure** detected on provider send.
4. **Webhook Signature Verification Failure Rate Exceeds 5%** over 1 hour.
5. **SSRF Guard Rejection** on internal IP address attempt (logged as security alert).
