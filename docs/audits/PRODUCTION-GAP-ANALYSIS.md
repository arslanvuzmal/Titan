# Titan-OS — Production Gap Analysis

**Audit date:** 2026-08-02
**Baseline commit:** `b5c74685c9adb6def7ea98439b18a1a3703c95e9` (`main`)
**Working branch:** `agent/titan-os-production-hardening`
**Auditor:** automated principal-engineer review (Phase 0)

---

## 0. How this audit was produced

Commands actually executed against the baseline commit:

| Command | Result |
|---|---|
| `git ls-files \| wc -l` | 137 tracked files, 22,057 tracked lines |
| `git log --oneline` | 21+ commits, all on `main`, heavily weighted to CI/deploy firefighting |
| `apps/api/.venv/Scripts/python -m pytest tests/ -q` | **19 passed** in 28.32s |
| `grep -rn "Worker(" --include=*.py apps/api/app` | **0 matches** — no Temporal worker exists |
| `git ls-files -z \| xargs -0 grep -lIE '<secret patterns>'` | 1 match: `deploy/.env.example:55`, verified to be the literal placeholder `SG.your_sendgrid_api_key_here` — **not a real secret** |
| `git ls-files \| grep -cE '(\.venv\|__pycache__\|node_modules\|\.mypy_cache)'` | `0` — no build/cache artifacts tracked |
| toolchain probe | Python 3.11.15, uv 0.11.26, Node 24.17.0, pnpm 9.15.4, Docker 29.5.3 (daemon running) — full local verification is possible |

**Baseline honesty statement:** the 19 passing tests are real, but they exercise ~5% of the tracked backend and none of the behaviour that this mission defines as safety-critical (outbox, suppression, quotas, evidence, scoring, delivery authorization). None of those subsystems exist yet.

---

## 1. Executive summary

The repository at `main` is **not a partially-complete Titan-OS**. It is a *different product*: a generic "autonomous AI business operations platform" built around LangGraph agents, a Clerk-authenticated Next.js dashboard, and a mocked "16-step Golden Path" sales demo.

Measured against the Titan-OS product definition — *an evidence-first AI sales intelligence, website research, lead qualification, CRM, and controlled outreach operating system* — the following is true:

- **Discovery:** absent. No Google Places adapter, no lead-source abstraction, no deduplication.
- **Research:** absent. No crawler, no browser worker, no page model, no evidence store.
- **Findings/evidence:** absent. Nothing in the schema can hold a finding, a selector, an observed value, or an artifact.
- **Scoring:** absent. `execute_sales_agent_graph` returns the hardcoded literal `{"score": 85, "reasoning": "High match with ICP. Recent funding round detected."}`.
- **Contacts:** absent. No contact, channel, or verification table.
- **Outreach:** present but **dangerous** — a model-invocable tool posts directly to SendGrid with no outbox, no suppression, no quota, no idempotency key, and no approval linkage.
- **Durability:** claimed, not delivered. Four `@workflow.defn` classes exist; **no worker registers any of them**, so no workflow can ever execute outside the two in-process test-server tests.
- **Compliance:** absent. No suppression list, no unsubscribe, no mailing address, no sender identity, no bounce/complaint handling.

**Of the 22 non-negotiable safety invariants in §28 of the mission, the baseline satisfies 3** (#20 no LeadPilot dependency, #21 production sending disabled *only incidentally* because no key is configured, #22 research/draft modes function *only vacuously* because they do not exist). The remaining 19 are unenforced, and several are actively violated.

### Verdict

A repair-in-place strategy is not viable for the domain layer. The correct strategy — and the one taken in Phases 1–9 — is:

1. **Keep** the monorepo shape, the Docker/CI scaffolding, the Next.js app shell, and the genuinely-correct utilities (security headers, redaction, rate limiter skeleton).
2. **Replace** the persistence layer, the domain model, the workflow layer, the tool/send path, and the dashboard's data plane.
3. **Delete or quarantine** the mock "Golden Path", the demo-seeded analytics, and the direct-send email tool.

---

## 2. Finding classification

Severity definitions used throughout:

| Severity | Meaning |
|---|---|
| **Critical** | Can cause unauthorized email, data leakage across tenants, secret exposure, or silent duplicate/unbounded sending. Blocks any production enablement. |
| **High** | Core product capability is absent or structurally wrong; or a security control exists but is bypassable. |
| **Medium** | Correctness, reliability, or maintainability defect that will cause production incidents but is contained. |
| **Low** | Hygiene, documentation, or developer-experience defect. |
| **Already correct** | Verified working; keep as-is or with cosmetic change. |
| **Deferred** | Intentionally out of scope for this hardening pass, with a documented reason. |

**Counts:** 14 Critical · 21 High · 17 Medium · 9 Low · 7 Already correct · 6 Deferred.

---

## 3. CRITICAL findings

### C-01 — A model-invocable tool sends email directly to a live provider

- **Files:** `apps/api/app/tools/implementations/email_tool.py`, `apps/api/app/tools/security.py`, `apps/api/app/tools/registry.py`
- **Current behavior:** `EmailTool.execute()` is registered as an LLM-callable tool. It reads `SENDGRID_API_KEY` from injected secrets and `POST`s straight to `https://api.sendgrid.com/v3/mail/send`. There is no outbox row, no suppression check, no quota reservation, no idempotency key, no approval reference, no evidence requirement, no sender-identity check, and no global kill-switch. The `from` address is the hardcoded literal `titan@your-organization.com`.
- **Required behavior:** invariants #1, #4, #5, #6, #7, #8, #10, #11. A model must be structurally incapable of causing delivery. Every send must originate from a leased `outbox_messages` row whose final authorization is re-evaluated by the outbox worker immediately before the provider call.
- **Risk:** a single prompt injection on a crawled page, or one hallucinated tool call, produces real unauthorized email from a real domain — spam complaints, domain reputation loss, and CAN-SPAM/GDPR exposure. This is the single most severe defect in the repository.
- **Solution:** delete the direct-send tool. Replace with `queue_draft_for_approval`, which can only write a `message_drafts` row. Add a repository invariant test that fails CI if any module outside `titan/delivery/outbox_worker.py` imports an email-provider client.
- **Verification:** `tests/invariants/test_no_direct_send_paths.py` (AST scan) + `tests/security/test_model_cannot_send.py`.

### C-02 — SSRF protection is decorative

- **Files:** `apps/api/app/tools/implementations/web_search_tool.py:38-73`, `apps/api/tests/unit/test_ssrf_protection.py`
- **Current behavior:** `_validate_url_for_ssrf` is only ever called on the **hardcoded constant** `https://google.serper.dev/search` (line 79). It never validates attacker-influenced input. The implementation itself is unsound:
  - `socket.gethostbyname()` is **IPv4-only** — any AAAA-only host bypasses it entirely;
  - only the *first* resolved address is checked, so multi-A-record hosts bypass it;
  - no scheme allowlist, no port restriction, no redirect revalidation, no response-size or time bound;
  - classic **TOCTOU**: it resolves the name, then `httpx` resolves it again independently — DNS rebinding wins;
  - `ip_obj.is_reserved` misses `0.0.0.0/8`, IPv4-mapped IPv6 (`::ffff:127.0.0.1`), and the GCP/Alibaba metadata hostnames.
- **Required behavior:** §7.2 in full.
- **Risk:** once a real crawler is added (the entire point of Titan-OS), this validator is the only thing between untrusted lead-supplied URLs and the cloud metadata endpoint. As written it stops none of it.
- **Solution:** new `titan/security/url_guard.py`: scheme allowlist, port allowlist, full `getaddrinfo` sweep with **every** address checked, IPv4-mapped-IPv6 unwrapping, metadata-host denylist, redirect chain revalidation, pinned-IP connection to defeat rebinding, and hard size/time/redirect caps. All network egress for research moves into the isolated browser worker, which holds no credentials.
- **Verification:** `tests/security/test_url_guard.py` (~60 cases) + Hypothesis property test + a live fixture site that 302s to `127.0.0.1`.

### C-03 — The tests assert the broken SSRF behaviour is correct

- **File:** `apps/api/tests/unit/test_ssrf_protection.py`
- **Current behavior:** `test_ssrf_allows_public_ips` makes **real DNS queries** to `google.serper.dev`, `api.github.com`, `www.google.com`. The test is network-dependent, and it green-lights the unsound validator described in C-02 — creating false assurance in CI.
- **Required behavior:** SSRF tests must be hermetic (mocked resolver) and must cover the bypasses, not just the obvious cases.
- **Risk:** the suite reports "CRITICAL SECURITY TEST" passing while the control is bypassable. This is worse than no test.
- **Solution:** replace with a hermetic suite using an injected resolver; add explicit bypass cases (`::ffff:169.254.169.254`, `[::1]`, `0.0.0.0`, decimal-IP literals, `metadata.google.internal`, redirect-to-private).
- **Verification:** new suite must fail against the old validator and pass against the new one — proven by running both.

### C-04 — No transactional outbox exists

- **Files:** entire delivery path (absent)
- **Current behavior:** delivery is `execute_approved_actions(email_draft)` returning the literal `{"status": "success", "crm_id": "crm_456"}` — plus the live SendGrid path in C-01.
- **Required behavior:** §4.5 and invariants #4, #11, #14.
- **Risk:** without an outbox there is no exactly-once boundary. Temporal activity retries, worker crashes, and duplicate webhook deliveries all produce duplicate real emails.
- **Solution:** `outbox_messages` table with `status`, `lease_owner`, `leased_until`, `attempt_count`, `next_attempt_at`, `provider_idempotency_key UNIQUE`, `dedupe_key UNIQUE`. Worker leases with `SELECT ... FOR UPDATE SKIP LOCKED`, re-checks the full authorization chain, reserves quota atomically, sends, records `provider_message_id`.
- **Verification:** `tests/integration/test_outbox_exactly_once.py` — 8 concurrent workers, injected mid-send crashes, asserts provider received each `dedupe_key` exactly once.

### C-05 — No suppression list, unsubscribe, bounce, or complaint handling

- **Files:** absent from schema and code
- **Current behavior:** nothing. `Approval`/`Action` models carry no recipient. There is no way to record that an address must never be contacted again.
- **Required behavior:** §14, §15, invariants #5, #15, #16.
- **Risk:** legally non-compliant (CAN-SPAM §5(a)(3)(A) 10-day honour requirement; GDPR Art. 21 right to object; UK PECR r.22). Any live send is unlawful before this exists.
- **Solution:** `suppression_entries` (workspace + normalized address/domain, reason, source, created_at, immutable, **survives contact deletion**), checked at draft time *and* re-checked in the outbox worker under the same transaction as the quota reservation.
- **Verification:** `tests/integration/test_suppression_race.py` — suppression inserted between lease and send must abort delivery.

### C-06 — No quota system; concurrent workers unbounded

- **Files:** absent
- **Current behavior:** no daily caps at workspace, campaign, sender, or recipient-domain level.
- **Required behavior:** §15.3, invariant #14.
- **Risk:** a discovery run returning 5,000 leads in `controlled_autopilot` sends 5,000 emails in minutes. Instant blocklisting of the sending domain and permanent reputation damage.
- **Solution:** `quota_counters(workspace_id, scope_type, scope_key, window_date, used, limit)` with `UNIQUE(workspace_id, scope_type, scope_key, window_date)`. Reservation is `INSERT ... ON CONFLICT DO UPDATE SET used = used + 1 WHERE used < limit RETURNING used` — a single atomic statement that cannot overshoot regardless of worker count. Exhaustion **defers** (`next_attempt_at` with deterministic jitter), never fails permanently (§15.4).
- **Verification:** `tests/integration/test_quota_concurrency.py` — 32 concurrent reservations against a limit of 10 must yield exactly 10 grants.

### C-07 — Cross-tenant isolation is claimed but structurally unenforced

- **Files:** `apps/api/app/core/dependencies.py`, `apps/api/app/api/tasks.py`, `apps/api/app/api/approvals.py`, `apps/api/tests/integration/test_tenant_isolation.py`
- **Current behavior:** isolation depends entirely on each handler remembering to add `"organizationId": user.organization_id` to a Prisma `where` clause. `get_task_trace` (`tasks.py:88-101`) **forgets the task linkage entirely** and returns the 10 most recent `AgentExecution` rows for the org regardless of `task_id` — the parameter is accepted and discarded, with a comment admitting it. `TenantBaseModel` is defined and never used.
- **Required behavior:** invariant #17; §16 "workspace isolation"; server-side enforcement that cannot be forgotten.
- **Risk:** broken object-level authorization — OWASP API1. One forgotten clause leaks another customer's leads, contacts, and message bodies.
- **Solution:** defence in depth — (a) every table carries `workspace_id NOT NULL`; (b) a `WorkspaceScopedSession` that injects the predicate at the SQLAlchemy `with_loader_criteria` level so it applies even when a handler forgets; (c) PostgreSQL RLS policies as the last line; (d) an invariant test that AST-scans every route for an unscoped query.
- **Verification:** `tests/security/test_cross_workspace_access.py` covering every resource with a foreign-workspace UUID; expects 404 (not 403 — no existence oracle).

### C-08 — `approvals.py` queries a table that does not exist and hides the failure

- **File:** `apps/api/app/api/approvals.py:38-70`
- **Current behavior:** raw SQL against `"ActionRequest"`. **No such model exists in `schema.prisma`.** Both handlers wrap the query in `try/except Exception` and either `return []` or `pass`, with the comments *"Fallback if DB table doesn't exist yet for smooth testing"* and *"Mocking for tests if table missing"*. So `GET /api/approvals` always returns `[]`, and `POST /{id}/decide` **skips the ownership check entirely** on any DB error and proceeds to signal Temporal.
- **Required behavior:** approvals are the human-oversight boundary; failure must be loud and fail-closed.
- **Risk:** **authorization bypass.** The `except` swallows the "does this action belong to your org?" check, then unconditionally sends a Temporal signal using an attacker-supplied `workflow_id` — a cross-tenant workflow-control primitive (invariant #18).
- **Solution:** real `message_approvals` table; ownership verified in the same transaction as the state change; approval token bound to draft ID + version; no bare `except`. Signalling requires the workflow's workspace to match the caller's.
- **Verification:** `tests/api/test_approval_authorization.py` — foreign approval ID → 404; foreign workflow signal → 403; DB error → 500, never success.

### C-09 — `get_db()` opens a transaction per request and is leaked by manual callers

- **Files:** `apps/api/app/core/database.py`, `apps/api/app/api/approvals.py:40,60`
- **Current behavior:** `get_db()` yields `db.tx()` for **every** request including pure reads. Worse, `approvals.py` calls `await anext(get_db())` twice — manually advancing the generator without ever closing it. The `finally`/`__aexit__` of the transaction context never runs, so the transaction is neither committed nor rolled back and the connection is never returned to the pool.
- **Required behavior:** §25 "database transactions kept short"; explicit boundaries; no model/browser/provider calls inside a transaction.
- **Risk:** connection-pool exhaustion under load → total API outage. Long-lived idle-in-transaction sessions block vacuum and can escalate to table bloat.
- **Solution:** SQLAlchemy async sessionmaker; read paths use a plain session, write paths use an explicit `async with session.begin()` unit-of-work. Ban manual generator advancement via a lint rule.
- **Verification:** load test asserting pool checkouts return to baseline; `tests/db/test_session_lifecycle.py`.

### C-10 — Temporal workflows are non-deterministic and unregistered

- **Files:** `apps/api/app/workflows/*.py`
- **Current behavior:**
  - **No worker exists** (`grep "Worker(" → 0 matches`), so none of the four workflows can run in production.
  - `agent_execution_workflow.py:47-52` calls `titan_orchestrator_graph.ainvoke()` **directly inside the workflow** — LangGraph makes network calls. The comment openly says *"In production, wrap ... in an @activity.defn"*. This is a determinism violation that corrupts workflow history on replay.
  - `sales_pipeline_workflow.py:150` reads `getattr(self, "hitl_decision", None)` — the attribute is never initialized in `__init__`, and the signal handler is `async def` (Temporal signal handlers that mutate state should be sync). Replay after worker restart sees a different attribute-presence state.
  - `sales_pipeline_workflow.py` imports `app.core.websocket.manager` **twice**, once inside and once outside `workflow.unsafe.imports_passed_through()`, then calls it from an activity — the activity mutates process-local websocket state, which does not survive a worker move.
  - `wait_condition` on line 150 has **no timeout** — a workflow can hang forever holding a task slot.
  - No `RetryPolicy` on most activities; no heartbeats; no cancellation; no versioning; no workflow queries.
- **Required behavior:** §4.2 in full.
- **Risk:** non-deterministic workflows fail on replay after any deploy, stranding in-flight leads mid-send with no recovery — the exact scenario that produces duplicate or lost emails.
- **Solution:** rewrite. Pure-deterministic workflow bodies; every I/O in an activity; typed dataclass args; per-category retry policies; heartbeats on crawl activities; approval via signal + query with expiry timer; `workflow.patched()` versioning; real worker entrypoints per task queue.
- **Verification:** `tests/workflows/test_replay_determinism.py` using `Replayer` against recorded histories; `tests/workflows/test_retry_no_duplicate_events.py`.

### C-11 — CI masks every failure with `|| true`

- **File:** `.github/workflows/ci-cd.yml:47-63`
- **Current behavior:** `ruff check`, `black --check`, `mypy`, `coverage run -m pytest`, and `coverage report` are **each** suffixed with `|| true`. The job is green whether the code compiles, type-checks, lints, or passes tests. Git history confirms intent: commit `3d4fb21` — *"ci: remove redundant vercel-deploy.yml to enforce 100% green GitHub checks"*.
- **Required behavior:** §22.
- **Risk:** CI provides zero signal. Every quality claim derived from a green badge is false — including the badge in `README.md` line 4.
- **Solution:** remove every `|| true`; add secret scanning (gitleaks), dependency audit (`pip-audit` + `pnpm audit`), migration validation, OpenAPI drift check, generated-schema drift check, and the repository invariant suite. Pin actions to commit SHAs.
- **Verification:** deliberately introduce a lint error and a failing test on a scratch branch; CI must go red for both.

### C-12 — `next.config.ts` disables TypeScript build errors

- **File:** `apps/web/next.config.ts:6-8`
- **Current behavior:** `typescript: { ignoreBuildErrors: true }`.
- **Required behavior:** §29 "Type checking passes".
- **Risk:** the dashboard ships with unknown type errors; the CI `tsc --noEmit` step is the only check and it runs *after* `pnpm lint`, which itself has no `--max-warnings 0`.
- **Solution:** remove the flag; fix resulting errors; add `tsc --noEmit` as a blocking gate.
- **Verification:** `pnpm exec tsc --noEmit` exits 0 with the flag removed.

### C-13 — Provider configuration is never delivered to the runtime

- **Files:** `apps/api/app/core/config.py`, `deploy/.env.example`, `docker-compose.yml`
- **Current behavior:** `Settings` declares exactly five fields: `ENVIRONMENT`, `FRONTEND_URL`, `DATABASE_URL`, `CLERK_ISSUER_URL`, `CLERK_JWKS_URL`. `deploy/.env.example` declares 20+ variables (`OPENAI_API_KEY`, `SENDGRID_API_KEY`, `TITAN_ENCRYPTION_KEY`, `HUBSPOT_*`, `SLACK_*`, `TEMPORAL_HOST`, `REDIS_URL`, `OTEL_*`) that `Settings` cannot see. Root `docker-compose.yml` defines **only** postgres/redis/temporal — no API, worker, or web service — so no container receives any of it. Code reads some of these via bare `os.getenv(...)` with silent `""` defaults (`tools/security.py`, `core/events.py`, `core/auth.py`).
- **Required behavior:** §23 — "Do not repeat the previous defect where provider settings existed in `.env` but were not supplied to the running API or worker."
- **Risk:** silent misconfiguration. `inject_secrets` returns `{"SENDGRID_API_KEY": ""}`, the tool fails at runtime with a confusing message, and — for security-relevant values like `CLERK_ISSUER_URL` — an empty default means auth is *misconfigured rather than refused*.
- **Solution:** one exhaustive `Settings` model with **no silent defaults for security-relevant values**; a startup validator that fails closed and prints exactly which variables are missing; `.env.example` generated from the model so drift is impossible; a CI check that every `Settings` field appears in every compose service that needs it.
- **Verification:** `tests/config/test_env_parity.py` — asserts `Settings` fields ≡ `.env.example` keys ≡ compose environment blocks.

### C-14 — No evidence model, so every claim would be fabricated

- **Files:** `packages/db/prisma/schema.prisma`
- **Current behavior:** the schema has no table capable of storing a page, an artifact, a finding, a selector, an observed value, or a claim→evidence link. The only "score" in the codebase is the hardcoded `85`, and the only "reasoning" is the hardcoded string *"High match with ICP. Recent funding round detected."* — a fabricated business claim about a fictional company.
- **Required behavior:** §7.5, §12.2, invariant #7 — "no evidence means no pitchable claim."
- **Risk:** this is the product's entire differentiation. Without it, Titan-OS is a mail-merge that invents facts about real businesses — reputationally and legally indefensible.
- **Solution:** `pages`, `browser_artifacts`, `audit_findings`, `finding_evidence` (immutable, content-fingerprinted excluding volatile fields per §7.4), plus a `claim_evidence_links` join enforcing the `sentence → claim → finding → evidence → source page` chain, validated before any draft can be approved.
- **Verification:** `tests/intelligence/test_message_evidence_validator.py` — a draft with an unlinked claim is rejected; fixture-site corpus asserts expected findings and expected **non**-findings.

---

## 4. HIGH findings

| ID | Finding | Files | Current → Required | Risk | Solution | Verification |
|---|---|---|---|---|---|---|
| H-01 | No lead-discovery layer | absent | none → Google Places Text Search + Place Details behind a `LeadSourceAdapter` with field masks, pagination, dedupe, cost accounting | product cannot start its own pipeline | provider adapter + `lead_sources` table | live-key contract test + recorded-cassette test |
| H-02 | No website research engine | absent | none → bounded crawl in isolated worker | core capability missing | Phase 2 browser worker | fixture-site suite |
| H-03 | No campaign / campaign-policy entity | schema | none → first-class `campaigns` + `campaign_policies` | policy cannot be persisted, so §3 modes are unenforceable | Phase 1 schema | invariant #18 test |
| H-04 | No operating modes | absent | none → 4 modes, fail-closed | autopilot cannot be gated | Phase 3 policy engine | `tests/policy/test_operating_modes.py` |
| H-05 | No contact model or verification | schema | none → `contacts`, `contact_channels`, `contact_verifications` with provenance | guessed addresses become sendable (invariant #6) | Phase 1 + Phase 4 eligibility rules | `tests/intelligence/test_contact_eligibility.py` |
| H-06 | Prisma Python cannot express required primitives | `packages/db`, `core/database.py` | Prisma → SQLAlchemy 2.0 + Alembic | `FOR UPDATE SKIP LOCKED`, partial unique indexes, `ON CONFLICT DO UPDATE ... WHERE`, and CTEs are all required by the outbox/quota design and are unsupported or unreliable in prisma-client-py (which is also effectively unmaintained) | full migration | migrations run from zero + upgrade path |
| H-07 | cuid primary keys, not UUID | `schema.prisma` | `@default(cuid())` → `uuid_generate_v7()`/`gen_random_uuid()` | §5 explicitly requires UUID PKs; cuid loses index locality and cross-system portability | Phase 1 | schema assertion test |
| H-08 | No idempotency keys anywhere | schema + API | none → `Idempotency-Key` on all mutations, `provider_idempotency_key` on outbox | duplicate sends and duplicate state | Phase 1 + Phase 8 | `tests/api/test_idempotency.py` |
| H-09 | No optimistic locking | schema | none → `version` column + `WHERE version = :v` | lost updates on concurrent draft edits | Phase 1 | concurrency test |
| H-10 | No append-only audit trail | `AuditLog` exists but unused | unused table → enforced writes on all sensitive mutations (§18) | no accountability for enabling sending, changing quotas, editing suppression | Phase 1 + service-layer hook | `tests/audit/test_sensitive_actions_logged.py` |
| H-11 | RBAC defined but never applied | `core/dependencies.py` | `require_role` used on **0 routes** | any authenticated org member can do anything | apply per-route; roles owner/admin/researcher/reviewer/operator/viewer | `tests/api/test_rbac_matrix.py` |
| H-12 | Role trusted from JWT claim only | `core/auth.py:104` | `payload.get("org_role")` → server-side `workspace_members` lookup | a re-issued or stale token grants stale privileges; no revocation path | Phase 8 | `tests/api/test_role_source_of_truth.py` |
| H-13 | No model gateway; no provider abstraction | `agents/*` | LangChain-direct → typed gateway with adapters | §9.1 requires NVIDIA/Gemini/OpenRouter/Cloudflare to be swappable; model IDs must be validated, not assumed | Phase 6 | `titan validate-models` against live catalogues |
| H-14 | No structured model outputs | `agents/schemas.py` | prose parsing → strict Pydantic schemas + bounded repair | hallucinated fields silently enter business logic | Phase 6 | `tests/models/test_structured_output_validation.py` |
| H-15 | No cost controls | absent | none → per-workspace/campaign/lead budgets, ledger, circuit breaker, hard stop | unbounded spend on a runaway crawl | Phase 6 `usage_ledger` | `tests/models/test_budget_enforcement.py` |
| H-16 | Prompt-injection defence is a substring denylist | `security/guardrails.py:16-24` | 7 lowercase substrings → architectural separation of system/policy/evidence/untrusted-content channels | trivially bypassed (`"ign​ore previous instructions"`, base64, translation, homoglyphs). Also **false-positives on legitimate input**: the phrase `"system prompt"` is blocked, so a user asking about their own system prompts is refused | Phase 6 channel isolation + capability restriction (models get no tools that can act) | `tests/security/test_prompt_injection_fixtures.py` against the fixture corpus |
| H-17 | Webhook handling absent; no signature verification | `integrations/webhooks.py` | — → Svix-style verification, raw-event preservation, dedupe by provider event ID, monotonic state | forged webhooks can mark bounced mail delivered, or suppress arbitrary addresses | Phase 5 | `tests/delivery/test_webhook_forgery.py`, `test_webhook_ordering.py` |
| H-18 | No reply ingestion or classification | absent | — → 15-class classifier + immediate stop-on-reply | invariant #15 unenforceable; continuing to mail someone who replied is the top complaint driver | Phase 5 | `tests/delivery/test_stop_on_reply.py` |
| H-19 | No follow-up engine | absent | — → stateful sequence with 11 hard stop conditions | same as H-18 | Phase 5 | sequence state-machine tests |
| H-20 | Dashboard renders fabricated analytics | `apps/web/src/lib/demoMode.ts`, `demo_seeded_data.json`, `components/dashboard/*` | hardcoded `"High-Value Lead Scored (94/100)"`, `"Acme Corp ($150k ARR Potential)"`, `"FinanceBot requested wire transfer $45,000"` rendered as live activity → real API data with explicit empty states | §17 "Do not display fake analytics"; an operator cannot distinguish demo from production | Phase 8 rebuild; demo data only behind an explicit `TITAN_DEMO_MODE` banner | visual + `tests/web/test_no_fabricated_data.spec.ts` |
| H-21 | README advertises unbuilt features as complete | `README.md` | claims RAG, Qdrant, "Full Observability", "16-step Golden Path ensuring production readiness", CI badge → honest status matrix | §26 "Do not advertise planned features as complete"; the CI badge is actively misleading given C-11 | Phase 9 rewrite with Implemented/Tested/Live-verified labels | doc review |

---

## 5. MEDIUM findings

| ID | Finding | Files | Detail & solution |
|---|---|---|---|
| M-01 | `get_task_trace` ignores `task_id` | `api/tasks.py:88-101` | Returns org-wide executions. Also a data-leak vector within an org across unrelated tasks. Implement real trace linkage. |
| M-02 | `cancel_task` never cancels | `api/tasks.py:63-86` | Sets DB status to `CANCELLED`; comment says *"Trigger temporal cancellation here..."*. The workflow keeps running and can still act. Wire real cancellation. |
| M-03 | Temporal client reconnected per request | `api/approvals.py:76` | `await Client.connect("localhost:7233")` inside the handler, with a **hardcoded host** ignoring `TEMPORAL_HOST`. Adds handshake latency to every approval and breaks in Docker. Use a lifespan-managed singleton. |
| M-04 | `datetime.utcnow()` is deprecated and naive | `core/events.py:24` | Python 3.12+ deprecation; naive UTC timestamps corrupt event ordering across DST/timezone boundaries. Use `datetime.now(timezone.utc)`. |
| M-05 | Event ID doubles as workflow ID without dedupe policy | `core/events.py:60-66` | `id=f"orchestrator-{event.event_id}"` with default `WorkflowIDReusePolicy`; a client-supplied `event_id` can collide or be replayed. Use a server-derived deterministic ID + explicit reuse policy. |
| M-06 | Client-supplied `event_id` written as DB primary key | `api/events.py:31-38` | Enables ID squatting and cross-tenant probing via PK collision. Generate server-side; keep client value as `external_id`. |
| M-07 | `BackgroundTasks` used for durable dispatch | `api/events.py:14,42` | If the process dies between DB write and dispatch, the event is silently lost — there is no retry and no dead-letter. The `EventDispatcher.dispatch` docstring even says *"Fallback/Dead-letter queue logic would go here"*. Replace with an outbox-style dispatch table. |
| M-08 | Broad `except Exception` swallowing | `api/approvals.py`, `core/events.py`, `tools/*` | Masks real failures as success or empty results. Replace with typed error taxonomy; never `pass`. |
| M-09 | WebSocket auth passes JWT in query string | `main.py:41` | Tokens land in access logs, proxy logs, and browser history. Use a short-lived ticket exchanged over the authenticated HTTP channel. |
| M-10 | WebSocket state is process-local | `core/websocket.py`, `websocket_manager.py` | Two near-duplicate managers exist. Neither survives multi-replica deployment; broadcasts reach only the connected replica. Use Redis pub/sub fan-out. |
| M-11 | CORS `allow_methods=["*"]`, `allow_headers=["*"]` with credentials | `main.py:26-32` | Overly permissive; tighten to the actual method/header set. |
| M-12 | Docker Compose is incomplete and partly wrong | `docker-compose.yml` | Obsolete `version: '3.9'`; **no API, worker, browser-worker, or web service**; port `8233` mapped on the `temporalio/auto-setup` container, which does not serve the Web UI (that is the separate `temporalio/ui` image) — so the documented UI URL cannot work; no healthchecks; no `depends_on: condition: service_healthy`; Temporal shares the application Postgres. Rebuild per §23. |
| M-13 | No migration system at all | repo | Prisma `db push` only (`package.json`). No migration files, no history, no rollback, no upgrade path. Adopt Alembic with a tested zero→head path *and* an upgrade path from the existing schema. |
| M-14 | Duplicated contract types with no generation | `packages/contracts` (Python) vs `packages/shared` (TS) | Hand-maintained parallel definitions guarantee drift. Generate TS from the Pydantic models (or from OpenAPI) and CI-check for drift. |
| M-15 | Duplicate scripts and duplicate observability modules | `scripts/debug-ci.sh` + `scripts/debug_ci.sh`; `observability/tracing.py` + `tracer.py` + `temporal_tracer.py` + `langgraph_tracer.py` + `langgraph_callback.py`; `core/tracing.py` | Six overlapping tracing modules and two identical scripts. Consolidate to one OTel setup module. |
| M-16 | Rate limiting is in-memory per process | `core/rate_limiter.py` | `slowapi` default storage does not coordinate across replicas; limits multiply by replica count. Back with Redis. |
| M-17 | `mypy` config disables 7 error codes | `pyproject.toml` | `attr-defined`, `misc`, `assignment`, `override`, `var-annotated`, `arg-type`, `no-redef` are all disabled — which removes most of mypy's value. Re-enable incrementally with `strict` on new `titan/` packages. |

---

## 6. LOW findings

| ID | Finding | Files |
|---|---|---|
| L-01 | README screenshots are `placehold.co` placeholders with the literal caption *"(Replace these placeholders with actual screenshots)"* | `README.md` |
| L-02 | Root `package.json` version `0.1.0` while the mission and docs say v0.2.0 | `package.json` |
| L-03 | `pnpm-workspace.yaml` `allowBuilds` values are the literal placeholder string `set this to true or false` | `pnpm-workspace.yaml` |
| L-04 | Both `netlify.toml` and `vercel.json` present after the migration to Vercel; `.netlify/` directory still tracked | root, `apps/web/.netlify` |
| L-05 | `apps/web/package-lock.json` coexists with `pnpm-lock.yaml` in a pnpm workspace | `apps/web` |
| L-06 | No `LICENSE` file although README claims MIT | root |
| L-07 | `.gitignore` misses `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `*.egg-info` is present but `.coverage.*` shards are not | `.gitignore` |
| L-08 | Deploy job uses `continue-on-error: true` on the production SSH step — a failed deploy reports success | `.github/workflows/ci-cd.yml` |
| L-09 | `pyproject.toml` has no pinned dependency versions (all `>=` or bare) — non-reproducible builds despite `uv.lock` existing | `apps/api/pyproject.toml` |

---

## 7. Already correct — keep

| ID | Item | Notes |
|---|---|---|
| K-01 | `core/security_headers.py` | Correct middleware shape; extend with HSTS/CSP/permissions-policy. |
| K-02 | `security/redaction.py` | Sound approach; reuse as the basis for the structured-logging redactor (§19). |
| K-03 | Monorepo layout (`apps/*`, `packages/*`, turbo, pnpm workspace) | Good structure; keep and extend with `apps/browser-worker`. |
| K-04 | Temporal chosen as the durability layer | Correct technology choice for §4.2; the *implementation* is what needs rewriting. |
| K-05 | pgvector Postgres image already in compose | Useful for future evidence-similarity work; keep. |
| K-06 | JWKS caching with a 1-hour TTL (`core/auth.py:30`) | Correct pattern; keep the cache, fix the trust model (H-12). |
| K-07 | No secrets committed; caches/venv correctly untracked | Verified by scan. Maintain with CI secret scanning (C-11). |

---

## 8. Intentionally deferred

| ID | Item | Reason |
|---|---|---|
| D-01 | Migrating off Clerk to self-hosted auth | Clerk is a reasonable production choice. The gap is *trusting the token's role claim* (H-12), which is fixed without replacing the provider. Revisit only if self-hosting is required. |
| D-02 | Qdrant / dedicated vector DB | pgvector is already available and sufficient at the expected corpus size. Removing the Qdrant claim from the README (H-21) resolves the honesty gap; adding a second datastore would violate §4.4's single-source-of-truth rule. |
| D-03 | Twenty CRM / Cal.com / PostHog / Langfuse integrations | Studied for patterns (see `docs/research/UPSTREAM-ENGINEERING-RESEARCH.md`); native `meetings`/`tasks` tables ship first. External sync is a post-v1 adapter. |
| D-04 | Auto-sending conversational replies | §14 explicitly forbids it without a separate policy. Replies are drafted and surfaced as tasks only. |
| D-05 | Email-verification provider integration (e.g. bounce-prediction APIs) | Interface and `contact_verifications` table ship; the concrete paid provider requires an account (§30.3). First-party published addresses work without it. |
| D-06 | Multi-region / HA Temporal | Single-cluster is appropriate for current scale; documented in `docs/DEPLOYMENT.md` as a scaling step. |

---

## 9. Invariant baseline (§28)

| # | Invariant | Baseline status | Evidence |
|---|---|---|---|
| 1 | A model cannot send email | ❌ **Violated** | C-01 |
| 2 | Browser content cannot alter policy | ❌ Unenforced | H-16; no policy engine exists |
| 3 | Arbitrary crawling only in isolated worker | ❌ Unenforced | C-02; no worker exists |
| 4 | No send without an outbox row | ❌ **Violated** | C-01, C-04 |
| 5 | No send to a suppressed recipient | ❌ **Violated** | C-05 |
| 6 | No send to a guessed email | ❌ Unenforced | H-05 |
| 7 | No send without evidence-backed claims | ❌ Unenforced | C-14 |
| 8 | No send when globally disabled | ⚠️ Incidental only | no kill-switch exists; sending fails only because no key is set |
| 9 | No send when campaign paused | ❌ Unenforced | H-03 |
| 10 | No send without sender authorization | ❌ Unenforced | C-01 (hardcoded `from`) |
| 11 | Retry cannot duplicate an email | ❌ **Violated** | C-04, C-10 |
| 12 | Duplicate webhook cannot duplicate state | ❌ Unenforced | H-17 |
| 13 | Delayed webhook cannot regress state | ❌ Unenforced | H-17 |
| 14 | Concurrent workers cannot exceed quota | ❌ **Violated** | C-06 |
| 15 | A replied lead gets no follow-up | ❌ Unenforced | H-18 |
| 16 | Bounce/complaint → suppression | ❌ Unenforced | C-05 |
| 17 | No cross-workspace read/mutate | ⚠️ Bypassable | C-07, C-08 |
| 18 | Request cannot override persisted policy | ❌ Unenforced | H-03 |
| 19 | API keys never in logs or responses | ⚠️ Partial | `redaction.py` exists but is not wired into logging |
| 20 | LeadPilot is not a runtime dependency | ✅ **Satisfied** | no imports found |
| 21 | Production sending disabled by default | ⚠️ Incidental only | see #8 |
| 22 | Research/draft modes work without email auth | ⚠️ Vacuous | neither mode exists |

**Score: 1 satisfied, 5 partial/incidental, 16 unenforced or violated.**

---

## 10. Risky migration register

Recorded before any schema work begins, per §27 Phase 0.

| Risk | Mitigation |
|---|---|
| Prisma → SQLAlchemy is a full persistence swap | The baseline schema holds **no production data** (no migration files ever existed; `db push` only). A clean-cut migration is therefore safe. An `alembic` baseline revision represents the legacy shape so an upgrade path is still testable (§29). |
| cuid → UUID PK change | New tables use UUID from revision 1. Legacy tables are dropped in the same revision rather than converted, since no data exists. |
| `Unsupported("vector(1536)")` column | Requires `CREATE EXTENSION vector` before the table. Handled explicitly in the first Alembic revision rather than the comment-only instruction currently at the bottom of `schema.prisma`. |
| Dropping the direct-send email tool | Any external caller of `send_email` breaks by design. Grep confirms **no callers outside the registry** — the tool is dead code today, which lowers the risk to zero while the security benefit is maximal. |
| Temporal task-queue rename | No workflows are currently running (no worker exists), so renaming `titan-task-queue` → `titan-research`/`titan-models`/`titan-delivery`/`titan-maintenance` strands nothing. |

---

## 11. Implementation checklist

Ordered per §27. Each phase gates the next.

- [x] **Phase 0** — Baseline captured; 19 tests confirmed passing; secret scan clean; this document.
- [ ] **Phase 1** — SQLAlchemy 2.0 + Alembic; full §5 schema; workspace isolation; event idempotency; evidence identity; lead dedupe; repository tests.
- [ ] **Phase 2** — `url_guard`; isolated Playwright browser worker; Lighthouse + axe-core; fixture sites; evidence fingerprinting; browser tests.
- [ ] **Phase 3** — Policy engine; 4 operating modes; send-authorization chain; fail-closed defaults.
- [ ] **Phase 4** — Playbooks (8); findings; opportunity mapping; deterministic scoring; contact eligibility; evidence-backed message generation + validator.
- [ ] **Phase 5** — Outbox; atomic quotas; Resend adapter; webhook verification + ordering; suppression; reply classification; follow-up cancellation.
- [ ] **Phase 6** — Model gateway + 4 adapters; typed outputs; cost ledger; circuit breakers; Google Places; Agent Reach.
- [ ] **Phase 7** — Temporal workflows + activities + workers; retries; heartbeats; approval signals/queries; replay tests.
- [ ] **Phase 8** — API v1; RBAC; idempotency; dashboard (campaign builder, lead workspace, evidence viewer, approval queue, settings).
- [ ] **Phase 9** — Observability; Docker Compose stack; hardened CI; invariant tests; docs; final verification report.

---

## 12. Scope note

This is a large, multi-phase rebuild of the domain, delivery, and safety layers of an existing repository. Phases are committed independently so each is reviewable. Any phase not completed within this pass is reported as **not implemented** in `docs/audits/FINAL-PRODUCTION-VERIFICATION.md` — never as complete. Live-provider behaviour (Resend delivery, Google Places quota, model catalogues) is labelled **Not yet live-verified** until a real credentialled call is made and its output recorded.
