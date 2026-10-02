# Cold-sending domains: setup

Phase 0 of the two-channel plan. Cold mail moves off `arslanvuzmallone.com`,
which keeps the website, replies and warm conversations. Everything below is
done once per domain. Only the operator does steps 1, 2 and 6 (purchases,
accounts, passwords).

## 1. Buy three domains

Brand-shaped, `.com` or `.co`, no hyphens, no digits, no "free/get/now/deal".
Candidates (availability not checked): `vuzmalstudio.com`, `arslanvuzmal.co`,
`workwitharslan.com`, `vuzmalworks.com`, `arslanbuilds.com`.

At the registrar, forward the bare domain (301) to `https://arslanvuzmallone.com`.

## 2. Two mailboxes per domain

One on Google Workspace, one on Microsoft 365, across the three domains — six
in all. Real first-name addresses (`arslan@`, `hello@`), your photo, a plain
signature. No aliases, no shared inboxes.

## 3. DNS records (per domain)

| Type | Name | Value | Notes |
|---|---|---|---|
| MX | @ | provider's MX | Google or Microsoft, as instructed by the admin console |
| TXT | @ | `v=spf1 include:_spf.google.com ~all` | or `include:spf.protection.outlook.com`; one SPF record only |
| TXT/CNAME | provider selector | DKIM key from the admin console | 2048-bit; turn signing on after the record resolves |
| TXT | `_dmarc` | `v=DMARC1; p=none; rua=mailto:dmarc@arslanvuzmallone.com; adkim=r; aspf=r` | move to `p=quarantine` after 30 clean days |
| A | `go` | `168.119.161.220` | the tracking host (pixel, evidence pages) |

## 4. Register for reputation data

- Google Postmaster Tools: add the domain, verify with the TXT record it gives.
- Microsoft SNDS: register the sending IPs it reports for the M365 mailboxes.

## 5. Tracking hosts and TLS (on the server)

Add to `/opt/titan/.env`:

```
TITAN_TRACKING_HOSTS=go.vuzmalstudio.com,go.arslanvuzmal.co,go.workwitharslan.com
TITAN_TRACKING_HOME=arslanvuzmallone.com
```

Then run `deploy/tls/enable-tracking-hosts.sh`. It waits for each `go` record
to resolve, proves the challenge path, gets a certificate, and checks the pixel
answers over https. Safe to re-run; a host that is not ready is skipped.

## 6. Mailbox credentials

Add each mailbox to `secrets/mailboxes.json` (the operator fills the
passwords; Titan never sees them typed). Use app passwords; SMTP on 587
STARTTLS (Hetzner blocks 25 and 465). Then:

```
docker compose exec api python -m titan.cli mailbox check
```

## 7. Warm-up, then the switch

- 21 days of warm-up before any cold send.
- Seed mailboxes for the placement round must be on the server
  (`TITAN_SEED_FILE`) with working credentials — 13 probes went unread in
  September because they were not.
- Set `TITAN_PLACEMENT_GATE_ENABLED=true`. From then on a mailbox sends only
  while its own probes read ≥70% inbox in the last 48 hours.
- When the first new mailbox reads inbox for 7 days: set
  `TITAN_MESSAGE_FORM=brief` and `TITAN_ONE_PAGER_SAMPLE_PERCENT=0`.
