"""One private page per lead: what was measured, where, and who says it matters.

The brief first message carries one link, and this is where it goes. The
mechanism, the repair and the references that the long message used to carry
inline live here instead -- four URLs in a stranger's first email reads as
phishing to a filter, while one link to a page on the sender's own domain does
not.

It also answers the question the follow-ups branch on. A visit is the
strongest evidence that a message was read, and the page's own script confirms
that a person stayed, which a pixel can never do.

**Only measured findings appear.** The same rule the composer obeys: not
contradicted, not model-inferred, confident, and backed by at least one
evidence row. A page that told an owner something about their own site that
is not true would be found out in seconds, by the one person it is for.

**The token is signed.** A bare lead id in a URL would let anybody walk the
range and read every business's audit. The signature is what stands in for a
login: the page has a single reader and no account to give them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import pathlib
import re
import uuid
from dataclasses import dataclass, field
from html import escape

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coldops.intelligence.references import Reference, references_for
from coldops.intelligence.vernacular import Engine, engine_for

#: The most findings the page shows. An audit with thirty items reads as a
#: sales tactic; the few that matter, explained, read as care.
MAX_FINDINGS = 6

#: How long the page's script waits before reporting that somebody stayed.
#: Matches ``engagement.MIN_DWELL_SECONDS``; a scanner never waits.
BEACON_DELAY_MS = 5000


# ------------------------------------------------------------------ the token
def _raw(secret: SecretStr | str) -> str:
    return secret.get_secret_value() if isinstance(secret, SecretStr) else secret


def evidence_token(lead_id: uuid.UUID, secret: SecretStr | str) -> str:
    """``<lead id>.<16 hex>``. Short enough for a message, unforgeable without
    the secret. A different purpose string from the pixel's, so one token can
    never be replayed as the other."""
    digest = hmac.new(
        _raw(secret).encode(), f"evidence:{lead_id}".encode(), hashlib.sha256
    ).hexdigest()
    return f"{lead_id}.{digest[:16]}"


def verify_evidence_token(token: str, secret: SecretStr | str) -> uuid.UUID | None:
    raw, _, signature = token.partition(".")
    if not signature:
        return None
    try:
        lead_id = uuid.UUID(raw)
    except ValueError:
        return None
    expected = evidence_token(lead_id, secret).partition(".")[2]
    if not hmac.compare_digest(signature, expected):
        return None
    return lead_id


def evidence_url(base_url: str, lead_id: uuid.UUID, secret: SecretStr | str) -> str:
    return f"{base_url.rstrip('/')}/e/{evidence_token(lead_id, secret)}"


# ------------------------------------------------------------------ the data
@dataclass(frozen=True, slots=True)
class PageFinding:
    issue_type: str
    title: str
    page_url: str | None
    observed_value: str | None
    business_impact: str | None
    recommended_solution: str | None
    measured_at: dt.datetime
    excerpts: tuple[str, ...] = ()
    references: tuple[Reference, ...] = ()

    @property
    def tier(self) -> int:
        engine = engine_for(self.issue_type, self.page_url)
        if engine is Engine.CONVERSION:
            return 0
        if engine is Engine.AUTOMATION:
            return 1
        return 2


@dataclass(frozen=True, slots=True)
class EvidencePage:
    workspace_id: uuid.UUID
    lead_id: uuid.UUID
    business_name: str
    domain: str | None
    findings: tuple[PageFinding, ...] = field(default_factory=tuple)
    #: Latest saved homepage screenshot per view: {"desktop"|"mobile": (key, when)}.
    shots: dict[str, tuple[str, dt.datetime]] = field(default_factory=dict)


#: Which stored artifact kind each view on the page reads.
SHOT_KINDS = {"desktop": "screenshot_desktop", "mobile": "screenshot_mobile"}

#: The only shape of storage key a screenshot may have. Checked before any path
#: is built from one, so a key in the database can never point outside the
#: screenshot directory. Mirrors storageKeyFor() in the browser worker.
_SHOT_KEY = re.compile(r"^shots/[0-9a-f]{2}/[0-9a-f]{64}\.jpg$")


def shot_path(artifact_dir: str, storage_key: str) -> pathlib.Path | None:
    """The file for a storage key, or None if the key is not a screenshot key."""
    if not _SHOT_KEY.match(storage_key):
        return None
    return pathlib.Path(artifact_dir) / storage_key


async def latest_shots(
    session: AsyncSession, *, workspace_id: uuid.UUID, lead_id: uuid.UUID
) -> dict[str, tuple[str, dt.datetime]]:
    """The most recent saved screenshot of each view of this lead's business.

    Looked up through the business, not the lead. A screenshot is stored once
    per identical image (unique on workspace, fingerprint, kind), so a second
    lead for the same site -- the same business in another campaign, or a
    re-run of the end-to-end test -- takes the same picture and saves no row of
    its own; keyed on the lead, its PDF and evidence page showed no screenshot.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT ON (a.kind) a.kind, a.storage_key, a.captured_at
                  FROM browser_artifacts a
                  JOIN crawl_runs c ON c.id = a.crawl_run_id AND c.workspace_id = :ws
                  JOIN research_runs r ON r.id = c.research_run_id AND r.workspace_id = :ws
                  JOIN leads l ON l.id = r.lead_id AND l.workspace_id = :ws
                 WHERE a.workspace_id = :ws
                   AND l.organization_id = (
                       SELECT organization_id FROM leads
                        WHERE id = :lead AND workspace_id = :ws)
                   AND a.kind = ANY(CAST(:kinds AS text[]))
                   AND a.storage_key IS NOT NULL
                 ORDER BY a.kind, a.captured_at DESC
                """
            ),
            {"ws": workspace_id, "lead": lead_id, "kinds": list(SHOT_KINDS.values())},
        )
    ).all()
    by_kind = {r.kind: (r.storage_key, r.captured_at) for r in rows}
    return {view: by_kind[kind] for view, kind in SHOT_KINDS.items() if kind in by_kind}


async def load_page(session: AsyncSession, *, lead_id: uuid.UUID) -> EvidencePage | None:
    """Everything the page shows, or None for a lead that does not exist.

    The first query is keyed on the lead's primary key alone, and unscoped by a
    workspace because the caller has none: it is a browser holding a token this
    estate signed. Every query after it names the workspace that row belongs to.
    """
    head = (
        await session.execute(
            text(
                """
                SELECT l.workspace_id, o.display_name, o.canonical_domain
                  FROM leads l
                  JOIN organizations o ON o.id = l.organization_id
                 WHERE l.id = :lead
                """
            ),
            {"lead": lead_id},
        )
    ).first()
    if head is None:
        return None
    workspace_id = head.workspace_id

    rows = (
        await session.execute(
            text(
                """
                SELECT f.id, f.issue_type, f.title, f.page_url, f.observed_value,
                       f.business_impact, f.recommended_solution, f.created_at,
                       f.severity, f.confidence,
                       array_remove(array_agg(DISTINCT e.excerpt), NULL) AS excerpts
                  FROM audit_findings f
                  JOIN finding_evidence e
                    ON e.finding_id = f.id AND e.workspace_id = :ws
                 WHERE f.workspace_id = :ws
                   AND f.lead_id = :lead
                   AND NOT f.contradicted
                   AND f.confidence >= 0.7
                   AND f.verification_method <> 'model_inference'
                 GROUP BY f.id
                """
            ),
            {"ws": workspace_id, "lead": lead_id},
        )
    ).all()

    findings = [
        PageFinding(
            issue_type=r.issue_type,
            title=r.title,
            page_url=r.page_url,
            observed_value=r.observed_value,
            business_impact=r.business_impact,
            recommended_solution=r.recommended_solution,
            measured_at=r.created_at,
            excerpts=tuple(x for x in (r.excerpts or ())[:2] if x),
            references=references_for(r.issue_type),
        )
        for r in rows
    ]
    # Tier first, as the message does: what stopped somebody buying, then what
    # is missing, then quality. One finding per issue type, so the page is not
    # the same defect on six pages.
    findings.sort(key=lambda f: (f.tier, f.issue_type))
    unique: list[PageFinding] = []
    seen_types: set[str] = set()
    for finding in findings:
        if finding.issue_type in seen_types:
            continue
        seen_types.add(finding.issue_type)
        unique.append(finding)

    return EvidencePage(
        workspace_id=workspace_id,
        lead_id=lead_id,
        business_name=head.display_name,
        domain=head.canonical_domain,
        findings=tuple(unique[:MAX_FINDINGS]),
        shots=await latest_shots(session, workspace_id=workspace_id, lead_id=lead_id),
    )


# ------------------------------------------------------------------ the page
def _link(url: str, label: str | None = None) -> str:
    return (
        f'<a href="{escape(url, quote=True)}" rel="noopener noreferrer nofollow">'
        f"{escape(label or url)}</a>"
    )


def _finding_html(index: int, finding: PageFinding) -> str:
    parts = [f'<section class="f"><h2><span>{index}</span>{escape(finding.title)}</h2>']
    facts = []
    if finding.page_url:
        facts.append(f"<dt>Where</dt><dd>{_link(finding.page_url)}</dd>")
    if finding.observed_value:
        facts.append(f"<dt>Measured</dt><dd>{escape(finding.observed_value)}</dd>")
    facts.append(f"<dt>Checked</dt><dd>{finding.measured_at:%d %B %Y}</dd>")
    parts.append(f"<dl>{''.join(facts)}</dl>")
    for excerpt in finding.excerpts:
        parts.append(f"<blockquote>{escape(excerpt[:400])}</blockquote>")
    if finding.business_impact:
        parts.append(f"<h3>Why it matters</h3><p>{escape(finding.business_impact)}</p>")
    if finding.recommended_solution:
        parts.append(
            f"<h3>How it gets fixed</h3><p>{escape(finding.recommended_solution)}</p>"
        )
    if finding.references:
        items = "".join(
            f"<li>{escape(r.publisher)}: {_link(r.url, r.title)}</li>"
            for r in finding.references
        )
        parts.append(f"<h3>Sources</h3><ul>{items}</ul>")
    parts.append("</section>")
    return "".join(parts)


def render(
    page: EvidencePage,
    *,
    token: str,
    owner_name: str,
    portfolio_url: str,
) -> str:
    """The whole page. Self-contained: no fonts, scripts or images from anywhere
    else, so opening it tells no third party anything."""
    name = escape(page.business_name)
    domain = escape(page.domain or "your website")
    if page.findings:
        body = "".join(_finding_html(i, f) for i, f in enumerate(page.findings, 1))
    else:
        body = (
            "<p>Everything I found earlier has since been fixed or could not be "
            "confirmed again, so there is nothing to show here.</p>"
        )
    shots = ""
    if page.shots:
        captured = max(when for _, when in page.shots.values())
        images = "".join(
            f'<img class="{view}" src="/e/{escape(token, quote=True)}/shot/{view}.jpg" '
            f'alt="Your homepage on {"a phone" if view == "mobile" else "a computer"}" '
            'loading="lazy">'
            for view in ("desktop", "mobile")
            if view in page.shots
        )
        shots = (
            f'<figure class="shots">{images}<figcaption>Your homepage as I saw it on '
            f"{captured:%d %B %Y}.</figcaption></figure>"
        )
    beacon = f"""<script>
setTimeout(function(){{try{{var d=document.documentElement;
var s=Math.round(100*(window.scrollY+window.innerHeight)/Math.max(d.scrollHeight,1));
navigator.sendBeacon('/e/{escape(token, quote=True)}/seen',
JSON.stringify({{dwell:{BEACON_DELAY_MS // 1000},scroll:s}}));}}catch(e){{}}}},{BEACON_DELAY_MS});
</script>"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<meta name="referrer" content="no-referrer">
<title>What I found on {domain}</title>
<style>
:root{{--bg:#f6f7f8;--fg:#15191d;--mute:#5b6570;--rule:#d9dee3;--accent:#1d5c8f;--card:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#101418;--fg:#e6eaee;--mute:#9aa6b2;--rule:#2a333c;--accent:#7fb6e6;--card:#161c22}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.6 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif}}
main{{max-width:720px;margin:0 auto;padding:40px 18px 64px}}
h1{{font-size:1.7rem;line-height:1.2;margin:0 0 8px}}
.lede{{color:var(--mute);margin:0 0 28px}}
.f{{background:var(--card);border:1px solid var(--rule);border-radius:6px;padding:18px 20px;margin:0 0 16px}}
.f h2{{font-size:1.1rem;margin:0 0 10px;display:flex;gap:10px}}
.f h2 span{{color:var(--accent);font-variant-numeric:tabular-nums}}
h3{{font-size:.8rem;text-transform:uppercase;letter-spacing:.08em;color:var(--mute);margin:16px 0 4px}}
dl{{display:grid;grid-template-columns:90px 1fr;gap:4px 12px;margin:0;font-size:.95rem}}
dt{{color:var(--mute)}}dd{{margin:0;overflow-wrap:anywhere}}
blockquote{{margin:10px 0;padding:8px 12px;border-left:3px solid var(--rule);color:var(--mute);font-size:.92rem}}
a{{color:var(--accent)}}ul{{padding-left:18px;margin:4px 0}}
.shots{{margin:0 0 20px;display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}}
.shots img{{border:1px solid var(--rule);border-radius:6px;max-width:100%;height:auto}}
.shots img.desktop{{flex:1 1 380px;min-width:0}}.shots img.mobile{{width:150px}}
.shots figcaption{{flex-basis:100%;color:var(--mute);font-size:.88rem}}
footer{{margin-top:32px;padding-top:16px;border-top:1px solid var(--rule);color:var(--mute);font-size:.92rem}}
</style></head>
<body><main>
<h1>What I found on {domain}</h1>
<p class="lede">Prepared for {name}. Each item below was measured on your live site, with the page it was found on and the date it was checked, so you can confirm it yourself.</p>
{shots}{body}
<footer>
<p>{escape(owner_name)} &middot; {_link(portfolio_url, "examples of my work")}</p>
<p>Reply to my email if you would like to go through any of this. If you would rather not hear from me again, reply and say so and I will take you off the list.</p>
<p>This page is private to {name} and is not indexed by search engines.</p>
</footer>
</main>{beacon}</body></html>"""


def not_found_html() -> str:
    """Identical for a forged token and a deleted lead, so neither is an oracle."""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name="robots" content="noindex"><title>Page not available</title></head>'
        '<body style="font:16px -apple-system,Segoe UI,sans-serif;padding:40px">'
        "<p>This page is not available.</p></body></html>"
    )


__all__ = [
    "BEACON_DELAY_MS",
    "MAX_FINDINGS",
    "SHOT_KINDS",
    "EvidencePage",
    "PageFinding",
    "evidence_token",
    "evidence_url",
    "latest_shots",
    "load_page",
    "not_found_html",
    "render",
    "shot_path",
    "verify_evidence_token",
]
