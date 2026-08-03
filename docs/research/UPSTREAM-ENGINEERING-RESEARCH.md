# Titan-OS — Upstream Engineering Research & Architectural Inspiration

**Document version:** 0.2.0  
**Last updated:** 2026-08-03  
**Status:** Active Reference  

This document records the architectural patterns, safety controls, and design precedents studied from mature open-source systems, enterprise frameworks, and official platform specifications to inform the architecture of Titan-OS.

---

## 1. Summary of Upstream Inspirations

| Project / Pattern | Domain / Category | Primary Architectural Lesson Adopted | What Titan Intentionally Omitted |
|---|---|---|---|
| **Temporal Python SDK** | Durable Execution | Explicit activity boundaries, deterministic replay semantics, signal/query HITL approvals | Direct network calls in workflow functions; non-deterministic state |
| **FastAPI & Pydantic v2** | API & Type Safety | Strict request/response validation, OpenAPI spec generation, custom field validators | Mutable settings models; implicit query parameters without explicit typing |
| **SQLAlchemy 2.0 & psycopg** | Persistence & Outbox | Explicit unit-of-work transactions, `SELECT ... FOR UPDATE SKIP LOCKED` for outbox leasing | ORM cascade deletes on compliance data; un-bounded sessions |
| **Transactional Outbox Pattern** | Delivery Safety | Exactly-once logical delivery boundary, atomic quota reservation before send | Microservice queue dependencies for single-monolith setups |
| **Playwright & axe-core & Lighthouse** | Browser Evidence | Isolated, credential-free execution sandbox, structured DOM & network evidence extraction | Form submission, interactive authentication, CAPTCHA bypass |
| **Google Places API v1** | Lead Discovery | Field masks, Place ID deduplication, structured location & rating filtering | Caching raw Places responses beyond allowed retention policies |
| **Resend SDK & Webhook Verification** | Email Delivery | Svix-compatible webhook signature verification, provider idempotency keys | Direct client calls from model tools or web route handlers |
| **Twenty CRM & Cal.com** | CRM & Scheduling | Normalized contact, organization, task, and meeting data models | Overly complex multi-tenant routing for single-workspace operations |
| **OpenTelemetry & Prometheus** | Observability | Structured log context propagation, outbox depth & send rate metric counters | High-cardinality label explosion (e.g. raw recipient emails in metric tags) |
| **Agent Reach** | Lead Enrichment | Provenance-tagged external channel discovery with confidence scores | Autonomous multi-step outreach without policy engine approval |

---

## 2. Deep Dive: Upstream Patterns & Titan Adaptations

### 2.1 Temporal Python SDK — Durable Workflow Execution
- **Pattern Studied:** Deterministic state machine execution with durable activity retry policies and signal-based human-in-the-loop (HITL) gates.
- **Why Relevant:** Titan-OS requires multi-step research, analysis, drafting, approval, and sending pipelines that survive worker restarts, infrastructure crashes, and network timeouts.
- **What Titan Adopted:**
  - Strict separation between deterministic workflow code and activity I/O.
  - Task queue isolation (`titan-research`, `titan-models`, `titan-delivery`, `titan-maintenance`).
  - Durable approval signals (`approve`, `reject`, `re_research`) paired with query methods for approval status.
- **What Titan Intentionally Did Not Adopt:**
  - In-workflow LLM execution or direct HTTP calls (a common anti-pattern in naive workflow scripts). All external calls are encapsulated strictly inside activities.

### 2.2 Transactional Outbox & Atomic Quota Reservation
- **Pattern Studied:** Transactional Outbox pattern combined with atomic database row update locks (`FOR UPDATE SKIP LOCKED`).
- **Why Relevant:** Prevents duplicate email sends under retry, crash, or concurrent worker execution conditions.
- **What Titan Adopted:**
  - PostgreSQL table `outbox_messages` with unique provider idempotency keys (`provider_idempotency_key`).
  - Atomic quota reservation via `INSERT INTO quota_counters ... ON CONFLICT DO UPDATE SET used = used + 1 WHERE used < limit`.
  - Final authorization re-check by the outbox worker immediately before invoking the email provider.
- **What Titan Intentionally Did Not Adopt:**
  - Asynchronous message brokers (Kafka/RabbitMQ) between control plane and outbox worker. Direct PostgreSQL polling with `SKIP LOCKED` minimizes infrastructure complexity while guaranteeing strong transactional consistency.

### 2.3 Browser Evidence Sandbox (Playwright + axe-core)
- **Pattern Studied:** Headless browser automation in an isolated, network-restricted container.
- **Why Relevant:** Web research must be safe against SSRF attacks, browser escapes, and prompt injection embedded in target pages.
- **What Titan Adopted:**
  - TypeScript Playwright worker (`apps/browser-worker`) completely isolated from email, model, and database credentials.
  - Dual-layer SSRF validation (`titan/security/url_guard.py` on control plane, `urlGuard.ts` in browser worker).
  - Evidence collection capturing static DOM nodes, CTA targets, accessibility violations (axe-core), and console logs.
- **What Titan Intentionally Did Not Adopt:**
  - Form filling, automated login, or CAPTCHA solving. Titan-OS strictly observes and audits; it never acts on behalf of a user on external websites.

### 2.4 Resend SDK & Webhook Processing
- **Pattern Studied:** Immutable provider event records with cryptographic signature verification.
- **Why Relevant:** Delivery updates (bounces, complaints, unsubscribes) must drive suppression without vulnerability to forged webhooks or state regression.
- **What Titan Adopted:**
  - Webhook signature verification enforcing timestamp tolerance windows.
  - Provider event deduplication via `provider_events` table using `(provider, provider_event_id)` unique constraint.
  - State machine guard preventing delayed `sent` events from overwriting terminal states (`bounced`, `complained`, `unsubscribed`).
- **What Titan Intentionally Did Not Adopt:**
  - Direct model tool execution of email sends.

### 2.5 Google Places API & Lead Provenance
- **Pattern Studied:** Field-masked places search with Place ID deduplication.
- **Why Relevant:** Provides clean, structured business discovery with verifiable public contact pointers.
- **What Titan Adopted:**
  - Google Places Text Search & Details adapter using official FieldMask headers (`places.id`, `places.displayName`, `places.websiteUri`, `places.nationalPhoneNumber`).
  - Strict deduplication on Place ID and canonical domain.
  - Provenance tracking attributing every lead to its original discovery source.
- **What Titan Intentionally Did Not Adopt:**
  - Storing raw Google Places data beyond permitted caching policies or attempting to scrape unlisted data.

---

## 3. Conclusion

By extracting proven design patterns from these mature systems and adapting them to Titan-OS's evidence-first principles, Titan-OS achieves enterprise-grade durability, strict security isolation, and compliance guarantees without unnecessary architectural bloat.
