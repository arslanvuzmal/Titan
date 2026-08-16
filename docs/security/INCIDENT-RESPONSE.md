# Titan-OS — Incident Response & Emergency Procedures

**Document Version:** 0.2.0  
**Classification:** Operational Security Policy  
**Last Updated:** 2026-08-03  

---

## 1. Emergency Outbound Kill-Switch

If an unauthorized email send, prompt injection attack, or deliverability anomaly is detected, execute the global emergency kill-switch immediately:

```bash
# Set global environment override on API and worker containers
docker exec titan-api python -c "from titan.config import settings; print(settings.environment)"
# Set TITAN_OUTBOUND_ENABLED=false across all environment manifests
```

Or via API (Owner role required):

```http
POST /api/v1/workspaces/current/emergency-halt
Authorization: Bearer <OWNER_TOKEN>
Content-Type: application/json

{
  "reason": "Suspected deliverability anomaly / security audit",
  "halt_scope": "all_campaigns"
}
```

---

## 2. Security Incident Severity Levels

| Level | Definition | Response SLA | Action Required |
|---|---|---|---|
| **SEV-0** | Unauthorized email delivery, credential leakage, cross-tenant data exposure. | **< 15 minutes** | Trigger global kill-switch, revoke active sessions, rotate leaked API keys, initiate root-cause investigation. |
| **SEV-1** | Bounce rate > 2%, complaint rate > 0.05%, SSRF guard bypass attempt detected. | **< 1 hour** | Pause affected campaign, audit outbox logs, inspect target domain reputation, review URL guard telemetry. |
| **SEV-2** | Provider rate-limit trip, model budget circuit breaker trip, non-critical webhook failure. | **< 4 hours** | Adjust rate-limit parameters, review workspace budget ledger, clear queued retries. |
| **SEV-3** | UI display discrepancy, non-blocking log warning, minor documentation gap. | **< 24 hours** | Schedule patch in routine maintenance window. |

---

## 3. Incident Playbooks

### Playbook A: Leaked API Credentials (e.g. Resend / Google Places / NVIDIA)

1. **Containment:** Immediately revoke key in provider administrative console.
2. **Key Rotation:** Generate new key in provider console. Update environment secret store / `.env`.
3. **Restart:** Perform rolling restart of `titan-api` and `titan-worker` containers.
4. **Audit:** Query `audit_log` table for all API requests executed using the leaked credential window.

### Playbook B: High Bounce / Spam Complaint Event

1. **Containment:** Automatically paused by outbox worker when daily threshold exceeded.
2. **Analysis:** Export list of bounced/complained addresses from `provider_events`.
3. **Suppression Enforcement:** Verify addresses are present in `suppression_entries`.
4. **Deliverability Check:** Validate SPF (`v=spf1 include:resend.com ~all`), DKIM, and DMARC (`p=reject`) DNS records for `arslanvuzmallone.com` using `dig txt`.

### Playbook C: SSRF Alarm Triggered

1. **Log Extraction:** Retrieve exact `seed_url` and target IP from `url_guard` log alert.
2. **Domain Blacklist:** Add malicious domain to `organization_domains` denylist.
3. **Browser Sandbox Audit:** Verify browser worker container remained isolated and did not initiate secondary connections to private IP ranges.
