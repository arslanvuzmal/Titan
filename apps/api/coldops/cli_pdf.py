"""``coldops pdf preview`` -- render real personal PDFs to look at, without sending anything.

    coldops pdf preview --workspace titan --limit 5 --out coldops-pdfs

Picks the highest-scoring leads that have a homepage screenshot and at least
one finding the evidence page would show, builds each document exactly as the
sweep would, has the browser worker render it, and writes the files to
``--out``. Nothing is saved on the artifact volume, nothing is queued, nothing
is sent. Copy them off the server with ``scp`` to read them.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import uuid

from sqlalchemy import select, text

from coldops.config import get_settings


async def _preview(args: argparse.Namespace) -> int:
    from coldops.db.models import Workspace
    from coldops.db.session import get_sessionmaker
    from coldops.intelligence import audit_pdf, case_studies, evidence_page
    from coldops.intelligence.composer import family_for
    from coldops.providers.browser_client import BrowserWorkerClient

    settings = get_settings()
    out_dir = pathlib.Path(args.out)

    async with get_sessionmaker()() as session:
        ws = (
            await session.execute(
                select(Workspace).where(Workspace.slug == args.workspace)
            )
        ).scalar_one_or_none()
        if ws is None:
            print(f"no workspace with slug {args.workspace!r}")
            return 1
        lead_ids = (
            (
                await session.execute(
                    text(
                        """
                    SELECT l.id FROM leads l
                     WHERE l.workspace_id = :ws
                       AND EXISTS (
                           SELECT 1 FROM browser_artifacts a
                             JOIN crawl_runs c ON c.id = a.crawl_run_id
                             JOIN research_runs r ON r.id = c.research_run_id
                            WHERE r.lead_id = l.id AND a.workspace_id = :ws
                              AND a.kind = 'screenshot_desktop' AND a.storage_key IS NOT NULL)
                     ORDER BY l.latest_score DESC NULLS LAST
                     LIMIT 60
                    """
                    ),
                    {"ws": ws.id},
                )
            )
            .scalars()
            .all()
        )
        pages = []
        for lead_id in lead_ids:
            page = await evidence_page.load_page(session, lead_id=lead_id)
            if page is not None and page.findings and "desktop" in page.shots:
                pages.append(page)
            if len(pages) >= args.limit:
                break

    if not pages:
        print(
            "No lead has both a saved screenshot and a finding the evidence page shows."
        )
        return 1

    client = BrowserWorkerClient(settings)
    written = 0
    try:
        for page in pages:
            finding = page.findings[0]
            study = case_studies.select(
                case_studies.registry(settings.case_studies_path),
                industry=None,
                family=family_for(finding.issue_type),
                issue_type=finding.issue_type,
            )
            evidence_url = (
                evidence_page.evidence_url(
                    settings.evidence_base_url, page.lead_id, settings.evidence_secret
                )
                if settings.evidence_base_url and settings.evidence_secret is not None
                else None
            )
            html = audit_pdf.build_html(
                page,
                finding=finding,
                case_study=study,
                evidence_url=evidence_url,
                sender_name=settings.owner_name,
                sender_site=str(settings.owner_portfolio_url).rstrip("/"),
            )
            result = await client.render_pdf(
                draft_id=uuid.uuid4(),
                html=html,
                max_bytes=audit_pdf.MAX_PDF_BYTES,
                max_pages=1,
                return_pdf=True,
                save=False,
            )
            name = audit_pdf.attachment_filename(page.business_name)
            if result.pdf is None:
                print(
                    f"  {name}: not rendered ({result.error or 'no document returned'})"
                )
                continue
            (out_dir / name).write_bytes(result.pdf)
            written += 1
            verdict = (
                "would be attached"
                if result.refused is None
                else f"would NOT be attached ({result.refused})"
            )
            print(
                f"  {name}: {result.pages} page(s), {result.bytes // 1024} KB, {verdict}"
            )
    finally:
        await client.aclose()
    print(f"Wrote {written} file(s) to {out_dir}")
    return 0 if written else 1


def _run(args: argparse.Namespace) -> int:
    from coldops.runtime import configure_event_loop

    configure_event_loop()
    pathlib.Path(args.out).mkdir(parents=True, exist_ok=True)
    return asyncio.run(_preview(args))


def add_pdf_parser(sub: argparse._SubParsersAction) -> None:
    pdf = sub.add_parser("pdf", help="the personal PDF attached to first emails")
    actions = pdf.add_subparsers(dest="pdf_action", required=True)
    preview = actions.add_parser(
        "preview", help="render real examples to files; sends nothing"
    )
    preview.add_argument("--workspace", default="titan")
    preview.add_argument("--limit", type=int, default=5)
    preview.add_argument("--out", default="coldops-pdfs")
    preview.set_defaults(func=_run)


__all__ = ["add_pdf_parser"]
