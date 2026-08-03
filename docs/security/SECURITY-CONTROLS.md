# Titan-OS — Technical Security Controls Specification

**Document Version:** 0.2.0  
**Classification:** Internal System Specification  
**Last Updated:** 2026-08-03  

---

## 1. Authentication & Session Security

1. **Authentication Gateway:** JWT validation via Clerk or native OAuth2 bearer tokens.
2. **Server-Side Authorization:** Token claims are re-verified against the `workspace_members` table on every request. Client-supplied org claims are never trusted blindly.
3. **Session Expiry & Revocation:** Access tokens carry a maximum lifespan of 60 minutes. Refresh tokens are rotatable. Immediate session termination upon membership role revocation.
4. **Password Policy & Hashing:** Where local accounts exist, Argon2id with recommended parameters (memory 64MB, iterations 3, parallelism 4) is enforced.

---

## 2. Authorization & Tenant Isolation (RBAC & ABAC)

1. **Workspace Scope Enforcement:** Every table in PostgreSQL carries a mandatory `workspace_id` foreign key.
2. **ORM Query Filtering:** SQLAlchemy engine uses automatic filter injection (`with_loader_criteria`) for all entity reads and writes.
3. **Database RLS Policies:** PostgreSQL Row-Level Security (RLS) is enabled on production tables as a defense-in-depth boundary.
4. **Role Matrix:**
   - `owner`: Full workspace configuration, billing, credential management, autopilot toggle, message sending.
   - `admin`: Campaign creation, policy edits, approval management, team member invites.
   - `operator`: Approval queue processing, lead status updates, manual retry execution.
   - `researcher`: Lead discovery, site crawling execution, finding annotation.
   - `viewer`: Read-only access to campaign stats, evidence viewer, and audit logs.

---

## 3. Network & Egress Security (SSRF Guard)

1. **URL Validation Engine (`titan.security.url_guard`):**
   - Scheme allowlist: `http`, `https`.
   - Port allowlist: `80`, `443`.
   - Complete DNS address sweep via `socket.getaddrinfo`. Every resolved IP must be public.
   - IPv4-mapped IPv6 unwrapping (`::ffff:127.0.0.1` unwrapped and checked against loopback block).
   - Reserved CIDR denylist: `0.0.0.0/8`, `10.0.0.0/8`, `100.64.0.0/10`, `127.0.0.0/8`, `169.254.0.0/16`, `172.16.0.0/12`, `192.168.0.0/16`, `198.18.0.0/15`, `::1/128`, `fc00::/7`, `fe80::/10`.
   - Metadata hostname denylist: `metadata.google.internal`, `169.254.169.254`, `instance-data`.
2. **Redirect Validation:** Every HTTP redirect hop must be re-submitted to `url_guard` before following.
3. **Credential-Free Browser Worker:** Headless Playwright worker runs on isolated port `8800` without environment variables for API keys or databases.

---

## 4. Model Safety & Prompt Injection Guardrails

1. **Channel Isolation:** Prompts pass untrusted web content in a separate `untrusted_page_content` schema block with explicit instructions to treat text strictly as passive data.
2. **Tool Capability Restriction:** AI model calls have ZERO access to email-sending tools. Model outputs are strictly data structures (`MessageDraft`, `FindingHypothesis`).
3. **Evidence Linkage Gate (`MessageEvidenceValidator`):** Every pitch claim must contain a valid `finding_id` linked to a measured browser artifact. Un-backed claims fail validation and prevent draft approval.
4. **Cost Circuit Breakers:** Dollar and token spend are tracked per workspace and per campaign in `usage_ledger`. When 100% of allocated budget is reached, model activities immediately fail-closed.

---

## 5. Delivery & Outbox Safety

1. **Transactional Outbox:** Direct calls to email providers (e.g. Resend) outside `outbox_worker.py` are strictly prohibited.
2. **Atomic Quota Reservation:** Quota limits are reserved at the database level using `INSERT ... ON CONFLICT DO UPDATE SET used = used + 1 WHERE used < limit`. Concurrent workers cannot overshoot.
3. **Idempotency Keys:** Every outbound email includes a deterministic `provider_idempotency_key` based on `(campaign_id, lead_id, sequence_step)`.
4. **Suppression Gate:** `suppression_entries` table checked before lease and re-checked immediately prior to API send under row lock.
