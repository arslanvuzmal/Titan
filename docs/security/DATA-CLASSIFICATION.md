# Titan-OS — Data Classification & Handling Policy

**Document Version:** 0.2.0  
**Classification:** Internal Data Governance Policy  
**Last Updated:** 2026-08-03  

---

## 1. Data Classification Tiers

Titan-OS processes data according to four distinct confidentiality tiers:

| Tier | Category | Examples | Handling Requirements |
|---|---|---|---|
| **Tier 1** | **System Secrets & Credentials** | API Keys (Resend, Google Places, Model Providers), JWT Secret Keys, Database Passwords, Webhook Signing Secrets. | Must NEVER be logged, checked into version control, or exposed via API endpoints. Stored encrypted at rest via Secret Manager or environment variables. Masked/redacted in all structured logs. |
| **Tier 2** | **Recipient & Lead Personal Data (PII)** | Contact email addresses, phone numbers, contact names, role titles, raw inbound message bodies. | Access restricted by workspace RBAC. Encrypted in transit (TLS 1.3) and at rest. Subject to deletion/suppression workflows upon request. Never shared across workspaces. |
| **Tier 3** | **Business Intelligence & Audit Evidence** | Domain URLs, Lighthouse performance scores, axe-core accessibility violations, public DOM selectors, screenshot artifacts. | Workspace isolated. Fingerprinted by stable hash excluding volatile capture metadata. Immutable once recorded. Retention managed per workspace policy. |
| **Tier 4** | **Public Metadata & System Logs** | System operational metrics, aggregate send rates, public documentation, non-sensitive audit events. | Unrestricted internal access. Sanitized for debugging. Redaction filter strips any embedded PII or Tier 1 secrets before log persistence. |

---

## 2. Redaction & Minimization Policy

1. **Log Sanitization:** All log output passes through `titan.security.redaction.Redactor`, which automatically strips patterns matching API keys, Bearer tokens, email addresses, and Authorization headers.
2. **Minimization of Public Data:** Titan-OS captures ONLY publicly accessible web content and contact pointers. Scraping of private directories or unauthorized personal data stores is prohibited.
3. **Suppression Permanence:** Deleting a lead or contact record from Titan-OS MUST NOT delete the corresponding `suppression_entries` record. Suppression records are permanent to guarantee compliance with unsubscribe and opt-out requests.
