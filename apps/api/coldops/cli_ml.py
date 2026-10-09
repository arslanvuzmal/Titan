"""``coldops ml`` -- see how each model is doing, promote one, export training data.

    coldops ml status --workspace titan
    coldops ml promote reply_reader <version> --by "Arslan"
    coldops ml export-site-dataset --workspace titan --out site-dataset

``status`` grades every version against the operator's labels. ``promote``
applies the promotion rule in ``coldops.ml.registry`` and refuses with a reason
when it does not hold. ``export-site-dataset`` writes the M1 training set:
screenshots and the rules' labels, nothing that names a business.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import uuid

from sqlalchemy import select, text

from coldops.config import get_settings


async def _workspace_id(slug: str) -> uuid.UUID:
    from coldops.db.models import Workspace
    from coldops.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(Workspace).where(Workspace.slug == slug))
        ).scalar_one_or_none()
    if row is None:
        raise SystemExit(f"no workspace with slug {slug!r}")
    return row.id


async def _status(args: argparse.Namespace) -> int:
    from coldops.db.session import workspace_unit_of_work
    from coldops.ml import registry

    workspace_id = await _workspace_id(args.workspace)
    async with workspace_unit_of_work(workspace_id) as session:
        names = (
            (
                await session.execute(
                    text(
                        "SELECT DISTINCT name FROM ml_models WHERE workspace_id = :ws ORDER BY name"
                    ),
                    {"ws": workspace_id},
                )
            )
            .scalars()
            .all()
        )
        if not names:
            print("No models registered yet. The shadow round registers them as it runs.")
            return 0
        for name in names:
            print(name)
            for s in sorted(
                await registry.evaluate(session, workspace_id=workspace_id, name=name),
                key=lambda s: (s.status != "active", s.version),
            ):
                acc = f"{s.accuracy:.0%}" if s.accuracy is not None else "-"
                print(
                    f"  {s.status:<8} {s.version:<60} {s.correct:>4}/{s.labelled:<4} {acc}"
                )
    print(
        f"\nA version needs {registry.MIN_LABELS} labelled replies before it can be judged."
    )
    return 0


async def _promote(args: argparse.Namespace) -> int:
    from coldops.db.session import workspace_unit_of_work
    from coldops.ml import registry

    workspace_id = await _workspace_id(args.workspace)
    async with workspace_unit_of_work(workspace_id) as session:
        model_id = (
            await session.execute(
                text(
                    "SELECT id FROM ml_models WHERE workspace_id = :ws "
                    "AND name = :name AND version = :version"
                ),
                {"ws": workspace_id, "name": args.name, "version": args.version},
            )
        ).scalar_one_or_none()
        if model_id is None:
            print(f"No version {args.version!r} of {args.name!r}.")
            return 1
        try:
            score = await registry.promote(
                session, workspace_id=workspace_id, model_id=model_id, approved_by=args.by
            )
        except registry.PromotionRefused as exc:
            print(f"Not promoted: {exc}")
            return 1
    print(
        f"Promoted {args.version}: {score.correct}/{score.labelled} right on held-out labels."
    )
    return 0


async def _export(args: argparse.Namespace) -> int:
    from coldops.db.session import workspace_unit_of_work
    from coldops.ml import datasets

    settings = get_settings()
    if not settings.artifact_dir:
        print("No artifact directory is configured; the screenshots live there.")
        return 1
    workspace_id = await _workspace_id(args.workspace)
    async with workspace_unit_of_work(workspace_id) as session:
        rows = await datasets.site_rows(session, workspace_id=workspace_id)
    report = datasets.write_export(
        rows,
        artifact_dir=settings.artifact_dir,
        out_dir=pathlib.Path(args.out),
        min_count=args.min_count,
    )
    print(
        f"Wrote {report.examples} examples, {len(report.classes)} classes, to {args.out}"
    )
    print("  " + ", ".join(f"{k} {v}" for k, v in sorted(report.per_split.items())))
    if report.skipped_missing_file:
        print(
            f"  {report.skipped_missing_file} skipped: screenshot file purged or never saved"
        )
    return 0


def _run(coro_fn):
    def handler(args: argparse.Namespace) -> int:
        from coldops.runtime import configure_event_loop

        configure_event_loop()
        if getattr(args, "out", None):
            pathlib.Path(args.out).mkdir(parents=True, exist_ok=True)
        return asyncio.run(coro_fn(args))

    return handler


def add_ml_parser(sub: argparse._SubParsersAction) -> None:
    ml = sub.add_parser("ml", help="trained models: status, promotion, datasets")
    actions = ml.add_subparsers(dest="ml_action", required=True)

    status = actions.add_parser(
        "status", help="how every model version scores on your labels"
    )
    status.add_argument("--workspace", default="titan")
    status.set_defaults(func=_run(_status))

    promote = actions.add_parser(
        "promote", help="make a version active, if it has earned it"
    )
    promote.add_argument("name")
    promote.add_argument("version")
    promote.add_argument("--by", required=True, help="who is approving this promotion")
    promote.add_argument("--workspace", default="titan")
    promote.set_defaults(func=_run(_promote))

    export = actions.add_parser(
        "export-site-dataset", help="screenshots + rule labels for M1"
    )
    export.add_argument("--workspace", default="titan")
    export.add_argument("--out", default="site-dataset")
    export.add_argument("--min-count", type=int, default=20)
    export.set_defaults(func=_run(_export))


__all__ = ["add_ml_parser"]
