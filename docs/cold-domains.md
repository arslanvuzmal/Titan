# Sending domains: repair arslanvuzmallone.com first, add domains later

Decided 3 October 2026: cold mail keeps going out from `arslanvuzmallone.com`
and its existing Spacemail mailboxes. That domain is repaired first. New
domains are bought only once it holds (Part B), never as a substitute for
fixing it.

Where it stands: every placement probe ever read (6 of 6) landed in spam.
Authentication is clean (SPF, DKIM `spacemail`, DMARC `p=quarantine`); the
damage is reputation — 492 cold sends in one week on a seven-week-old domain,
a PDF on many of them, and no warm-up traffic at all after Smartlead was
dropped on 24 August.

## Part A — repair (now)

### A1. Switch on the protections (server `.env`, then restart)

```
TITAN_PLACEMENT_GATE_ENABLED=true
TITAN_MESSAGE_FORM=brief
TITAN_ONE_PAGER_SAMPLE_PERCENT=0
```

- **Placement gate:** a mailbox sends cold mail only while its own probes
  read ≥70% inbox over the last 48 hours. While the domain reads spam, cold
  sending pauses by design: more cold mail into spam folders deepens the hole.
  Drafts keep being written and wait.
- **Brief form:** 60–120 words, one link, no references block, no attachment.
- Only `arslan@` carries cold mail. `outreach@`, `sales@` and `projects@`
  take part in warm-up and placement probes only.

### A2. Seed inboxes on the server

The gate is blind without readings, and 13 probes went unread in September
because the seed credentials were not on the server.

- 2 Gmail and 1 Yahoo — fresh accounts used for nothing else. Each needs
  2-Step Verification switched on and an **app password** (16 characters,
  pasted without spaces); Titan reads them over IMAP with it.
- **Not Outlook/Hotmail.** *Corrected 3 October; the first version of this
  page said 2 Outlook.* Microsoft stopped accepting password logins over IMAP
  for personal Outlook, Hotmail, Live and MSN accounts on 16 September 2024,
  so Titan cannot read one. Reading a Microsoft inbox needs OAuth 2.0 (a free
  Azure app registration plus a refresh token), which is not built yet. Until
  it is, Microsoft placement is checked by hand.
- Listed in `secrets/seeds.json` (start from `secrets/seeds.json.example`),
  with `TITAN_SEED_FILE=/run/secrets/seeds.json` in `.env`.

### A3. Warm-up partners

Mail between our own five mailboxes never leaves Spacemail, so it teaches
Gmail and Microsoft nothing. Partners are mailboxes on those providers that
receive our warm-up mail, rescue it from spam, read it and reply.

- 3–5 more fresh **Gmail and Yahoo** accounts, each with an app password —
  **not** Outlook, for the same reason as above: rescuing mail from spam needs
  an IMAP login, and Microsoft refuses passwords. And **not** the seeds. A seed that
  rescues and replies to our mail would file our next probe in the inbox
  because of its own history, and the gate would reopen on a reading it
  manufactured. Titan refuses the overlap.
- Listed in `secrets/warmup_partners.json` (same shape as `mailboxes.json`),
  then:

```
TITAN_WARMUP_PARTNER_FILE=/run/secrets/warmup_partners.json
TITAN_WARMUP_ENABLED=true
```

The round runs daily at 09:10 UTC, after the placement round is read.

Five partners is a small network. A paid warm-up network (hundreds of real
inboxes, roughly $15–30 per mailbox a month) moves reputation faster; it can
run alongside this, connected to `arslan@` by SMTP/IMAP.

### A4. Reputation data

- **Google Postmaster Tools:** add `arslanvuzmallone.com`, verify with the TXT
  record it gives. It shows Gmail's own domain reputation once volume allows.
- **DMARC reports:** add `rua=mailto:<an address you read>` to the existing
  `_dmarc` record, keeping `p=quarantine`.

### A5. What "repaired" means

All of these, held for 14 consecutive days:

- `arslan@` reads ≥80% inbox at Gmail and Yahoo on the automatic probes, and
  lands in the inbox when checked by hand at Outlook;
- cold sending running at ≥15 a day through the gate;
- hard bounces under 2%, no spam complaints.

Then, and only then, Part B.

## Part B — more domains (after A5)

Three brand-shaped domains, `.com` or `.co`, no hyphens or digits. Two
mailboxes each (Workspace + M365). Per domain:

| Type | Name | Value |
|---|---|---|
| MX | @ | provider's MX |
| TXT | @ | `v=spf1 include:_spf.google.com ~all` (or the Microsoft include) |
| TXT/CNAME | DKIM selector | key from the admin console, 2048-bit |
| TXT | `_dmarc` | `v=DMARC1; p=none; rua=mailto:…` → `quarantine` after 30 clean days |
| A | `go` | `168.119.161.220` |

Then `TITAN_TRACKING_HOSTS=go.<domain>,…` and
`deploy/tls/enable-tracking-hosts.sh`, credentials into `mailboxes.json`,
21 days of warm-up, and the same placement gate before any cold send.
