"""Training data for the site-vision model (M1): homepage screenshots, labelled by the rules.

**What a row is.** One business: its latest desktop and mobile homepage
screenshot, and the set of defect types the rule detectors found on its site
-- only findings that pass the evidence page's own bar (confidence 0.7 or
above, measured rather than inferred by a model, not contradicted by a later
re-check). Those rules ran on every crawl, so a type *absent* from a row is a
weak negative: the rule looked and did not fire.

That is weak supervision, and the README says so. The model's first job is to
learn what the rules already see from the picture alone; its value comes later,
when it disagrees with them and a person checks who was right.

**What is never exported:** business names, domains, addresses, URLs, or any
text from the page. The files are the two screenshots (of public homepages) and
a label vector, keyed by a random-looking id derived from the lead id.

**The split is by business and deterministic** -- a hash of the lead id -- so
the same business is always in the same split across exports, and a model is
never tested on a site it trained on.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import shutil
import uuid
from collections import Counter

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coldops.intelligence.evidence_page import shot_path

#: A defect type seen fewer times than this is not a class: too few examples
#: to learn and too few to test on.
DEFAULT_MIN_COUNT = 20


@dataclasses.dataclass(frozen=True)
class SiteRow:
    lead_id: uuid.UUID
    desktop_key: str
    mobile_key: str | None
    issue_types: frozenset[str]
    captured_at: dt.datetime

    @property
    def example_id(self) -> str:
        """Stable, and not the lead id itself."""
        return hashlib.sha256(f"site-dataset:{self.lead_id}".encode()).hexdigest()[:16]

    @property
    def split(self) -> str:
        bucket = (
            int(hashlib.sha256(f"split:{self.lead_id}".encode()).hexdigest()[:8], 16) % 10
        )
        return "test" if bucket == 0 else "val" if bucket == 1 else "train"


async def site_rows(session: AsyncSession, *, workspace_id: uuid.UUID) -> list[SiteRow]:
    rows = (
        await session.execute(
            text(
                """
                WITH shots AS (
                    SELECT DISTINCT ON (r.lead_id, a.kind)
                           r.lead_id, a.kind, a.storage_key, a.captured_at
                      FROM browser_artifacts a
                      JOIN crawl_runs c ON c.id = a.crawl_run_id AND c.workspace_id = :ws
                      JOIN research_runs r ON r.id = c.research_run_id AND r.workspace_id = :ws
                     WHERE a.workspace_id = :ws
                       AND a.kind IN ('screenshot_desktop', 'screenshot_mobile')
                       AND a.storage_key IS NOT NULL
                     ORDER BY r.lead_id, a.kind, a.captured_at DESC
                ),
                found AS (
                    SELECT f.lead_id, array_agg(DISTINCT f.issue_type) AS types
                      FROM audit_findings f
                     WHERE f.workspace_id = :ws
                       AND NOT f.contradicted
                       AND f.confidence >= 0.7
                       AND f.verification_method <> 'model_inference'
                     GROUP BY f.lead_id
                )
                SELECT d.lead_id, d.storage_key AS desktop, m.storage_key AS mobile,
                       d.captured_at, COALESCE(found.types, ARRAY[]::text[]) AS types
                  FROM shots d
                  LEFT JOIN shots m ON m.lead_id = d.lead_id AND m.kind = 'screenshot_mobile'
                  LEFT JOIN found ON found.lead_id = d.lead_id
                 WHERE d.kind = 'screenshot_desktop'
                """
            ),
            {"ws": workspace_id},
        )
    ).all()
    return [
        SiteRow(
            lead_id=r.lead_id,
            desktop_key=r.desktop,
            mobile_key=r.mobile,
            issue_types=frozenset(r.types or ()),
            captured_at=r.captured_at,
        )
        for r in rows
    ]


def vocabulary(rows: list[SiteRow], *, min_count: int = DEFAULT_MIN_COUNT) -> list[str]:
    counts = Counter(t for row in rows for t in row.issue_types)
    return sorted(t for t, n in counts.items() if n >= min_count)


@dataclasses.dataclass(frozen=True)
class ExportReport:
    examples: int
    skipped_missing_file: int
    classes: list[str]
    per_split: dict[str, int]


_README = """# ColdOps site-vision dataset

One row per business in `manifest.jsonl`:

    {{"id": "...", "split": "train|val|test", "desktop": "images/<id>-desktop.jpg",
     "mobile": "images/<id>-mobile.jpg" | null, "labels": [0, 1, ...]}}

`labels` is multi-hot over `classes.json` ({n_classes} classes). A 1 means the
ColdOps rule detectors found that defect on the business's site with
confidence 0.7 or more; a 0 means they looked and did not. That is weak
supervision: the labels are the rules' opinion, not a person's.

Splits are by business and stable across exports: {splits}.

Exported {when}. No names, domains, URLs or page text are included.

Suggested first model: a pretrained image encoder (e.g. SigLIP or a small
ConvNeXt) with a multi-label head, trained on the desktop screenshot; report
per-class average precision on `test`. Export to ONNX for CPU inference.
"""


def write_export(
    rows: list[SiteRow],
    *,
    artifact_dir: str,
    out_dir: pathlib.Path,
    min_count: int = DEFAULT_MIN_COUNT,
    now: dt.datetime | None = None,
) -> ExportReport:
    classes = vocabulary(rows, min_count=min_count)
    images = out_dir / "images"
    images.mkdir(parents=True, exist_ok=True)
    per_split: Counter[str] = Counter()
    missing = 0
    with (out_dir / "manifest.jsonl").open("w", encoding="utf-8") as manifest:
        for row in sorted(rows, key=lambda r: r.example_id):
            desktop_src = shot_path(artifact_dir, row.desktop_key)
            if desktop_src is None or not desktop_src.exists():
                missing += 1
                continue
            desktop = f"images/{row.example_id}-desktop.jpg"
            shutil.copyfile(desktop_src, out_dir / desktop)
            mobile = None
            if row.mobile_key:
                mobile_src = shot_path(artifact_dir, row.mobile_key)
                if mobile_src is not None and mobile_src.exists():
                    mobile = f"images/{row.example_id}-mobile.jpg"
                    shutil.copyfile(mobile_src, out_dir / mobile)
            manifest.write(
                json.dumps(
                    {
                        "id": row.example_id,
                        "split": row.split,
                        "desktop": desktop,
                        "mobile": mobile,
                        "labels": [int(c in row.issue_types) for c in classes],
                    }
                )
                + "\n"
            )
            per_split[row.split] += 1
    (out_dir / "classes.json").write_text(json.dumps(classes, indent=2), encoding="utf-8")
    (out_dir / "README.md").write_text(
        _README.format(
            n_classes=len(classes),
            splits=", ".join(f"{k} {v}" for k, v in sorted(per_split.items())),
            when=(now or dt.datetime.now(dt.UTC)).strftime("%Y-%m-%d %H:%M UTC"),
        ),
        encoding="utf-8",
    )
    return ExportReport(
        examples=sum(per_split.values()),
        skipped_missing_file=missing,
        classes=classes,
        per_split=dict(per_split),
    )


__all__ = [
    "DEFAULT_MIN_COUNT",
    "ExportReport",
    "SiteRow",
    "site_rows",
    "vocabulary",
    "write_export",
]
