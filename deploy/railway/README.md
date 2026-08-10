# Deploying Titan-OS: Vercel + Railway

The dashboard goes to Vercel. Everything else goes to Railway. This split is
not a preference — five of the six components cannot run on Vercel:

| Component | Why it needs a container host |
|---|---|
| Temporal worker | Polls a task queue continuously; a serverless function has no request to hang that on |
| Outbox worker | Holds `FOR UPDATE SKIP LOCKED` leases across a loop that must outlive any single request |
| Browser worker | Playwright + Chromium, and its whole security value is running isolated with no credentials |
| Temporal server | Stateful, with its own database |
| PostgreSQL, Redis | Managed data services |

The alternative — one VM running `deploy/docker-compose.prod.yml` — is a
complete substitute for everything below. Pick one.

---

## Before you start

**Rotate the provider keys first.** Any key that has been pasted into a chat
window, a terminal transcript, or a support ticket must be treated as public.
Rotating is minutes; a leaked Places key billed against your account is not.

Decide which of these is true, because it changes what you deploy:

- **Research only** — Titan discovers, crawls, scores, and drafts. Nothing is
  ever sent. `TITAN_PRODUCTION_SENDING_ENABLED=false`. This is the tested
  configuration and the one to start with.
- **Sending enabled** — do not do this yet. Deliverability has never been
  verified against a real sending domain, and without the follow-up scheduler
  each lead receives exactly one message. See
  `docs/PRODUCTION-ENABLEMENT-CHECKLIST.md`.

---

## 1. Authentication

Pick one. `TITAN_AUTH_MODE` decides which, and selecting one closes the other:
a deployment set to `clerk` returns 501 from `/api/v1/auth/token`, so there is
no second way in.

### Option A — username and passcode (`TITAN_AUTH_MODE=local`)

Titan's own identity provider. Two fields, argon2id, no external dependency.
Suited to a system with a handful of operators, which is what this is.

```
railway run --service api titan set-passcode \
  --email you@example.com --username you
```

It prompts for the passcode; it never takes it as an argument you would leave
in your shell history. The account and its workspace membership must already
exist — this command grants access to an existing member and creates nobody.

What it does *not* give you: SSO, MFA, or a password-reset email. Titan has no
outbound path for a reset link that is not the outreach mailbox itself, so
recovery is the same command run again by someone with database access.

Five consecutive failures lock the account for fifteen minutes
(`TITAN_LOGIN_MAX_ATTEMPTS`, `TITAN_LOGIN_LOCKOUT_SECONDS`). That lockout is
what makes a short passcode defensible online — it does nothing for a stolen
database, where only the argon2 cost stands between the dump and a login. If
that is your threat model, use a long passcode or use Clerk.

### Option B — Clerk (`TITAN_AUTH_MODE=clerk`)

1. Create a Clerk application. Note the **Frontend API URL** — it looks like
   `https://something-12.clerk.accounts.dev`. That is the issuer.
2. In Clerk, add the user who will operate Titan.
3. Create the matching Titan user *before* first sign-in, with the same email:

   ```sql
   INSERT INTO users (email, display_name, is_active)
   VALUES ('you@example.com', 'Your Name', true);

   INSERT INTO workspace_members (workspace_id, user_id, role)
   SELECT w.id, u.id, 'owner'
   FROM workspaces w, users u
   WHERE w.slug = 'titan' AND u.email = 'you@example.com';
   ```

   Titan provisions nobody implicitly. A valid Clerk token for an unknown
   subject gets 401 — who may reach this system stays a deliberate act. On
   first sign-in the Clerk subject is bound to this row, and only if Clerk
   states the email is verified.

Either way, an account with no `password_hash` cannot sign in locally. That is
every row created before passcodes existed, so turning on local auth grants
nobody a session they did not already have.

**The role is never read from the token.** It is read from `workspace_members`
on every request, so revoking a membership takes effect immediately rather
than when the token expires.

---

## 2. Railway

Create one project with these services. All the `apps/api` services run the
same image with different start commands.

| Service | Root directory | Start command |
|---|---|---|
| `postgres` | Railway Postgres plugin | — |
| `redis` | Railway Redis plugin | — (optional, see below) |
| `api` | `apps/api` | `uvicorn titan.api.main:app --host 0.0.0.0 --port $PORT` |
| `outbox-worker` | `apps/api` | `python -m titan.workers.outbox` |
| `temporal-worker` | `apps/api` | `python -m titan.workers.temporal_worker` |
| `browser-worker` | `apps/browser-worker` | `node dist/src/server.js` |

Only `api` gets a public domain. The workers and the browser worker are
reached over the private network and must not be exposed.

### Temporal

Railway has no Temporal plugin. Either:

- **Temporal Cloud** — set `TITAN_TEMPORAL_HOST` to your namespace endpoint and
  supply the client certificate. This is the option that does not require you
  to operate a Temporal cluster.
- **A Railway service** from `temporalio/auto-setup:1.22.4` with its own
  Postgres, mirroring the `temporal` and `temporal-postgres` services in
  `deploy/docker-compose.prod.yml`. Note that image is meant for development;
  for anything you depend on, use Cloud.

Until Temporal is reachable the `temporal-worker` will restart-loop and no
research run will progress. The API and CRM work regardless — which is exactly
the failure mode to watch for, because nothing else looks wrong.

### Migrations

Run once against the deployed database, before the first API start, and again
after any deploy that adds a migration:

```
railway run --service api alembic upgrade head
```

Do not put this in the API start command: several API replicas would race, and
a failed migration would take the API down with it rather than failing on its
own.

**The existing deployed database is not at a revision this repository knows.**
It reports `4c1d9b7a2e50`, which appears in no commit, and it carries two
tables and thirteen columns the repository's models do not define — including a
`users.username` that the passcode migration here also adds. `alembic upgrade`
against it will fail on the missing revision before it touches anything, which
is the correct behaviour and not a problem to work around with `stamp`.
Reconciling it means recovering the deployed source or writing the delta as a
migration by hand; either way it is a deliberate step, and stamping the
database to silence the error would leave alembic's history lying about what
the schema contains.

---

## 3. Environment variables

`.env.example` at the repository root is generated from `titan/config.py` and
lists all of them. The ones that matter for a deployment:

**Every `apps/api` service** (api and both workers — a worker missing one of
these fails in a way the API will not show you):

```
TITAN_ENVIRONMENT=production
TITAN_DATABASE_URL=${{Postgres.DATABASE_URL}}        # +psycopg, see below
TITAN_RATE_LIMIT_REDIS_URL=${{Redis.REDIS_URL}}      # optional; see below
TITAN_AUTH_MODE=local                                # or clerk; see section 1
TITAN_LOCAL_JWT_SECRET=<32+ random bytes>            # local mode only
TITAN_CLERK_ISSUER_URL=https://<your>.clerk.accounts.dev   # clerk mode only
TITAN_FRONTEND_URL=https://<your-project>.vercel.app
TITAN_TEMPORAL_HOST=<temporal endpoint>:7233
TITAN_BROWSER_WORKER_URL=http://browser-worker.railway.internal:8800
TITAN_BROWSER_WORKER_TOKEN=<generate a long random value>
TITAN_PRODUCTION_SENDING_ENABLED=false
TITAN_GOOGLE_PLACES_API_KEY=<rotated key>
TITAN_OPENROUTER_API_KEY=<rotated key>
TITAN_NVIDIA_API_KEY=<rotated key>
TITAN_SENDER_MAILING_ADDRESS=<a real postal address>
```

**Sending through a mailbox rather than an API provider** (`outbox-worker`
only — no other service may hold these):

```
TITAN_EMAIL_PROVIDER=smtp
TITAN_SMTP_HOST=mail.spacemail.com
TITAN_SMTP_PORT=465
TITAN_SMTP_SECURITY=ssl                    # or starttls on 587
TITAN_SMTP_USERNAME=outreach@example.com
TITAN_SMTP_PASSWORD=<the mailbox password>
```

Use a mailbox dedicated to outreach, never the administrative one. Cold email
earns complaints even when it is done well, and reputation damage lands on the
mailbox and the domain that sent it -- so the address you rely on for invoices
and password resets must not be the address that sends campaigns.

`TITAN_SMTP_SECURITY=none` is refused for anything but a loopback host, so a
misconfiguration cannot put the mailbox password on the wire in clear. Point it
at Mailpit (`localhost:1025`) to review rendered messages without sending.

Redis is used only for distributed rate limiting. Leaving
`TITAN_RATE_LIMIT_REDIS_URL` unset is supported — limits then apply per
process rather than across replicas, which is fine for a single API instance
and wrong the moment you scale to two.

Railway's `DATABASE_URL` is `postgresql://`; Titan needs the driver named
explicitly. Set `TITAN_DATABASE_URL` to the same value with
`postgresql+psycopg://`, or the API will start on the wrong driver and fail on
the first query.

**`browser-worker` only:**

```
BROWSER_WORKER_PORT=8800
BROWSER_WORKER_TOKEN=<the same value as TITAN_BROWSER_WORKER_TOKEN>
```

That is the complete list for this service. It is the only component that
fetches attacker-controlled URLs, so it holds no database URL, no provider key,
and no email credential — a full browser escape yields nothing that can read
tenant data or send mail. Do not add variables to it out of convenience.

---

## 4. Vercel

Import the repository and set **Root Directory to `apps/web`**. Everything
else comes from `apps/web/vercel.json`.

One environment variable:

```
NEXT_PUBLIC_API_URL=https://<your-api>.up.railway.app
```

It is `NEXT_PUBLIC_`, so it is compiled into the bundle and visible to anyone
who opens devtools. That is fine — it is a public API address, and every route
behind it requires a verified token. Never put a secret behind that prefix.

### Preview deployments

Every Vercel preview gets a unique hostname, so it cannot be listed in the
API's allowed origins in advance. Set on the API:

```
TITAN_VERCEL_PREVIEW_SCOPE=<your-project-scope>
```

which allows `https://<anything>-<scope>.vercel.app` and nothing else. Leaving
it unset means previews cannot reach the API — the safe default, since
`*.vercel.app` is an origin anyone in the world can deploy to.

---

## 5. Continuous deployment

- **Vercel** deploys `apps/web` on every push, with a preview per pull request.
- **CI** publishes `ghcr.io/<owner>/titan-api` and
  `ghcr.io/<owner>/titan-browser-worker` on every push to `main`, tagged both
  `sha-<commit>` and `latest`. Pull requests build but never push.
- **Railway** can watch the repository, or pull the image by tag.

Pin `sha-<commit>` in anything you depend on. `latest` moves under you, so a
restart six weeks from now silently becomes an upgrade — and you find out
during whatever caused the restart.

---

## 6. Verify the deployment

In order. Each step fails differently, and a later one passing does not imply
an earlier one worked.

```bash
API=https://<your-api>.up.railway.app

# 1. The process is up.
curl -sf $API/health

# 2. It can reach Postgres and the schema is at a known revision.
#    Note: /ready checks the database only. Redis being down will not show up
#    here, and neither will an unreachable Temporal or browser worker.
curl -s $API/ready | jq

# 3. Sending is blocked, and it says why. Expect would_send: false.
curl -s $API/ops/sending-preflight | jq

# 4. Auth actually rejects. Expect 401, not 200 and not 500.
curl -s -o /dev/null -w '%{http_code}\n' $API/api/v1/stats

# 5. Local sign-in refuses a wrong passcode and does not say why.
#    Expect 401 {"detail":"invalid credentials"} -- the same answer an
#    unknown username gets. A 501 here means TITAN_AUTH_MODE is clerk.
curl -s -X POST $API/api/v1/auth/token -H 'content-type: application/json' \
  -d '{"username":"nobody","passcode":"wrong-on-purpose"}'
```

Then sign in to the Vercel URL and confirm:

- the banner reads **Delivery is blocked** and lists the blockers;
- **Operations** shows the Temporal worker's runs — if it shows none after a
  research run, the worker is not connected;
- **Leads** lists real rows, not an error.

### What "deployed" does not mean

- **Nothing sends.** `TITAN_PRODUCTION_SENDING_ENABLED` is false, and turning
  it on requires the deliverability work in
  `docs/PRODUCTION-ENABLEMENT-CHECKLIST.md` first.
- **Follow-ups are scheduled, not composed.** `titan.intelligence.sequencing`
  decides which step is owed and when, and the scanner writes `next_action_at`.
  Composing the follow-up is still the research pipeline's job, so a scheduled
  step does not become a message until that runs.
- **Replies are classified, but nothing collects them yet.**
  `titan.delivery.inbound.ingest_inbound` classifies a message and stops the
  sequence or suppresses accordingly. No webhook route and no IMAP poller feeds
  it, so the caller is still whoever hands it an `InboundMessage`.
- **No mailbox has been verified end to end.** SMTP auth is confirmed against
  `mail.spacemail.com` for `projects@` and `admin@` only, and no real message
  has been delivered through it.

`docs/audits/FINAL-PRODUCTION-VERIFICATION.md` §4 is the full list.
