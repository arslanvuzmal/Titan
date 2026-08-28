"""Titan-OS operator CLI.

    titan preflight        # can this process deliver mail, and if not, why
    titan check-providers  # live health check against configured providers
    titan env-example      # regenerate .env.example from the Settings model
    titan invariants       # print the safety invariants and where each is enforced
    titan smartlead        # verify the Smartlead connection and carrier campaign,
                           # or list/manage campaigns and sending accounts
    titan set-passcode     # give an existing account a username and passcode
    titan schedules        # install the recurring jobs that close the loop
    titan sequences        # backfill the follow-up sequence on older campaigns
    titan recover-contacts # resolve contacts from crawls already on disk
    titan repoint-contacts # move leads onto the best address already crawled
    titan auth             # audit SPF/DKIM/DMARC and say what is missing
    titan trickle          # release higher-risk addresses a few a day

``env-example`` exists so that the documented environment and the code that
reads it cannot drift: the file is generated, never hand-maintained, which is
how the pre-0.2 repository ended up with 20 documented variables the runtime
could not see (gap analysis C-13).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import pathlib
import sys
import uuid
from typing import Any

from pydantic_core import PydanticUndefined

from titan import __version__
from titan.config import Settings, get_settings
from titan.intelligence import sender_auth
from titan.runtime import configure_event_loop

#: Fields whose value is a credential; the example file shows them empty.
SECRET_HINT = ("key", "secret", "token", "password", "credential")


def _is_secret(name: str) -> bool:
    return any(hint in name.lower() for hint in SECRET_HINT)


#: What `titan mailbox init` writes. Every password is a placeholder that
#: :mod:`titan.delivery.mailboxes` refuses, so a file that was created and
#: never filled in fails at the command line rather than at a provider.
MAILBOX_TEMPLATE = """{
  "_comment": [
    "One block per sending mailbox. Titan authenticates as the mailbox it puts",
    "in the From header, which is what keeps SPF and DKIM aligned.",
    "",
    "Put the app password -- not the account password -- where the placeholder",
    "is. Nothing in Titan prints these back. Keep this file out of git.",
    "",
    "Remove the imap block only if the mailbox genuinely cannot be read. A",
    "mailbox nobody reads is a mailbox whose bounces and unsubscribe requests",
    "are never collected."
  ],
  "mailboxes": [
    {
      "from_email": "outreach@example.com",
      "label": "cold outreach",
      "enabled": true,
      "smtp": {
        "host": "smtp.example.com",
        "port": 465,
        "security": "ssl",
        "username": "outreach@example.com",
        "password": "PASTE_APP_PASSWORD_HERE"
      },
      "imap": {
        "host": "imap.example.com",
        "port": 993,
        "security": "ssl",
        "username": "outreach@example.com",
        "password": "PASTE_APP_PASSWORD_HERE"
      }
    }
  ]
}
"""


def cmd_preflight(_: argparse.Namespace) -> int:
    settings = get_settings()
    blockers = settings.sending_preflight_errors()

    print(f"Titan-OS {__version__}  environment={settings.environment.value}")
    print(f"  database:        {settings.database_url.split('@')[-1]}")
    print(f"  email provider:  {settings.email_provider}")
    print(
        f"  global sending:  {'ENABLED' if settings.production_sending_enabled else 'disabled'}"
    )
    print()

    if not blockers:
        print("PROCESS GATE OPEN: this process is permitted to deliver mail.")
        print()
        print("  Delivery still requires, per message:")
        print("    - workspace sending authorized")
        print("    - campaign active and authorized")
        print("    - verified sender identity (SPF/DKIM/DMARC + mailing address)")
        print("    - lead score above the campaign threshold, no reply recorded")
        print("    - contact from an eligible source, verified, not suppressed")
        print("    - evidence-backed claims and a passing message validation")
        print("    - available workspace/campaign/sender/domain quota")
        return 0

    print(f"PROCESS GATE CLOSED: {len(blockers)} blocker(s).")
    for blocker in blockers:
        print(f"  - {blocker}")
    print()
    print("See docs/PRODUCTION-ENABLEMENT-CHECKLIST.md.")
    return 1


def cmd_consolidate(args: argparse.Namespace) -> int:
    """Merge each industry's city campaigns into one business-type campaign.

    Prints the plan and changes nothing unless ``--apply`` is given. The default
    has to be the safe one: this reassigns every lead, draft and message a
    campaign owns, and an operator who typed the wrong workspace should get a
    report rather than a migration.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from titan.db.models import Workspace
    from titan.db.session import get_sessionmaker, workspace_unit_of_work
    from titan.outreach.consolidation import apply_move, build_plan

    async def run() -> int:
        async with get_sessionmaker()() as session:
            query = select(Workspace.id, Workspace.slug).where(
                Workspace.id == _uuid.UUID(args.workspace)
                if _looks_like_uuid(args.workspace)
                else Workspace.slug == args.workspace
            )
            row = (await session.execute(query)).first()
        if row is None:
            print(f"no workspace matched {args.workspace!r}")
            return 1
        workspace_id, slug = row

        async with workspace_unit_of_work(workspace_id) as session:
            plan = await build_plan(session, workspace_id=workspace_id)

        print(f"Consolidation plan for {slug}")
        print()
        print(plan.render() or "  nothing to consolidate")
        print()

        actionable = plan.actionable
        if not actionable:
            print("Nothing to do.")
            return 0

        campaigns = sum(len(m.absorbed) for m in actionable)
        print(
            f"{campaigns} campaign(s) would be absorbed into {len(actionable)}, "
            f"and each survivor would be marked as spanning all markets."
        )
        if not args.apply:
            print("Dry run. Re-run with --apply to carry it out.")
            return 0

        for move in actionable:
            # One transaction per industry. A lead whose drafts stayed behind
            # has a draft whose campaign is not its own, and every gate that
            # reads policy from the campaign would then read the wrong one.
            async with workspace_unit_of_work(workspace_id) as session:
                moved = await apply_move(session, move, workspace_id=workspace_id)
            summary = ", ".join(f"{n} {t}" for t, n in sorted(moved.items())) or "nothing"
            print(f"  {move.industry.value:<20} moved {summary}")
        print()
        print("Done. The absorbed campaigns are paused, not deleted.")
        return 0

    configure_event_loop()
    return asyncio.run(run())


def cmd_redraft(args: argparse.Namespace) -> int:
    """Rewrite every draft the message rules would now refuse.

    Prints what it would do and changes nothing unless ``--apply`` is given.
    The default has to be the safe one: this replaces the words waiting to go to
    several hundred strangers.

    Nothing that has actually been sent is touched. There is no unsending, and
    rewriting the record of what left the building would destroy the only
    account of what a recipient read.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from titan.config import get_settings
    from titan.db.models import Workspace
    from titan.db.session import get_sessionmaker
    from titan.outreach.redraft import redraft_all

    async def run() -> int:
        async with get_sessionmaker()() as session:
            query = select(Workspace.id, Workspace.slug).where(
                Workspace.id == _uuid.UUID(args.workspace)
                if _looks_like_uuid(args.workspace)
                else Workspace.slug == args.workspace
            )
            row = (await session.execute(query)).first()
        if row is None:
            print(f"no workspace matched {args.workspace!r}")
            return 1
        workspace_id, slug = row
        owner = get_settings().owner_name

        report, lines = await redraft_all(
            workspace_id,
            owner_name=owner,
            apply=args.apply,
            limit=args.limit,
            everything=args.all,
        )

        print(f"Drafts the message rules would now refuse, in {slug}")
        print()
        if not report.stale:
            print("  none -- every draft in the queue passes.")
            return 0

        if not args.apply:
            for line in lines:
                print(line)
            print()
            print(
                f"{report.stale} draft(s) would be rewritten through the same "
                f"drafting path a new lead goes through."
            )
            print("Dry run. Re-run with --apply to carry it out.")
            return 0

        print(f"  {report.line()}")
        for code, count in sorted(report.refused.items()):
            print(f"    refused: {code:<44} {count}")
        print()
        print(
            "Rewritten drafts await approval again. A queued row keeps its "
            "place and picks up the new words, and cannot send on an approval "
            "given for the old ones."
        )
        return 0

    configure_event_loop()
    return asyncio.run(run())


def cmd_backfill_costs(args: argparse.Namespace) -> int:
    """Reprice the model calls the old adapters recorded at $0.00.

    Prints what it would do and changes nothing unless ``--apply`` is given.

    The ledger is append-only, so this corrects it the way a ledger is
    corrected: by appending an adjustment entry per underpriced call, not by
    rewriting history. Nothing that already carries a provider-reported price
    is touched, and nothing without token counts is priced -- there would be
    nothing to price it from, and a ledger figure nobody can derive is worse
    than an absent one.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from titan.db.models import Workspace
    from titan.db.session import get_sessionmaker
    from titan.models.backfill import reprice, survey

    async def run() -> int:
        async with get_sessionmaker()() as session:
            query = select(Workspace.id, Workspace.slug).where(
                Workspace.id == _uuid.UUID(args.workspace)
                if _looks_like_uuid(args.workspace)
                else Workspace.slug == args.workspace
            )
            row = (await session.execute(query)).first()
        if row is None:
            print(f"no workspace matched {args.workspace!r}")
            return 1
        workspace_id, slug = row

        report = await (reprice if args.apply else survey)(workspace_id)

        print(f"Model calls recorded at $0.00, in {slug}")
        print()
        if not report.providers:
            print("  none -- every model call in the ledger carries a price.")
            return 0

        for entry in report.providers:
            print(f"  {entry.line()}")
        print()
        print(
            f"  {report.rows} call(s) underpriced by ${report.cost_usd:.6f} at "
            f"the gateway's rate card, to be recorded as estimated."
        )
        if report.unpriceable:
            print(
                f"  {report.unpriceable} of them have no token counts and stay "
                f"at $0.00. There is nothing to price them from."
            )
        if report.reported_rows:
            print(
                f"  {report.reported_rows} call(s) already carry a "
                f"provider-reported price and are left alone."
            )
        print()
        if not report.applied:
            print(
                "The ledger is append-only, so nothing is rewritten: each "
                "underpriced call gets an adjustment entry carrying the "
                "difference, dated to the original call."
            )
            print("Dry run. Re-run with --apply to carry it out.")
            return 0

        print(f"{report.written} adjustment entry/entries appended.")
        if report.written < report.rows - report.unpriceable:
            print(
                "  The remainder were already adjusted by an earlier run and "
                "were not written twice."
            )
        print(
            "The originals are unchanged and still say what was recorded at "
            "the time. model_runs is append-only too and keeps its $0.00; "
            "usage_ledger is the authoritative cost record."
        )
        return 0

    configure_event_loop()
    return asyncio.run(run())


def cmd_check_providers(_: argparse.Namespace) -> int:
    """Live health check. Makes real calls; reports what actually happened."""
    settings = get_settings()

    async def run() -> int:
        failures = 0

        if settings.email_provider == "resend" and settings.resend_api_key:
            from titan.delivery.providers.resend import ResendProvider

            provider = ResendProvider(api_key=settings.resend_api_key.get_secret_value())
            ok, detail = await provider.health_check()
            print(f"  resend:        {'ok' if ok else 'FAIL'} - {detail}")
            failures += 0 if ok else 1
            await provider.aclose()
        else:
            print("  resend:        skipped (not configured)")

        if settings.email_provider == "smartlead" and settings.smartlead_api_key:
            from titan.providers.smartlead import SmartleadClient

            smartlead = SmartleadClient.from_settings(settings)
            ok, detail = await smartlead.health_check()
            print(f"  smartlead:     {'ok' if ok else 'FAIL'} - {detail}")
            failures += 0 if ok else 1
            await smartlead.aclose()
        else:
            print("  smartlead:     skipped (not configured)")

        if settings.email_provider == "instantly" and settings.instantly_api_key:
            from titan.providers.instantly import InstantlyClient

            instantly = InstantlyClient(settings.instantly_api_key.get_secret_value())
            ok, detail = await instantly.health_check()
            print(f"  instantly:     {'ok' if ok else 'FAIL'} - {detail}")
            failures += 0 if ok else 1
            if ok and settings.instantly_campaign_id:
                # The carrier campaign must send exactly one step, and the only
                # place that is currently discovered is the first send. Checked
                # here so a misshapen campaign is found while an operator is
                # watching, not when a message is already in the queue.
                from titan.delivery.providers.instantly import InstantlyProvider

                shape_ok, shape_detail = await InstantlyProvider(
                    instantly, campaign_id=settings.instantly_campaign_id
                ).verify_campaign_shape()
                label = "ok" if shape_ok else "FAIL"
                print(f"  instantly campaign: {label} - {shape_detail}")
                failures += 0 if shape_ok else 1
            elif ok:
                print("  instantly campaign: no TITAN_INSTANTLY_CAMPAIGN_ID set")
                failures += 1
            await instantly.aclose()
        else:
            print("  instantly:     skipped (not configured)")

        try:
            from sqlalchemy import text

            from titan.db.session import get_engine

            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            print("  database:      ok")
        except Exception as exc:
            print(f"  database:      FAIL - {type(exc).__name__}: {exc}")
            failures += 1

        for label, configured in (
            ("nvidia", settings.nvidia_api_key is not None),
            ("gemini", settings.gemini_api_key is not None),
            ("openrouter", settings.openrouter_api_key is not None),
            ("cloudflare", settings.cloudflare_api_token is not None),
            ("google places", settings.google_places_api_key is not None),
            ("agent reach", settings.agent_reach_api_key is not None),
        ):
            # Reported as "configured", never as "working": claiming a provider
            # works without a real call is exactly the kind of unverified
            # assertion this project refuses to make.
            print(
                f"  {label + ':':<14} {'configured' if configured else 'not configured'}"
            )

        return failures

    print(f"Titan-OS provider health ({settings.environment.value})")
    configure_event_loop()
    failures = asyncio.run(run())
    print()
    print("OK" if failures == 0 else f"{failures} check(s) failed")
    return 0 if failures == 0 else 1


def cmd_smartlead(args: argparse.Namespace) -> int:
    """Operator surface for the connected Smartlead account.

    Read-mostly. The one mutating action, ``status``, is the same control an
    operator has in the Smartlead UI. Nothing here can cause a send: only the
    outbox worker hands a message over, and only after every gate has passed.
    """
    settings = get_settings()
    if settings.smartlead_api_key is None:
        print("TITAN_SMARTLEAD_API_KEY is not set. Add it to .env and retry.")
        return 1

    # Unwrapped once here rather than inside the closure: the None check above
    # does not narrow across a nested function.
    api_key = settings.smartlead_api_key.get_secret_value()

    from titan.providers.smartlead import SmartleadClient, SmartleadError

    async def run() -> int:
        client = SmartleadClient.from_settings(settings)
        try:
            if args.action == "campaigns":
                campaigns = await client.list_campaigns()
                if not campaigns:
                    print("No campaigns visible to this key.")
                    return 0
                print(f"{'ID':<10} {'STATUS':<10} NAME")
                for campaign in sorted(campaigns, key=lambda c: c.id):
                    marker = (
                        " <- carrier"
                        if campaign.id == settings.smartlead_campaign_id
                        else ""
                    )
                    print(
                        f"{campaign.id:<10} {campaign.status:<10} {campaign.name}{marker}"
                    )
                return 0

            if args.action == "accounts":
                accounts = await client.list_email_accounts()
                if not accounts:
                    print("No sending accounts on this Smartlead workspace.")
                    return 0
                for account in accounts:
                    print(
                        f"{account.get('id'):<10} {account.get('from_email', '?')}  "
                        f"warmup={account.get('warmup_details') is not None}"
                    )
                return 0

            if args.action == "status":
                if args.campaign is None or args.value is None:
                    print("usage: titan smartlead status --campaign ID --value START")
                    return 2
                await client.set_campaign_status(args.campaign, args.value)
                print(f"campaign {args.campaign} set to {args.value}")
                return 0

            # verify
            ok, detail = await client.health_check()
            print(f"  connection:    {'ok' if ok else 'FAIL'} - {detail}")
            if not ok:
                return 1
            if settings.smartlead_campaign_id is None:
                print(
                    "  carrier:       NOT CONFIGURED - set TITAN_SMARTLEAD_CAMPAIGN_ID "
                    "to a single-step campaign"
                )
                return 1

            from titan.delivery.providers.smartlead import SmartleadProvider

            provider = SmartleadProvider(
                api_key, settings.smartlead_campaign_id, client=client
            )
            shape_ok, shape_detail = await provider.verify_campaign_shape()
            print(f"  carrier shape: {'ok' if shape_ok else 'FAIL'} - {shape_detail}")
            return 0 if shape_ok else 1
        except SmartleadError as exc:
            print(f"FAIL - {exc}")
            return 1
        finally:
            await client.aclose()

    print(f"Titan-OS Smartlead ({settings.environment.value})")
    configure_event_loop()
    return asyncio.run(run())


def cmd_validate_models(_: argparse.Namespace) -> int:
    """Check every configured model route against its provider's live catalogue.

    The `TITAN_MODEL_ROUTE_*` defaults are plausible identifiers, not verified
    ones. This is how an operator finds out a model does not exist before a
    campaign does.
    """
    from titan.models.gateway import ModelGateway
    from titan.models.providers import build_providers

    settings = get_settings()
    providers = build_providers(settings)
    if not providers:
        print("No model providers are configured.")
        print("Set at least one of TITAN_NVIDIA_API_KEY, TITAN_GEMINI_API_KEY,")
        print("TITAN_OPENROUTER_API_KEY, or the Cloudflare gateway variables.")
        return 1

    gateway = ModelGateway(providers, settings)
    configure_event_loop()
    report = asyncio.run(gateway.validate_models())

    print(f"Titan-OS model routes ({', '.join(sorted(providers))})")
    print()
    for entry in report["routes"]:
        status = entry.get("status", "unknown")
        marker = "ok  " if status == "ok" else "FAIL"
        print(
            f"  [{marker}] {entry['task']:<13} {entry.get('provider', '?')}:{entry.get('model_id', '?')}"
        )
        if entry.get("detail"):
            print(f"           {entry['detail']}")
    print()
    print("All routes valid." if report["ok"] else "One or more routes are invalid.")
    return 0 if report["ok"] else 1


def cmd_env_example(args: argparse.Namespace) -> int:
    """Generate .env.example from the Settings model."""
    lines: list[str] = [
        "# Titan-OS environment.",
        "#",
        "# GENERATED FILE -- do not edit by hand.",
        "#   cd apps/api && python -m titan.cli env-example > ../../.env.example",
        "#",
        "# Every variable below is declared in titan/config.py. Nothing else in",
        "# the codebase reads configuration, so this file is exhaustive by",
        "# construction rather than by discipline.",
        "#",
        "# Secrets are intentionally blank. Never commit a real value.",
        "",
    ]

    for name, field in Settings.model_fields.items():
        env_name = f"TITAN_{name.upper()}"
        description = (field.description or "").strip()
        if description:
            lines.append(f"# {description}")

        if _is_secret(name):
            lines.append(f"{env_name}=")
            continue

        default: Any = field.default
        if default is PydanticUndefined or default is None:
            lines.append(f"{env_name}=")
        else:
            value = default.value if hasattr(default, "value") else default
            if isinstance(value, bool):
                value = str(value).lower()
            lines.append(f"{env_name}={value}")

    output = "\n".join(lines) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(output)
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(output)
    return 0


def cmd_invariants(_: argparse.Namespace) -> int:
    """Print each safety invariant and where it is enforced."""
    rows = [
        ("1", "A model cannot send email", "tests/invariants (AST scan)"),
        ("2", "Browser content cannot alter policy", "titan/policy (pure functions)"),
        ("3", "Arbitrary crawling only in the isolated worker", "tests/invariants"),
        ("4", "No send without an outbox row", "titan/delivery/outbox_worker.py"),
        ("5", "No send to a suppressed recipient", "titan/delivery/suppression.py"),
        ("6", "No send to a guessed email", "titan/policy/engine.py"),
        (
            "7",
            "No send without evidence-backed claims",
            "titan/intelligence/message_validator.py",
        ),
        ("8", "No send when globally disabled", "titan/config.py kill switch"),
        ("9", "No send when the campaign is paused", "titan/policy/engine.py"),
        ("10", "No send without sender authorization", "titan/db/models/identity.py"),
        ("11", "A retry cannot duplicate an email", "outbox provider_idempotency_key"),
        (
            "12",
            "A duplicate webhook cannot duplicate state",
            "UNIQUE(provider, event_id)",
        ),
        ("13", "A delayed webhook cannot regress state", "messages.state_rank"),
        ("14", "Concurrent workers cannot exceed quota", "titan/delivery/quotas.py"),
        ("15", "A replied lead gets no follow-up", "leads.replied_at"),
        ("16", "Bounce/complaint suppresses", "titan/delivery/webhooks.py"),
        ("17", "No cross-workspace access", "titan/db/session.py + RLS"),
        ("18", "A request cannot override persisted policy", "campaign_policies"),
        ("19", "API keys never in logs or responses", "titan/security/redaction.py"),
        ("20", "LeadPilot is not a runtime dependency", "tests/invariants"),
        ("21", "Production sending disabled by default", "titan/config.py"),
        ("22", "Research/draft modes work without email auth", "titan/policy/modes.py"),
    ]
    print(f"{'#':>3}  {'Invariant':<48}  Enforced by")
    print("-" * 100)
    for number, statement, where in rows:
        print(f"{number:>3}  {statement:<48}  {where}")
    return 0


def cmd_set_passcode(args: argparse.Namespace) -> int:
    """Give an existing account a username and a passcode.

    An operator command rather than a route. Titan has no outbound path for a
    reset link -- the delivery layer is the thing being protected -- so a
    self-service reset would either mail through the outreach mailbox or invent
    a second, unaudited sender. Whoever can run this already has the database.

    Creates nothing. The account and its workspace membership must exist, so
    that granting access stays a deliberate act and a typo in a username cannot
    conjure a member.
    """
    import getpass

    from sqlalchemy import func, select

    from titan.api.passwords import PasscodeRejected, check_strength, hash_passcode
    from titan.db.models import User, WorkspaceMember
    from titan.db.session import get_sessionmaker

    settings = get_settings()

    if args.passcode:
        passcode = args.passcode
    elif not sys.stdin.isatty():
        # Scripted use: `echo 'secret' | titan set-passcode --user ...`
        passcode = sys.stdin.readline().rstrip("\n")
    else:
        passcode = getpass.getpass("New passcode: ")
        if passcode != getpass.getpass("Repeat passcode: "):
            print("passcodes did not match", file=sys.stderr)
            return 2

    try:
        check_strength(passcode, minimum_length=settings.min_passcode_length)
    except PasscodeRejected as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    if passcode.isdigit():
        print(
            f"note: an all-digit passcode of {len(passcode)} characters is "
            f"{10 ** len(passcode):,} guesses. Online that is safe -- "
            f"{settings.login_max_attempts} attempts then a "
            f"{settings.login_lockout_seconds}s lock. Against a stolen "
            "database it is not; the argon2 hash is the only thing standing "
            "between that dump and a working login.",
            file=sys.stderr,
        )

    username = args.username.strip().lower()
    digest = hash_passcode(passcode)

    async def run() -> int:
        async with get_sessionmaker()() as session:
            user = (
                await session.execute(
                    select(User).where(func.lower(User.email) == args.email.lower())
                )
            ).scalar_one_or_none()
            if user is None:
                print(f"no user with email {args.email}", file=sys.stderr)
                return 1

            clash = (
                await session.execute(
                    select(User).where(
                        func.lower(User.username) == username, User.id != user.id
                    )
                )
            ).scalar_one_or_none()
            if clash is not None:
                print(f"username {username!r} is taken", file=sys.stderr)
                return 1

            members = (
                (
                    await session.execute(
                        select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)
                    )
                )
                .scalars()
                .all()
            )
            if not members:
                # A passcode without a membership authenticates and then
                # authorizes nothing, which reads as a broken login.
                print(
                    f"{args.email} has no workspace membership; add one first",
                    file=sys.stderr,
                )
                return 1

            user.username = username
            user.password_hash = digest
            user.failed_login_count = 0
            user.locked_until = None
            await session.commit()

        print(f"{args.email} can now sign in as {username!r}.")
        print(f"  workspaces: {len(members)}")
        print("  the passcode is stored as an argon2id hash and is not recoverable.")
        return 0

    configure_event_loop()
    return asyncio.run(run())


def _looks_like_uuid(value: str) -> bool:
    import uuid as _uuid

    try:
        _uuid.UUID(value)
    except ValueError:
        return False
    return True


def cmd_status(args: argparse.Namespace) -> int:
    """The six numbers, and anything currently wrong.

    The whole point is that it takes one command and no interpretation. Every
    fault this system has had was visible in these numbers days before anyone
    noticed, and none of them was anywhere a person would look.
    """
    import asyncio
    import uuid as _uuid

    from sqlalchemy import select

    from titan.activities.vitals import check_pipeline_vitals
    from titan.db.models import Workspace
    from titan.db.session import dispose_engine, get_sessionmaker
    from titan.workflows.types import CheckVitalsInput

    async def run() -> int:
        async with get_sessionmaker()() as session:
            query = select(Workspace.id).where(
                Workspace.id == _uuid.UUID(args.workspace)
                if _looks_like_uuid(args.workspace)
                else Workspace.slug == args.workspace
            )
            workspace_id = (await session.execute(query)).scalar_one_or_none()
        if workspace_id is None:
            print(f"no workspace matching {args.workspace!r}")
            return 1

        result = await check_pipeline_vitals(
            CheckVitalsInput(workspace_id=str(workspace_id))
        )
        await dispose_engine()

        print(f"Titan-OS  {args.workspace}\n")
        print(result.reading)
        if result.alarms or result.suppressed:
            print("\n  alarms")
            for code in result.alarms:
                print(f"    ! {code}")
            for code in result.suppressed:
                print(f"    . {code}  (already raised today)")
        else:
            print("\n  no alarms")
        return 0

    return asyncio.run(run())


def cmd_sweep(args: argparse.Namespace) -> int:
    """Queue the approved drafts nothing ever queued.

    Queueing lives inside LeadResearchWorkflow, one step after the approval it
    waits for. When the workflow is no longer running at that moment -- the
    approval window elapsed, the worker restarted, the run was cancelled -- the
    draft is approved, valid, and invisible to everything downstream.

    Each one is handed to the same queue_message activity the workflow would
    have called, so every gate is re-applied by the same code. Idempotent: the
    activity dedupes on the draft id.
    """
    import asyncio
    import uuid as _uuid

    from sqlalchemy import select

    from titan.activities.stranded import sweep_stranded_drafts
    from titan.db.models import Workspace
    from titan.db.session import dispose_engine, get_sessionmaker
    from titan.workflows.types import SweepStrandedInput

    async def run() -> int:
        async with get_sessionmaker()() as session:
            query = select(Workspace.id).where(
                Workspace.id == _uuid.UUID(args.workspace)
                if _looks_like_uuid(args.workspace)
                else Workspace.slug == args.workspace
            )
            workspace_id = (await session.execute(query)).scalar_one_or_none()
        if workspace_id is None:
            print(f"no workspace matching {args.workspace!r}")
            return 1
        result = await sweep_stranded_drafts(
            SweepStrandedInput(workspace_id=str(workspace_id), limit=args.limit)
        )
        print(f"found   {result.found}")
        print(f"queued  {result.queued}")
        print(f"refused {result.refused}")
        for reason, count in result.refused_reasons:
            print(f"          {count:>4}  {reason}")
        await dispose_engine()
        return 0

    configure_event_loop()
    return asyncio.run(run())


def cmd_schedules(args: argparse.Namespace) -> int:
    """Install the recurring jobs, or show what installing would do.

    Two separate acts, deliberately not one. Installing the schedules turns on
    *measurement*: the weekly report and sender re-verification, both of which
    only read Titan's database and the DNS records a human already published.
    Starting the campaign loops turns on *work* -- discovery, drafting and
    queueing -- and needs ``--start-campaigns`` said out loud.

    Nothing here sends mail. A queued message still passes every delivery gate
    in the outbox worker, and approval gating still applies. But the difference
    between "the reports arrive" and "the machine is running" is worth a flag.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from titan.db.enums import CampaignStatus
    from titan.db.models import Campaign, Workspace
    from titan.db.session import get_sessionmaker
    from titan.workers.temporal_worker import RESEARCH_QUEUE, connect
    from titan.workflows import schedules

    async def run() -> int:
        async with get_sessionmaker()() as session:
            # One workspace by slug or id, or every one. Defaulting to all is
            # right for a single-tenant deployment and wrong the moment it is
            # not, so an operator can name one.
            query = select(Workspace.id, Workspace.slug)
            if args.workspace:
                query = query.where(
                    Workspace.slug == args.workspace
                    if not _looks_like_uuid(args.workspace)
                    else Workspace.id == _uuid.UUID(args.workspace)
                )
            workspaces = list((await session.execute(query)).all())
            campaigns_by_ws: dict[_uuid.UUID, list] = {}
            rows = (
                await session.execute(
                    select(Campaign.workspace_id, Campaign.id, Campaign.status)
                )
            ).all()
            for ws_id, campaign_id, status in rows:
                campaigns_by_ws.setdefault(ws_id, []).append((campaign_id, status))

        if not workspaces:
            if args.workspace:
                print(f"no workspace matching {args.workspace!r}")
                return 1
            print("no workspaces; nothing to schedule")
            return 0

        jobs: list = []
        starts: list = []
        for ws_id, slug in workspaces:
            jobs.extend(schedules.plan_schedules(ws_id, task_queue=RESEARCH_QUEUE))
            if args.start_campaigns:
                starts.extend(
                    schedules.plan_orchestrators(
                        ws_id,
                        campaigns_by_ws.get(ws_id, []),
                        task_queue=RESEARCH_QUEUE,
                    )
                )
            skipped = [
                c
                for c, st in campaigns_by_ws.get(ws_id, [])
                if st is not CampaignStatus.ACTIVE
            ]
            if skipped and args.start_campaigns:
                print(f"{slug}: {len(skipped)} campaign(s) not active; not started")

        if args.dry_run:
            print("PLAN (nothing was changed)")
            for job in jobs:
                print(f"  schedule  {job.schedule_id}  cron={job.cron!r}  {job.note}")
            for start in starts:
                print(f"  loop      {start.workflow_id}")
            if not args.start_campaigns:
                print("  (campaign loops omitted; pass --start-campaigns to include)")
            return 0

        client = await connect()
        applied = await schedules.install(client, jobs)
        if starts:
            applied.extend(await schedules.start_orchestrators(client, starts))
        print(schedules.summarise(applied))
        return 1 if any(a.outcome is schedules.Outcome.FAILED for a in applied) else 0

    configure_event_loop()
    return asyncio.run(run())


def cmd_sequences(args: argparse.Namespace) -> int:
    """Give campaigns created before sequence provisioning existed their steps.

    Every campaign made before this was wired has no ``email_sequences`` row, so
    the follow-up scheduler finds nothing owed and each lead is contacted once.
    New campaigns get theirs at creation; this is for the ones that predate it.

    Idempotent: a campaign that already has an active sequence is left alone,
    because replacing it would orphan the drafts referencing its steps.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from titan.db.models import Campaign, Workspace
    from titan.db.session import workspace_unit_of_work
    from titan.outreach.provisioning import ensure_sequence

    async def run() -> int:
        from titan.db.session import get_sessionmaker

        async with get_sessionmaker()() as session:
            query = select(Workspace.id, Workspace.slug)
            if args.workspace:
                query = query.where(
                    Workspace.slug == args.workspace
                    if not _looks_like_uuid(args.workspace)
                    else Workspace.id == _uuid.UUID(args.workspace)
                )
            workspaces = (await session.execute(query)).all()

        if not workspaces:
            print("no workspaces matched")
            return 1

        created = 0
        skipped = 0
        for workspace_id, slug in workspaces:
            async with workspace_unit_of_work(workspace_id) as session:
                campaigns = (await session.execute(select(Campaign))).scalars().all()
                for campaign in campaigns:
                    if args.dry_run:
                        print(f"  would check  {slug}/{campaign.slug}")
                        continue
                    sequence = await ensure_sequence(
                        session,
                        workspace_id=workspace_id,
                        campaign_id=campaign.id,
                    )
                    if sequence is None:
                        skipped += 1
                        print(f"  has one      {slug}/{campaign.slug}")
                    else:
                        created += 1
                        print(f"  provisioned  {slug}/{campaign.slug}")

        if args.dry_run:
            print("PLAN (nothing was changed)")
            return 0
        print(f"{created} provisioned, {skipped} already had a sequence")
        return 0

    configure_event_loop()
    return asyncio.run(run())


def cmd_mailbox(args: argparse.Namespace) -> int:
    """Inspect and prove the sending pool, without ever printing a password.

    Four things an operator needs and had no way to get: a file to fill in, a
    view of what is in it, proof that each credential actually opens its
    mailbox, and proof that a message sent as one of them arrives. The last
    two are separate on purpose -- a mailbox can authenticate perfectly and
    still have its mail refused at the far end.
    """
    from titan.delivery.mailboxes import MailboxConfigError, load_mailboxes

    settings = get_settings()

    if args.mailbox_command == "init":
        target = pathlib.Path(args.path)
        if target.exists() and not args.force:
            print(f"{target} already exists. Pass --force to overwrite it.")
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(MAILBOX_TEMPLATE, encoding="utf-8")
        try:
            # Best effort: on Windows this is a no-op, and the file is on the
            # operator's own machine either way. Worth doing where it works.
            target.chmod(0o600)
        except OSError:
            pass
        print(f"Wrote {target}.")
        print()
        print("Fill in one block per mailbox and put the app password where the")
        print("placeholder is. Nothing in Titan will print it back. Then:")
        print()
        print(f"  TITAN_MAILBOX_FILE={target}")
        print("  TITAN_EMAIL_PROVIDER=smtp_pool")
        print()
        print("and run `titan mailbox check` to prove each one opens.")
        return 0

    path = args.path or settings.mailbox_file
    if not path:
        print("No mailbox file. Set TITAN_MAILBOX_FILE or pass --path.")
        print("`titan mailbox init --path secrets/mailboxes.json` writes one.")
        return 1

    try:
        registry = load_mailboxes(path)
    except MailboxConfigError as exc:
        print(f"{path} cannot be used: {exc}")
        return 1

    if not registry:
        print(f"{path} lists no enabled mailboxes.")
        return 1

    if args.mailbox_command == "list":
        print(f"Mailboxes in {path}")
        print()
        for account in registry.accounts():
            smtp = account.smtp
            print(f"  {account.from_email}")
            if account.label:
                print(f"      {account.label}")
            print(
                f"      smtp  {smtp.username}@{smtp.host}:{smtp.port} "
                f"({smtp.security}), password set"
            )
            if account.imap:
                imap = account.imap
                print(
                    f"      imap  {imap.username}@{imap.host}:{imap.port} "
                    f"({imap.security}), password set"
                )
            else:
                print("      imap  NOT CONFIGURED -- bounces and unsubscribe")
                print("            requests to this address will not be collected")
        print()
        print(f"{len(registry)} mailbox(es), {len(registry.readable())} readable.")
        return 0

    async def run() -> int:
        from titan.delivery.mailbox import ImapConfig, ImapMailbox
        from titan.delivery.providers.smtp_pool import SmtpPoolProvider

        failures = 0

        if args.mailbox_command == "check":
            provider = SmtpPoolProvider(
                registry, timeout_seconds=float(settings.smtp_timeout_seconds)
            )
            _, detail = await provider.health_check()
            # The aggregate boolean is "can this pool send at all", which is
            # not the question here: one broken mailbox out of three must show
            # as one broken mailbox, so the per-mailbox lines are what is
            # counted.
            for line in detail.split("; ")[1:]:
                print(f"  {line}")
                failures += 1 if "FAILED" in line else 0
            print()

            for account in registry.readable():
                imap = account.imap
                assert imap is not None
                mailbox = ImapMailbox(
                    ImapConfig(
                        host=imap.host,
                        port=imap.port,
                        username=imap.username,
                        password=imap.password,
                        security=imap.security,
                        folder=settings.imap_folder,
                    )
                )
                imap_ok, imap_detail = await mailbox.health_check()
                label = "ok" if imap_ok else "FAILED"
                print(f"  {account.from_email} imap: {label} -- {imap_detail}")
                failures += 0 if imap_ok else 1

            unreadable = [a.from_email for a in registry.accounts() if a.imap is None]
            if unreadable:
                print()
                print("  Not readable, so nobody will see their bounces:")
                for address in unreadable:
                    print(f"    {address}")
            return 1 if failures else 0

        # -------------------------------------------------------------- test
        # A real message, to an address the operator named on the command line.
        # Not to a lead, and not from the queue: this proves the transport, and
        # a transport proved against a stranger is a stranger who got a test.
        from titan.delivery.providers.base import OutboundEmail

        provider = SmtpPoolProvider(
            registry, timeout_seconds=float(settings.smtp_timeout_seconds)
        )
        sources = [args.mailbox] if args.mailbox else registry.addresses()
        for address in sources:
            if registry.get(address) is None:
                print(f"  {address}: not in {path}")
                failures += 1
                continue
            stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%SZ")
            result = await provider.send(
                OutboundEmail(
                    to_email=args.to,
                    from_email=address,
                    from_name="Titan",
                    reply_to=address,
                    subject=f"Titan delivery test from {address}",
                    text_body=(
                        f"This is a delivery test sent by Titan at {stamp}.\n\n"
                        f"It was sent from {address}, authenticated as that "
                        f"mailbox.\n\nIf it reached the inbox rather than spam, "
                        f"this mailbox is ready to send.\n"
                    ),
                    idempotency_key=f"mailbox-test:{address}:{stamp}",
                )
            )
            if result.accepted:
                print(f"  {address}: sent -- {result.provider_message_id}")
            else:
                print(
                    f"  {address}: FAILED -- {result.error_kind}: {result.error_detail}"
                )
                failures += 1
        print()
        print(f"Check {args.to}, including its spam folder.")
        return 1 if failures else 0

    configure_event_loop()
    return asyncio.run(run())


#: How many addresses one transaction covers. Small enough that a long run
#: does not hold row locks the discovery pipeline needs.
VERIFY_BATCH = 40


def cmd_verify_contacts(args: argparse.Namespace) -> int:
    """Re-check addresses stored before there was a verifier configured.

    Verification runs at discovery, so every address found while
    TITAN_MAILBOX_VERIFIER was 'null' carries the answer available then, which
    was none. This is the catch-up pass, through the same verifier and the same
    resolution the discovery path uses.

    Prints what it would change and changes nothing, unless --apply.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from titan.db.models import Workspace
    from titan.db.session import dispose_engine, get_sessionmaker, workspace_unit_of_work
    from titan.intelligence.reverification import reverify
    from titan.intelligence.verifier import build_verifier

    settings = get_settings()

    async def run() -> int:
        async with get_sessionmaker()() as session:
            query = select(Workspace.id).where(
                Workspace.id == _uuid.UUID(args.workspace)
                if _looks_like_uuid(args.workspace)
                else Workspace.slug == args.workspace
            )
            workspace_id = (await session.execute(query)).scalar_one_or_none()
        if workspace_id is None:
            print(f"no workspace matching {args.workspace!r}")
            return 1

        verifier = build_verifier(settings.mailbox_verifier, settings)
        if verifier.name == "null":
            # Not an error, but running it would examine everything, learn
            # nothing and report a clean sweep -- which is worse than saying so.
            print(
                f"TITAN_MAILBOX_VERIFIER is {settings.mailbox_verifier!r}, which "
                f"resolves to the null verifier. It answers UNKNOWN for every "
                f"address, so this pass would check nothing."
            )
            return 1

        ok, detail = await verifier.health_check()
        print(f"verifier: {verifier.name} - {detail}")
        if not ok:
            return 1
        print()

        # In committed batches, not one long transaction. 604 addresses at two
        # seconds a domain is half an hour, and half an hour of open
        # transaction holds row locks on contact_channels that the discovery
        # pipeline is writing to at the same time.
        from titan.intelligence.reverification import ReverifyReport

        report = ReverifyReport()
        offset = 0
        while report.examined < args.limit:
            batch_size = min(VERIFY_BATCH, args.limit - report.examined)
            async with workspace_unit_of_work(workspace_id) as session:
                batch = await reverify(
                    session,
                    workspace_id=workspace_id,
                    verifier=verifier,
                    limit=batch_size,
                    offset=offset,
                    apply=args.apply,
                )
            if batch.examined == 0:
                break
            report.examined += batch.examined
            report.checked += batch.checked
            report.changed += batch.changed
            report.downgraded.extend(batch.downgraded)
            for status, count in batch.outcomes.items():
                report.outcomes[status] = report.outcomes.get(status, 0) + count
            # A dry run writes nothing, so every row it looked at is still in
            # the result set and the window has not moved under it.
            offset += batch.examined if not args.apply else batch.remained
            print(
                f"  ...{report.examined} checked, {report.changed} changed",
                flush=True,
            )
        print()

        print(f"examined  {report.examined}")
        print(f"checked   {report.checked}")
        print(f"changed   {report.changed}")
        for status, count in sorted(report.outcomes.items(), key=lambda kv: -kv[1]):
            print(f"            {count:>5}  {status}")
        if report.downgraded:
            print()
            print("no longer sendable -- each one a hard bounce that will not happen:")
            for line in report.downgraded[:25]:
                print(f"  {line}")
            if len(report.downgraded) > 25:
                print(f"  ... and {len(report.downgraded) - 25} more")
        # Asked again after the run: the probe counts how many servers refused
        # the probe itself, and that is one fact about this host rather than
        # many facts about other people's mailboxes.
        _, after = await verifier.health_check()
        if "refused the probe itself" in after:
            print()
            print(f"note: {after.split('; ', 2)[-1]}")

        if not args.apply:
            print()
            print("Dry run. Re-run with --apply to record these.")
        await dispose_engine()
        return 0

    configure_event_loop()
    return asyncio.run(run())


async def _warmup_days(workspace_id: uuid.UUID) -> dict[str, int]:
    """Each sending mailbox's position on its ramp, by address.

    Read from the sender identities rather than from the credential file: the
    file says how to log in, the identity carries the send history, and warm-up
    volume is a property of the history.
    """
    from sqlalchemy import func, select

    from titan.db.models import Message, SenderIdentity
    from titan.db.session import workspace_session
    from titan.delivery.deliverability import warmup_day

    now = dt.datetime.now(dt.UTC)
    days: dict[str, int] = {}
    async with workspace_session(workspace_id) as session:
        rows = (
            (
                await session.execute(
                    select(SenderIdentity).where(SenderIdentity.is_active.is_(True))
                )
            )
            .scalars()
            .all()
        )
        for identity in rows:
            first = await session.scalar(
                select(func.min(Message.sent_at)).where(
                    Message.sender_identity_id == identity.id,
                    Message.state == "sent",
                )
            )
            # The earlier of the two wins, so a mailbox warmed elsewhere before
            # Titan saw it is not put back on day zero.
            started = min(
                [d for d in (first, identity.warmup_started_at) if d], default=None
            )
            days[identity.from_email.lower()] = warmup_day(started, now)
    return days


def cmd_recover_contacts(args: argparse.Namespace) -> int:
    """Resolve contacts for leads whose research already crawled the address.

    Until the ``capture-contact-below-threshold`` patch, a lead that scored
    under its campaign's bar returned BELOW_THRESHOLD *before* the contact
    stage ran. The crawl had already happened, the address was already sitting
    in the page evidence, and it was dropped along with the lead. The patch
    fixed the path; it cannot reach backwards, because these leads will not be
    researched again.

    On the live workspace that is 1,939 organisations whose stored pages carry
    a published address and which have no contact row -- every one of them
    already paid for in crawl time.

    This does not crawl, guess or construct anything. It re-reads pages already
    in the database and runs the same ``resolve_contact`` activity the pipeline
    runs, so provenance, eligibility, MX and verification are applied exactly
    as they would have been at the time. Invariant 6 holds for the same reason
    it holds in the pipeline: the only addresses considered are ones the
    crawler recorded verbatim from the site.

    Prints what it would create and creates nothing, unless --apply.
    """
    import uuid as _uuid

    from sqlalchemy import cast, func, select
    from sqlalchemy.dialects.postgresql import JSONB

    from titan.contracts.evidence import PageEvidence
    from titan.db.models import (
        Contact,
        CrawlRun,
        Lead,
        Organization,
        Page,
        ResearchRun,
        Workspace,
    )
    from titan.db.session import dispose_engine, get_sessionmaker
    from titan.intelligence.contacts import extract_contacts_from_pages

    async def run() -> int:
        async with get_sessionmaker()() as session:
            workspace_id = (
                await session.execute(
                    select(Workspace.id).where(
                        Workspace.id == _uuid.UUID(args.workspace)
                        if _looks_like_uuid(args.workspace)
                        else Workspace.slug == args.workspace
                    )
                )
            ).scalar_one_or_none()
            if workspace_id is None:
                print(f"no workspace matching {args.workspace!r}")
                return 1

            # The most recent completed run per lead, for leads whose
            # organisation has no contact at all. One run, not all of them:
            # re-reading five historical crawls of the same site would find the
            # same address five times.
            latest = (
                select(
                    ResearchRun.lead_id,
                    func.max(ResearchRun.created_at).label("newest"),
                )
                .where(
                    ResearchRun.workspace_id == workspace_id,
                    ResearchRun.status == "completed",
                )
                .group_by(ResearchRun.lead_id)
                .subquery()
            )
            # Only leads whose crawl actually recorded an address.
            #
            # Without this the query returns the same first N contactless leads
            # on every call -- nothing in it orders differently between runs --
            # so a batch that finds nothing finds the identical nothing next
            # time, and a loop over batches makes no progress at all. Measured:
            # 150 examined, 0 found, repeatedly.
            #
            # Restricting to pages carrying a visible_emails entry selects the
            # population that can actually produce a contact (1,939 orgs), and
            # a lead drops out of it the moment one is stored -- so the batches
            # drain. It is also far faster: the previous query spent its whole
            # budget parsing page evidence for sites that published no address.
            has_published_address = (
                select(Page.id)
                .join(CrawlRun, CrawlRun.id == Page.crawl_run_id)
                .where(
                    CrawlRun.research_run_id == ResearchRun.id,
                    func.jsonb_array_length(
                        func.coalesce(
                            Page.observations["visible_emails"],
                            cast("[]", JSONB),
                        )
                    )
                    > 0,
                )
                .exists()
            )
            candidates = (
                (
                    await session.execute(
                        select(
                            ResearchRun.id,
                            Lead.id,
                            Lead.campaign_id,
                            Organization.id,
                            Organization.canonical_domain,
                        )
                        .join(latest, latest.c.lead_id == ResearchRun.lead_id)
                        .where(ResearchRun.created_at == latest.c.newest)
                        .join(Lead, Lead.id == ResearchRun.lead_id)
                        .join(Organization, Organization.id == Lead.organization_id)
                        .where(
                            ~select(Contact.id)
                            .where(Contact.organization_id == Organization.id)
                            .exists(),
                            has_published_address,
                        )
                        .limit(args.limit)
                    )
                )
                .tuples()
                .all()
            )

            planned: list[tuple[str, str]] = []
            nothing_published: list[str] = []
            for run_id, lead_id, campaign_id, org_id, domain in candidates:
                crawls = (
                    (
                        await session.execute(
                            select(CrawlRun.id).where(
                                CrawlRun.research_run_id == run_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                pages: list[PageEvidence] = []
                for crawl_id in crawls:
                    rows = (
                        (
                            await session.execute(
                                select(Page.observations).where(
                                    Page.crawl_run_id == crawl_id
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for observations in rows:
                        try:
                            pages.append(PageEvidence.model_validate(observations))
                        except Exception:
                            # A page whose evidence will not parse is one this
                            # pass skips, not one it fails on. The pipeline
                            # would have had the same trouble with it.
                            continue
                usable = [
                    candidate
                    for candidate in extract_contacts_from_pages(pages, domain)
                    if candidate.is_usable
                ]
                if usable:
                    planned.append((domain or str(org_id), usable[0].normalized))
                else:
                    nothing_published.append(domain or str(org_id))

        print(
            f"examined       {len(candidates)} leads whose crawl found an address"
        )
        print(f"address found  {len(planned)}")
        print(f"nothing to use {len(nothing_published)}")
        for domain, address in planned[:20]:
            print(f"  {domain:<40} {address}")
        if len(planned) > 20:
            print(f"  ... and {len(planned) - 20} more")

        if not args.apply:
            print()
            print("dry run. Re-run with --apply to resolve and store these.")
            return 0

        # The real path, not a reimplementation of it: the activity applies
        # campaign policy, MX, verification and suppression, and writes the
        # contact and its channel in one unit of work.
        from titan.activities.pipeline import resolve_contact
        from titan.workflows.types import ContactActivityInput

        stored = 0
        refused = 0
        for run_id, lead_id, campaign_id, _org_id, domain in candidates:
            try:
                result = await resolve_contact(
                    ContactActivityInput(
                        workspace_id=str(workspace_id),
                        lead_id=str(lead_id),
                        campaign_id=str(campaign_id),
                        research_run_id=str(run_id),
                        idempotency_key=f"recover:{lead_id}:contact",
                    )
                )
            except Exception as error:  # one bad lead must not stop the pass
                print(f"  ! {domain}: {type(error).__name__}: {error}")
                refused += 1
                continue
            if result.eligible_channel_id:
                stored += 1
            else:
                refused += 1
        print()
        print(f"stored {stored} contacts, {refused} produced none")
        return 0

    try:
        return asyncio.run(run())
    finally:
        asyncio.run(dispose_engine())


def cmd_repoint_contacts(args: argparse.Namespace) -> int:
    """Point each lead at the best address its crawl already found.

    Until the ranking fix, ``resolve_contact`` returned on the *first* eligible
    candidate and iteration order was page-crawl order -- so a firm publishing
    ``info@`` on its contact page and a partner's mailbox on its people page
    was written to at whichever page the crawler reached first. It also stored
    only that one address, so the better alternative was never even recorded.

    The pages are still here. This re-reads them, re-ranks the candidates with
    today's rules, and repoints the lead when a front desk exists and the
    current address is not one. On the live workspace that is 154 of 311 leads,
    including ``careers@faceretreat.com`` (a hiring inbox, now refused outright)
    and ``adele.nicol@andersonstrathern.co.uk`` (a named partner) both of which
    had ``info@`` published on the same site all along.

    Nothing is crawled and no address is constructed: every candidate is a
    string the crawler recorded verbatim. Prints what it would change and
    changes nothing, unless --apply.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from titan.contracts.evidence import PageEvidence
    from titan.db.models import (
        Contact,
        ContactChannel,
        CrawlRun,
        Lead,
        Organization,
        Page,
        ResearchRun,
        Workspace,
    )
    from titan.db.session import (
        dispose_engine,
        get_sessionmaker,
        workspace_unit_of_work,
    )
    from titan.intelligence.contacts import (
        extract_contacts_from_pages,
        is_never_contact,
        is_role_address,
        rank_contacts,
    )

    async def run() -> int:
        async with get_sessionmaker()() as session:
            workspace_id = (
                await session.execute(
                    select(Workspace.id).where(
                        Workspace.id == _uuid.UUID(args.workspace)
                        if _looks_like_uuid(args.workspace)
                        else Workspace.slug == args.workspace
                    )
                )
            ).scalar_one_or_none()
            if workspace_id is None:
                print(f"no workspace matching {args.workspace!r}")
                return 1

            leads = (
                (
                    await session.execute(
                        select(
                            Lead.id,
                            Organization.id,
                            Organization.canonical_domain,
                            ContactChannel.id,
                            ContactChannel.normalized_value,
                        )
                        .join(Organization, Organization.id == Lead.organization_id)
                        .join(
                            ContactChannel,
                            ContactChannel.id == Lead.primary_contact_channel_id,
                        )
                        .where(Lead.workspace_id == workspace_id)
                    )
                )
                .tuples()
                .all()
            )

            # Only leads currently pointed at something that is not a front
            # desk. A lead already on info@ has nothing to gain and re-reading
            # its pages would cost the same as one that does.
            candidates = [
                row
                for row in leads
                if not is_role_address(row[4]) or is_never_contact(row[4])
            ]
            print(f"leads not on a front-desk address: {len(candidates)}")

            planned: list[tuple[uuid.UUID, uuid.UUID, str, str, str]] = []
            for lead_id, org_id, domain, channel_id, current in candidates[
                : args.limit
            ]:
                runs = (
                    (
                        await session.execute(
                            select(ResearchRun.id).where(ResearchRun.lead_id == lead_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                pages: list[PageEvidence] = []
                for run_id in runs:
                    crawls = (
                        (
                            await session.execute(
                                select(CrawlRun.id).where(
                                    CrawlRun.research_run_id == run_id
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for crawl_id in crawls:
                        for observations in (
                            (
                                await session.execute(
                                    select(Page.observations).where(
                                        Page.crawl_run_id == crawl_id
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        ):
                            try:
                                pages.append(PageEvidence.model_validate(observations))
                            except Exception:
                                continue
                if not pages:
                    continue
                ranked = [
                    c
                    for c in rank_contacts(
                        extract_contacts_from_pages(pages, domain)
                    )
                    if c.is_usable
                ]
                if not ranked:
                    continue
                best = ranked[0]
                # Only ever a move *to* a front desk. A sideways move between
                # two named mailboxes buys nothing and changes who a stranger
                # hears from, which is not a change worth making silently.
                if best.normalized == current or not best.is_generic_role:
                    continue
                # `canonical_domain` is nullable; the report prints the org
                # id when it is absent, so the tuple carries a str either way.
                planned.append(
                    (lead_id, org_id, domain or str(org_id), current, best.normalized)
                )

        print(f"a front desk is available instead:  {len(planned)}")
        for _, _, domain, current, better in planned[:15]:
            print(f"  {domain:<34} {current:<36} -> {better}")
        if len(planned) > 15:
            print(f"  ... and {len(planned) - 15} more")

        if not args.apply:
            print()
            print("dry run. Re-run with --apply to repoint these.")
            return 0

        moved = 0
        for lead_id, org_id, _domain, current, better in planned:
            async with workspace_unit_of_work(workspace_id) as session:
                contact_id = (
                    await session.execute(
                        select(Contact.id).where(Contact.organization_id == org_id)
                    )
                ).scalar_one_or_none()
                if contact_id is None:
                    continue
                existing = (
                    await session.execute(
                        select(ContactChannel).where(
                            ContactChannel.contact_id == contact_id,
                            ContactChannel.normalized_value == better,
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    existing = ContactChannel(
                        workspace_id=workspace_id,
                        contact_id=contact_id,
                        channel_type="email",
                        value=better,
                        normalized_value=better,
                        value_domain=better.split("@", 1)[1],
                        source="first_party_website",
                        discovered_at=dt.datetime.now(dt.UTC),
                        verification_status="published_first_party",
                        confidence=0.8,
                        is_active=True,
                    )
                    session.add(existing)
                    await session.flush()
                else:
                    existing.is_active = True
                lead = await session.get(Lead, lead_id)
                if lead is not None:
                    lead.primary_contact_channel_id = existing.id
                # The address we are moving off is retired only when today's
                # rules refuse it outright -- a hiring inbox, a privacy desk.
                # An ordinary named mailbox stays on file, inactive as a
                # primary but not erased: it was really published there.
                if is_never_contact(current):
                    old = (
                        await session.execute(
                            select(ContactChannel).where(
                                ContactChannel.contact_id == contact_id,
                                ContactChannel.normalized_value == current,
                            )
                        )
                    ).scalar_one_or_none()
                    if old is not None:
                        old.is_active = False
                moved += 1

        print()
        print(f"repointed {moved} leads onto a front-desk address")
        return 0

    try:
        return asyncio.run(run())
    finally:
        asyncio.run(dispose_engine())


def cmd_auth(args: argparse.Namespace) -> int:
    """Audit sender authentication and print the records that are missing.

    Everything here is read from public DNS -- nothing is changed, and nothing
    *can* be changed from here: these are records only the domain owner can
    publish. The point is to say precisely which ones, in the form they go in.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from titan.db.models import SenderIdentity, Workspace
    from titan.db.session import dispose_engine, get_sessionmaker
    from titan.delivery import dns_auth

    async def run() -> int:
        async with get_sessionmaker()() as session:
            workspace_id = (
                await session.execute(
                    select(Workspace.id).where(
                        Workspace.id == _uuid.UUID(args.workspace)
                        if _looks_like_uuid(args.workspace)
                        else Workspace.slug == args.workspace
                    )
                )
            ).scalar_one_or_none()
            if workspace_id is None:
                print(f"no workspace matching {args.workspace!r}")
                return 1
            senders = (
                (
                    await session.execute(
                        select(
                            SenderIdentity.from_email, SenderIdentity.sending_domain
                        ).where(
                            SenderIdentity.workspace_id == workspace_id,
                            SenderIdentity.is_active.is_(True),
                        )
                    )
                )
                .tuples()
                .all()
            )

        if not senders:
            print("no active sender identities")
            return 1

        # One report per sending domain, not per mailbox: SPF, DKIM and DMARC
        # are properties of the domain, and three mailboxes on one domain would
        # otherwise print the same findings three times.
        seen: dict[str, str] = {}
        for from_email, domain in senders:
            seen.setdefault(domain, from_email)

        failures = 0
        for domain, from_email in sorted(seen.items()):
            report = dns_auth.verify_sender_domain(
                from_email=from_email,
                sending_domain=domain,
                dkim_selectors=sender_auth.COMMON_DKIM_SELECTORS,
            )
            print(f"\n{domain}")
            for check in (report.spf, report.dkim, report.dmarc, report.alignment):
                mark = "ok  " if check.ok else "FAIL"
                print(f"  {mark} {check.name:<10} {check.detail}")
            for warning in report.warnings:
                print(f"  warn {warning}")
            if not report.ok:
                failures += 1

            missing = _missing_hardening(domain)
            if missing:
                print("\n  Not required to deliver, but this is what separates a")
                print("  domain that is merely accepted from one that is trusted:")
                for label, record, value in missing:
                    print(f"\n    {label}")
                    print(f"      {record}")
                    print(f"      {value}")

        print()
        return 1 if failures else 0

    try:
        return asyncio.run(run())
    finally:
        asyncio.run(dispose_engine())


def _missing_hardening(domain: str) -> list[tuple[str, str, str]]:
    """Records that are absent and worth publishing, with their exact contents.

    Deliberately not "best practice" boilerplate. Each entry is here because it
    changes how a receiver treats this mail:

    * **MTA-STS** tells a receiver to refuse to deliver to us over an
      unencrypted connection, which closes a downgrade attack and is one of the
      signals Google publishes as contributing to sender reputation.
    * **TLS-RPT** is how you find out it broke.

    DMARC policy is handled by ``check_dmarc`` itself and appears as a warning
    above rather than being repeated here.
    """
    import dns.resolver

    def present(name: str) -> bool:
        try:
            dns.resolver.resolve(name, "TXT", lifetime=8)
            return True
        except Exception:
            return False

    out: list[tuple[str, str, str]] = []
    if not present(f"_mta-sts.{domain}"):
        out.append(
            (
                "MTA-STS -- refuse unencrypted delivery",
                f"_mta-sts.{domain}  TXT",
                "v=STSv1; id=20260828T000000Z",
            )
        )
        out.append(
            (
                "  ...and the policy it points at, served over HTTPS",
                f"https://mta-sts.{domain}/.well-known/mta-sts.txt",
                "version: STSv1 / mode: testing / mx: <your mx> / max_age: 604800",
            )
        )
    if not present(f"_smtp._tls.{domain}"):
        out.append(
            (
                "TLS-RPT -- get told when encrypted delivery fails",
                f"_smtp._tls.{domain}  TXT",
                f"v=TLSRPTv1; rua=mailto:admin@{domain}",
            )
        )
    return out


def cmd_trickle(args: argparse.Namespace) -> int:
    """Hold the higher-risk addresses, and release a few each day.

    ``--hold`` deactivates every lead whose only published address is a named
    or departmental mailbox. Without it, the command *releases* up to
    ``--limit`` of those held, oldest first, so a scheduled run drips them back
    into the queue at a rate the bounce rate can absorb.

    A channel released here is an ordinary channel again. Nothing marks it, and
    a later ``--hold`` will pick it up again if it has still not been written
    to -- which is what makes running this daily safe.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from titan.db.models import Contact, ContactChannel, Lead, Workspace
    from titan.db.session import (
        dispose_engine,
        get_sessionmaker,
        workspace_unit_of_work,
    )
    from titan.intelligence.contacts import is_never_contact, is_role_address

    async def run() -> int:
        async with get_sessionmaker()() as session:
            workspace_id = (
                await session.execute(
                    select(Workspace.id).where(
                        Workspace.id == _uuid.UUID(args.workspace)
                        if _looks_like_uuid(args.workspace)
                        else Workspace.slug == args.workspace
                    )
                )
            ).scalar_one_or_none()
            if workspace_id is None:
                print(f"no workspace matching {args.workspace!r}")
                return 1

            rows = (
                (
                    await session.execute(
                        select(
                            ContactChannel.id,
                            ContactChannel.normalized_value,
                            ContactChannel.is_active,
                            Lead.id,
                            Lead.last_contacted_at,
                        )
                        .join(Contact, Contact.id == ContactChannel.contact_id)
                        .join(Lead, Lead.primary_contact_channel_id == ContactChannel.id)
                        .where(ContactChannel.workspace_id == workspace_id)
                        .order_by(ContactChannel.created_at)
                    )
                )
                .tuples()
                .all()
            )

        # Never-contact addresses are not "risky", they are refused. They are
        # excluded here so a release can never hand one back.
        risky = [
            row
            for row in rows
            if not is_role_address(row[1])
            and not is_never_contact(row[1])
            and row[4] is None
        ]
        held = [r for r in risky if not r[2]]
        live = [r for r in risky if r[2]]

        print(f"leads on a named or departmental mailbox: {len(risky)}")
        print(f"  currently held back                    {len(held)}")
        print(f"  currently releasable to the queue      {len(live)}")

        if args.hold:
            target = live
            verb = "hold back"
        else:
            target = held[: args.limit]
            verb = "release"
        print(f"\nwould {verb}: {len(target)}")
        for row in target[:10]:
            print(f"  {row[1]}")
        if len(target) > 10:
            print(f"  ... and {len(target) - 10} more")

        if not args.apply:
            print()
            print("dry run. Re-run with --apply to carry it out.")
            return 0

        changed = 0
        async with workspace_unit_of_work(workspace_id) as session:
            for channel_id, _email, _active, _lead_id, _contacted in target:
                channel = await session.get(ContactChannel, channel_id)
                if channel is None:
                    continue
                channel.is_active = not args.hold
                changed += 1
        print()
        print(f"{verb}: {changed}")
        return 0

    try:
        return asyncio.run(run())
    finally:
        asyncio.run(dispose_engine())


def cmd_warmup(args: argparse.Namespace) -> int:
    """Give the mailboxes a history, rather than waiting for one.

    The ramp -- how much a mailbox may send today -- has been running all
    along. This is the other half: mail that is actually delivered, opened,
    rescued from the spam folder and replied to, so that the receiving networks
    have seen the mailbox behave like a person before it writes to a stranger.

    Every recipient is a mailbox in your own credential file. There is no path
    here to a lead.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from titan.db.models import Workspace
    from titan.db.session import dispose_engine, get_sessionmaker
    from titan.delivery.mailboxes import MailboxConfigError, load_mailboxes
    from titan.delivery.warmup import (
        check_recipients_are_participants,
        describe_pool,
        participants_from,
        plan,
        send_round,
        tend,
    )

    settings = get_settings()
    path = args.path or settings.mailbox_file
    if not path:
        print("No mailbox file. Set TITAN_MAILBOX_FILE or pass --path.")
        return 1
    try:
        registry = load_mailboxes(path)
    except MailboxConfigError as exc:
        print(f"{path} cannot be used: {exc}")
        return 1

    async def run() -> int:
        async with get_sessionmaker()() as session:
            query = select(Workspace.id).where(
                Workspace.id == _uuid.UUID(args.workspace)
                if _looks_like_uuid(args.workspace)
                else Workspace.slug == args.workspace
            )
            workspace_id = (await session.execute(query)).scalar_one_or_none()
        if workspace_id is None:
            print(f"no workspace matching {args.workspace!r}")
            return 1

        days = await _warmup_days(workspace_id)
        participants = participants_from(registry, days=days)

        print(describe_pool(participants))
        print()
        for participant in participants:
            print(
                f"  {participant.address:38} day {participant.day:>2}  "
                f"sends {participant.volume_today()} warm-up message(s) today"
            )
        print()

        if args.warmup_command == "status":
            await dispose_engine()
            return 0

        today = plan(participants)
        check_recipients_are_participants(today, participants)
        print(f"Today's plan: {len(today)} message(s)")
        for line in today[:20]:
            print(f"  {line.describe()}")
        if len(today) > 20:
            print(f"  ... and {len(today) - 20} more")
        print()

        if args.warmup_command == "plan" or not args.apply:
            print("Nothing sent. Re-run `titan warmup run --apply` to carry it out.")
            await dispose_engine()
            return 0

        sent = await send_round(
            today, timeout_seconds=float(settings.smtp_timeout_seconds)
        )
        print(
            f"sent {sent.sent}, skipped {sent.skipped_already_sent} already "
            f"delivered, {sent.failed} failed"
        )

        # The receiving half, in the same run: anything filed as junk is moved
        # back, everything is read, and a share is answered. Rescuing a message
        # from spam is the single most valuable signal warm-up produces.
        tended = await tend(
            participants, timeout_seconds=float(settings.smtp_timeout_seconds)
        )
        print(
            f"rescued {tended.rescued_from_spam} from spam, read "
            f"{tended.marked_read}, replied to {tended.replied}"
        )
        for problem in (sent.errors + tended.errors)[:10]:
            print(f"  ! {problem}")

        await dispose_engine()
        return 1 if (sent.failed or sent.errors or tended.errors) else 0

    configure_event_loop()
    return asyncio.run(run())


def main() -> int:
    parser = argparse.ArgumentParser(prog="titan", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "preflight", help="report whether this process may send mail"
    ).set_defaults(func=cmd_preflight)
    sub.add_parser("check-providers", help="live provider health check").set_defaults(
        func=cmd_check_providers
    )
    sub.add_parser(
        "validate-models", help="check model routes against live catalogues"
    ).set_defaults(func=cmd_validate_models)
    env_parser = sub.add_parser("env-example", help="regenerate .env.example")
    env_parser.add_argument("-o", "--output", help="write to a file instead of stdout")
    env_parser.set_defaults(func=cmd_env_example)
    sub.add_parser("invariants", help="print safety invariants").set_defaults(
        func=cmd_invariants
    )
    smartlead_parser = sub.add_parser(
        "smartlead", help="inspect and manage the connected Smartlead account"
    )
    smartlead_parser.add_argument(
        "action",
        nargs="?",
        default="verify",
        choices=["verify", "campaigns", "accounts", "status"],
        help="verify the connection and carrier campaign, or list/manage campaigns",
    )
    smartlead_parser.add_argument(
        "--campaign", type=int, default=None, help="campaign id, for 'status'"
    )
    smartlead_parser.add_argument(
        "--value",
        default=None,
        choices=["START", "PAUSED", "STOPPED"],
        help="new status, for 'status'",
    )
    smartlead_parser.set_defaults(func=cmd_smartlead)

    passcode_parser = sub.add_parser(
        "set-passcode", help="set an existing account's username and passcode"
    )
    passcode_parser.add_argument(
        "--email", required=True, help="the existing account to give a passcode"
    )
    passcode_parser.add_argument(
        "--username", required=True, help="the sign-in handle (stored lowercased)"
    )
    passcode_parser.add_argument(
        "--passcode",
        default=None,
        help=(
            "the passcode. Omit it: the value lands in your shell history "
            "otherwise. Prompts on a terminal, or reads one line from stdin."
        ),
    )
    passcode_parser.set_defaults(func=cmd_set_passcode)

    status_parser = sub.add_parser(
        "status",
        help="the six numbers that say whether the pipeline is alive",
    )
    status_parser.add_argument("--workspace", default="titan")
    status_parser.set_defaults(func=cmd_status)

    sweep_parser = sub.add_parser(
        "sweep",
        help="queue approved drafts that nothing ever queued",
    )
    sweep_parser.add_argument("--workspace", default="titan")
    sweep_parser.add_argument("--limit", type=int, default=None)
    sweep_parser.set_defaults(func=cmd_sweep)

    schedules_parser = sub.add_parser(
        "schedules",
        help="install the recurring jobs that close the loop",
    )
    schedules_parser.add_argument(
        "--workspace",
        default=None,
        help="a workspace slug or id; defaults to every workspace",
    )
    schedules_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be installed and change nothing",
    )
    schedules_parser.add_argument(
        "--start-campaigns",
        action="store_true",
        help=(
            "also start the always-on loop for each ACTIVE campaign. Separate "
            "from installing the schedules because this one starts work."
        ),
    )
    schedules_parser.set_defaults(func=cmd_schedules)

    sequences_parser = sub.add_parser(
        "sequences",
        help="give campaigns created before provisioning existed their steps",
    )
    sequences_parser.add_argument(
        "--workspace", default=None, help="workspace slug or id (default: all)"
    )
    sequences_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print which campaigns would be checked and change nothing",
    )
    sequences_parser.set_defaults(func=cmd_sequences)

    consolidate_parser = sub.add_parser(
        "consolidate",
        help="merge each industry's city campaigns into one business-type campaign",
    )
    consolidate_parser.add_argument("--workspace", default="titan")
    consolidate_parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "carry the plan out. Without this the command prints what it would "
            "do and changes nothing."
        ),
    )
    consolidate_parser.set_defaults(func=cmd_consolidate)

    mailbox_parser = sub.add_parser(
        "mailbox",
        help="the mailboxes Titan sends as, and whether each one works",
    )
    mailbox_sub = mailbox_parser.add_subparsers(dest="mailbox_command", required=True)

    mailbox_init = mailbox_sub.add_parser("init", help="write a mailbox file to fill in")
    mailbox_init.add_argument("--path", default="secrets/mailboxes.json")
    mailbox_init.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing file, losing the passwords already in it",
    )

    mailbox_list = mailbox_sub.add_parser(
        "list", help="what is configured, with the passwords redacted"
    )
    mailbox_list.add_argument("--path", default=None)

    mailbox_check = mailbox_sub.add_parser(
        "check", help="log in to every mailbox, over SMTP and IMAP"
    )
    mailbox_check.add_argument("--path", default=None)

    mailbox_test = mailbox_sub.add_parser(
        "test",
        help=(
            "send one real message from each mailbox to an address you name, "
            "to prove the transport before any lead is written to"
        ),
    )
    mailbox_test.add_argument("--path", default=None)
    mailbox_test.add_argument(
        "--to", required=True, help="where the test messages go. Your own address."
    )
    mailbox_test.add_argument(
        "--mailbox",
        default=None,
        help="send from only this one, instead of every mailbox in the file",
    )

    mailbox_parser.set_defaults(func=cmd_mailbox)

    verify_parser = sub.add_parser(
        "verify-contacts",
        help="re-check addresses stored before a verifier was configured",
    )
    verify_parser.add_argument("--workspace", default="titan")
    verify_parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help=(
            "how many to check in this pass. Bounded because every one of "
            "these is a connection to somebody else's mail server."
        ),
    )
    verify_parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "record the answers. Without this the command prints what it "
            "would change and changes nothing."
        ),
    )
    verify_parser.set_defaults(func=cmd_verify_contacts)

    recover_parser = sub.add_parser(
        "recover-contacts",
        help="resolve contacts for leads whose crawl already found the address",
    )
    recover_parser.add_argument("--workspace", default="titan")
    recover_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help=(
            "how many leads to examine in this pass. Bounded because --apply "
            "runs MX and verification per address."
        ),
    )
    recover_parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "resolve and store. Without this the command prints what it would "
            "create and creates nothing."
        ),
    )
    recover_parser.set_defaults(func=cmd_recover_contacts)

    trickle_parser = sub.add_parser(
        "trickle",
        help="hold the higher-risk addresses and release a few each day",
    )
    trickle_parser.add_argument("--workspace", default="titan")
    trickle_parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help=(
            "how many to release in this run. Five a day against a 1.3% base "
            "rate keeps the blended rate under the 2% that blocks a mailbox."
        ),
    )
    trickle_parser.add_argument(
        "--hold",
        action="store_true",
        help="deactivate them all instead of releasing. Run once, at the start.",
    )
    trickle_parser.add_argument("--apply", action="store_true")
    trickle_parser.set_defaults(func=cmd_trickle)

    auth_parser = sub.add_parser(
        "auth",
        help="audit SPF, DKIM, DMARC and print the records that are missing",
    )
    auth_parser.add_argument("--workspace", default="titan")
    auth_parser.set_defaults(func=cmd_auth)

    repoint_parser = sub.add_parser(
        "repoint-contacts",
        help="move leads onto the best address their crawl already found",
    )
    repoint_parser.add_argument("--workspace", default="titan")
    repoint_parser.add_argument("--limit", type=int, default=500)
    repoint_parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "carry it out. Without this the command prints what it would "
            "change and changes nothing."
        ),
    )
    repoint_parser.set_defaults(func=cmd_repoint_contacts)

    warmup_parser = sub.add_parser(
        "warmup",
        help="give the mailboxes a history: delivered, read and replied-to mail",
    )
    warmup_sub = warmup_parser.add_subparsers(dest="warmup_command", required=True)
    for name, blurb in (
        ("status", "the pool, and where each mailbox is on its ramp"),
        ("plan", "who would write to whom today; sends nothing"),
        ("run", "carry out today's plan, then tend the mailboxes"),
    ):
        sub_parser = warmup_sub.add_parser(name, help=blurb)
        sub_parser.add_argument("--workspace", default="titan")
        sub_parser.add_argument("--path", default=None)
        if name == "run":
            sub_parser.add_argument(
                "--apply",
                action="store_true",
                help=(
                    "actually send. Without it the plan is printed and nothing "
                    "leaves. Recipients are only ever mailboxes in your own "
                    "credential file."
                ),
            )
    warmup_parser.set_defaults(func=cmd_warmup)

    redraft_parser = sub.add_parser(
        "redraft",
        help="rewrite every draft the message rules would now refuse",
    )
    redraft_parser.add_argument("--workspace", default="titan")
    redraft_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="rewrite at most this many, for a first pass you can read",
    )
    redraft_parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "rewrite every unsent draft, not only the ones a rule would "
            "refuse. For an improvement to the writing that no rule can catch."
        ),
    )
    redraft_parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "carry it out. Without this the command prints what it would "
            "rewrite and changes nothing."
        ),
    )
    redraft_parser.set_defaults(func=cmd_redraft)

    backfill_parser = sub.add_parser(
        "backfill-costs",
        help="reprice model runs that were recorded as free",
    )
    backfill_parser.add_argument("--workspace", default="titan")
    backfill_parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "carry it out. Without this the command prints what it would "
            "reprice and changes nothing."
        ),
    )
    backfill_parser.set_defaults(func=cmd_backfill_costs)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
