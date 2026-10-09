"""The personal PDF: a one-page website check, about one business, attached to the first email.

A brochure from a stranger reads like phishing to filters and to people. A
document called "Smile Dental - website check.pdf" that opens on a picture of
*their own* homepage is the opposite: it proves the email was written for them
before they have read a word of it.

**Every sentence is one the evidence page already shows.** The title, what was
observed, what it costs, what fixes it -- all read from the same
``audit_findings`` row, through the same ``load_page`` filter (confidence 0.7
or above, never inferred by a model, not contradicted by a later re-check).
The PDF adds layout, not claims. The one sentence about the sender comes from
the case-study registry, whose loader already refuses anything that reads as a
claim about the recipient.

**Built here, rendered by the browser worker.** This module writes HTML whose
images are ``artifact:`` keys; the worker resolves them from its own volume,
crops each screenshot to what a visitor sees first, refuses every network
request, and saves ``pdfs/<draft-id>.pdf``. The outbox worker reads that file
at send time and attaches it -- or, if it is not there, sends without it and
without the sentence announcing it.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
import uuid
from html import escape

from coldops.intelligence.case_studies import CaseStudy
from coldops.intelligence.evidence_page import EvidencePage, PageFinding

#: A first email carries the PDF only if it is at most this size. Half the send
#: gate's ceiling, so a document that grows is caught here, at render time,
#: rather than by the gate refusing the email.
MAX_PDF_BYTES = 200 * 1024

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9 &'.,()-]+")


def pdf_key(draft_id: uuid.UUID) -> str:
    """Where the worker writes it, relative to the artifact directory."""
    return f"pdfs/{draft_id}.pdf"


def pdf_path(artifact_dir: str | None, draft_id: uuid.UUID) -> pathlib.Path | None:
    if not artifact_dir:
        return None
    return pathlib.Path(artifact_dir) / pdf_key(draft_id)


def attachment_filename(business_name: str | None) -> str:
    """``Smile Dental - website check.pdf``: the business's own name, made safe.

    Restricted to plain characters: a filename is rendered by every mail client
    differently, and an accented or emoji name that becomes ``=?utf-8?...`` in
    one of them looks like exactly the kind of attachment people do not open.
    """
    name = _UNSAFE_FILENAME.sub(" ", business_name or "").strip()
    name = re.sub(r"\s+", " ", name)[:60].strip(" .-")
    return f"{name} - website check.pdf" if name else "Website check.pdf"


def attachment_note(domain: str | None) -> str:
    """The sentence in the email that introduces the attachment."""
    where = f"{domain}" if domain else "your website"
    return (
        f"I have attached a one-page check of {where}, with screenshots of what I found."
    )


def choose_finding(
    page: EvidencePage, cited_issue_types: list[str]
) -> PageFinding | None:
    """The finding the email itself led with, so the PDF and the email agree.

    Falls back to the page's first finding (already ordered conversion first),
    and to nothing when the evidence page would show nothing either.
    """
    by_type = {f.issue_type: f for f in page.findings}
    for issue_type in cited_issue_types:
        if issue_type in by_type:
            return by_type[issue_type]
    return page.findings[0] if page.findings else None


def _p(text: str | None, cls: str = "") -> str:
    if not text:
        return ""
    attr = f' class="{cls}"' if cls else ""
    return f"<p{attr}>{escape(text.strip())}</p>"


def build_html(
    page: EvidencePage,
    *,
    finding: PageFinding,
    case_study: CaseStudy | None,
    evidence_url: str | None,
    sender_name: str,
    sender_site: str | None,
    today: dt.date | None = None,
) -> str:
    """One A4 page. Pure: same inputs, same document."""
    when = (today or dt.datetime.now(dt.UTC).date()).strftime("%d %B %Y").lstrip("0")
    name = escape(page.business_name)
    domain = escape(page.domain or "")

    shots = []
    if "desktop" in page.shots:
        shots.append(
            '<figure class="desk"><img data-crop="0.56" data-width="760" '
            f'src="artifact:{escape(page.shots["desktop"][0], quote=True)}" alt="">'
            "<figcaption>Your homepage on a computer</figcaption></figure>"
        )
    if "mobile" in page.shots:
        shots.append(
            '<figure class="phone"><img data-crop="1.6" data-width="260" '
            f'src="artifact:{escape(page.shots["mobile"][0], quote=True)}" alt="">'
            "<figcaption>On a phone</figcaption></figure>"
        )
    shots_html = f'<div class="shots">{"".join(shots)}</div>' if shots else ""

    where = (
        f'<p class="where">Seen on <span>{escape(finding.page_url)}</span></p>'
        if finding.page_url
        else ""
    )
    proof = ""
    if case_study is not None:
        link = f'<p class="link">{escape(case_study.url)}</p>' if case_study.url else ""
        proof = (
            f"<section><h2>Who is writing</h2>{_p(case_study.sentence())}{link}</section>"
        )
    evidence = (
        "<section><h2>The full evidence</h2><p>Every measurement behind this page, "
        f'with the date it was taken:</p><p class="link">{escape(evidence_url)}</p></section>'
        if evidence_url
        else ""
    )
    site = f" &middot; {escape(sender_site)}" if sender_site else ""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{name} - website check</title>
<style>
@page {{ size: A4; margin: 0; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font: 10.5pt/1.45 "Segoe UI", Helvetica, Arial, sans-serif; color: #1b2430; }}
.sheet {{ width: 210mm; height: 297mm; padding: 16mm 17mm 14mm; display: flex;
  flex-direction: column; gap: 5mm; overflow: hidden; }}
header {{ display: flex; justify-content: space-between; align-items: baseline;
  border-bottom: 1.5pt solid #1f4f99; padding-bottom: 3mm; }}
h1 {{ font-size: 19pt; line-height: 1.15; margin: 0; letter-spacing: -0.01em; }}
.meta {{ font-size: 9pt; color: #5b6676; text-align: right; }}
.shots {{ display: flex; gap: 5mm; align-items: flex-start; }}
figure {{ margin: 0; }}
figure img {{ display: block; border: 0.75pt solid #c9d1dc; border-radius: 2mm; }}
.desk img {{ width: 128mm; }}
.phone img {{ width: 42mm; }}
figcaption {{ font-size: 8pt; color: #5b6676; margin-top: 1.5mm; }}
.finding {{ background: #eef3fa; border-radius: 2.5mm; padding: 4mm 5mm; }}
.finding h2 {{ color: #1f4f99; }}
.finding .title {{ font-size: 13pt; font-weight: 600; margin: 0 0 2mm; }}
h2 {{ font-size: 8.5pt; text-transform: uppercase; letter-spacing: 0.08em; color: #5b6676;
  margin: 0 0 1.5mm; }}
p {{ margin: 0 0 1.5mm; }}
.where {{ font-size: 9pt; color: #5b6676; }}
.where span, .link {{ font-family: Consolas, "Courier New", monospace; font-size: 8.5pt;
  word-break: break-all; color: #1f4f99; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 5mm; }}
footer {{ margin-top: auto; border-top: 0.75pt solid #c9d1dc; padding-top: 2.5mm;
  font-size: 8.5pt; color: #5b6676; }}
</style></head>
<body><div class="sheet">
<header><h1>Website check<br>{name}</h1>
<div class="meta">{domain}<br>{escape(when)}</div></header>
{shots_html}
<section class="finding"><h2>What I found</h2>
<p class="title">{escape(finding.title)}</p>
{_p(finding.observed_value)}{where}</section>
<div class="grid">
<section><h2>What it costs you</h2>{_p(finding.business_impact) or _p("Not measured.")}</section>
<section><h2>What fixes it</h2>{_p(finding.recommended_solution) or _p("Not measured.")}</section>
</div>
{proof}
{evidence}
<footer>{escape(sender_name)}{site} &middot; Measured on
{escape(finding.measured_at.strftime("%d %B %Y").lstrip("0"))} by visiting the page as a
customer would. Reply to the email this came with and I will take you off my list.</footer>
</div></body></html>"""


__all__ = [
    "MAX_PDF_BYTES",
    "attachment_filename",
    "attachment_note",
    "build_html",
    "choose_finding",
    "pdf_key",
    "pdf_path",
]
