# Titan-OS

**Evidence-first sales intelligence, website research, lead qualification, and
controlled outreach.**

Titan-OS observes before it pitches, proves before it claims, and refuses to
send when it cannot show its working. A model can draft a message; only the
outbox worker can deliver one, and only after every gate has passed.

> **Status: working build, incomplete against the mission (v0.2.0).** Phases 0–8
> are done: the safety substrate, the delivery chokepoint, discovery, the model
> gateway, durable workflows, the `/api/v1` surface with authentication and
> RBAC, and the operator CRM. The follow-up scheduler, inbound reply
> classification, an Agent Reach adapter, OTel metrics and the retention jobs are
> **not**.
> [`docs/audits/FINAL-PRODUCTION-VERIFICATION.md`](docs/audits/FINAL-PRODUCTION-VERIFICATION.md)
> states exactly what runs, what was tested, and what is missing.
> Do not enable production sending against this build: without the follow-up and
> reply paths it contacts each lead once, and no deliverability item has been
> verified against a real sending domain.

---

## Status by component

Labels mean what they say. **Live-verified** means a real credentialled call was
made and its output recorded — as of 2026-08-03 the model gateway and Google
Places have reached that bar, and nothing else has.

| Component | Status |
|---|---|
| PostgreSQL schema, migrations, workspace isolation | Implemented · integration-tested |
| SSRF guard (Python + TypeScript) | Implemented · unit + property-tested |
| Isolated browser evidence worker | Implemented · unit-tested · crawl path not verified here |
| Fixture sites (6, with known non-defects) | Implemented |
| Policy engine + 4 operating modes | Implemented · 51 tests |
| Findings, scoring, playbooks, contact eligibility, message validator | Implemented · 73 tests |
| Transactional outbox, atomic quotas, suppression | Implemented · integration-tested under concurrency |
| Resend adapter + webhook verification/ordering | Implemented · **not live-verified by choice** |
| Smartlead adapter (single-step carrier campaign) + campaign management | Implemented · unit-tested · **not live-verified** |
| Structured logging with redaction | Implemented |
| Docker Compose stack, CI, operator CLI | Implemented · stack run end-to-end, 8 services healthy |
| Google Places discovery | Implemented · **live-verified** |
| Model gateway (NVIDIA / OpenRouter / Cloudflare routes) | Implemented · **live-verified** |
| Temporal workflows and worker | Implemented · tested against a real Temporal server |
| `/api/v1` resource surface, authentication, RBAC | Implemented · 38 integration tests |
| Operator CRM (`/crm`) | Implemented · browser-verified against the running stack |
| Agent Reach discovery | **Not implemented** |
| Follow-up scheduler | **Not implemented** — the largest remaining gap: one message per lead |
| Inbound reply ingestion and classification | **Schema + stop-on-reply only**; replies must be recorded by hand |
| Metrics and tracing (OTel/Prometheus) | **Not implemented** — structured logging only |
| Retention/purge jobs | **Schema only** |
| Legacy demo dashboard (`/dashboard`) | **Superseded by `/crm`** — still renders fabricated data behind a DEMONSTRATION DATA banner |

**477 Python tests and 14 TypeScript tests pass** (the Python figure excludes the
18 workflow tests, which need a Temporal test server). Commands and per-directory
counts are in the verification report.

---

## What makes it different

- **No evidence, no claim.** Every sentence asserting a fact about a recipient's
  business must trace through a stored claim map to a finding backed by an
  immutable browser artifact. The validator rejects the draft otherwise.
- **A model cannot send.** The only module permitted to hold an email provider
  client is the outbox worker. An AST scan in CI fails the build if that changes.
- **Four independent gates on delivery**, plus a per-message policy evaluation
  that runs *again* immediately before the provider call — so pausing a campaign
  or receiving a reply stops mail that is already queued.
- **Guessed addresses are structurally ineligible.** `pattern_guess` is absent
  from the eligible-source set in code, so a misconfigured campaign policy cannot
  permit one.
- **Exactly-once delivery**, proven with 8 concurrent workers and an injected
  mid-send crash.
- **A campaign platform cannot widen the blast radius.** Titan can deliver
  through Smartlead, but only via a carrier campaign it checks has exactly one
  sequence step — a second step would send mail no gate here ever evaluated, so
  the adapter refuses to run against such a campaign.

---

## Architecture

```
apps/api/titan/
  config.py          exhaustive settings; no os.getenv anywhere else
  db/                SQLAlchemy 2.0 models, Alembic migrations, scoped sessions
  security/          SSRF guard, redaction
  policy/            operating modes + the send-authorization decision
  intelligence/      findings, scoring, playbooks, contacts, message validation
  delivery/          outbox worker, quotas, suppression, providers, webhooks
  api/               health, readiness, sending preflight
  workers/           outbox worker entrypoint
apps/browser-worker/ isolated Playwright service (holds no credentials)
```

The browser worker is the only component that fetches attacker-controlled URLs,
and it holds no database, email, or model credentials — a full browser escape
yields nothing that can send mail or read tenant data.

---

## Local setup

```bash
# 1. Infrastructure
docker compose up -d postgres

# 2. Backend
cd apps/api
uv venv --python 3.11 && uv pip install -e ".[dev]"
export TITAN_DATABASE_URL="postgresql+psycopg://titan:titan_dev_password@localhost:5432/titan"
python -m alembic upgrade head

# 3. Verify
python -m pytest tests -q          # 274 passed
python -m titan.cli preflight      # explains why sending is disabled
python -m titan.cli invariants     # the 22 safety invariants and where each lives

# 4. Browser worker
cd ../browser-worker
npm install && npx playwright install chromium
npx tsc -p tsconfig.json --noEmit
npm test
```

Full-stack `docker compose up` builds the API, outbox worker, browser worker,
and web app. The Temporal services start but **no Titan worker registers against
them** — Phase 7 is not implemented, and a service pointing at a nonexistent
module would be fiction.

---

## Safety posture

Production sending is disabled at four independent levels and defaults to off at
every one. `python -m titan.cli preflight` reports which gates are closed:

```
PROCESS GATE CLOSED: 4 blocker(s).
  - TITAN_PRODUCTION_SENDING_ENABLED is false (global kill switch)
  - TITAN_EMAIL_PROVIDER is 'mock'; no real provider configured
  - TITAN_EMAIL_AUTH_PREFLIGHT_ACKNOWLEDGED is false (SPF/DKIM/DMARC not acknowledged)
  - TITAN_SENDER_MAILING_ADDRESS is not set
```

Enabling outreach is a deliberate, documented operator action:
[`docs/PRODUCTION-ENABLEMENT-CHECKLIST.md`](docs/PRODUCTION-ENABLEMENT-CHECKLIST.md).

---

## Documentation

| Document | Contents |
|---|---|
| [`docs/audits/PRODUCTION-GAP-ANALYSIS.md`](docs/audits/PRODUCTION-GAP-ANALYSIS.md) | The audit that motivated this rebuild: 74 findings against the pre-0.2 code |
| [`docs/audits/FINAL-PRODUCTION-VERIFICATION.md`](docs/audits/FINAL-PRODUCTION-VERIFICATION.md) | What was executed, what passed, what is missing, residual risks |
| [`docs/PRODUCTION-ENABLEMENT-CHECKLIST.md`](docs/PRODUCTION-ENABLEMENT-CHECKLIST.md) | External setup required before live outreach |
| [`docs/security/THREAT-MODEL.md`](docs/security/THREAT-MODEL.md) | Threats and mitigations |
| [`docs/security/SECURITY-CONTROLS.md`](docs/security/SECURITY-CONTROLS.md) | Control inventory |
| [`docs/research/UPSTREAM-ENGINEERING-RESEARCH.md`](docs/research/UPSTREAM-ENGINEERING-RESEARCH.md) | Patterns studied and what was adopted or rejected |

---

**Owner:** Arslan Vuzmal Lone · [arslanvuzmallone.dev](https://arslanvuzmallone.dev)
