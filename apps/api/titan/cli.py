"""Titan-OS operator CLI.

    titan preflight        # can this process deliver mail, and if not, why
    titan check-providers  # live health check against configured providers
    titan env-example      # regenerate .env.example from the Settings model
    titan invariants       # print the safety invariants and where each is enforced

``env-example`` exists so that the documented environment and the code that
reads it cannot drift: the file is generated, never hand-maintained, which is
how the pre-0.2 repository ended up with 20 documented variables the runtime
could not see (gap analysis C-13).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from pydantic_core import PydanticUndefined

from titan import __version__
from titan.config import Settings, get_settings
from titan.runtime import configure_event_loop

#: Fields whose value is a credential; the example file shows them empty.
SECRET_HINT = ("key", "secret", "token", "password", "credential")


def _is_secret(name: str) -> bool:
    return any(hint in name.lower() for hint in SECRET_HINT)


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
    env_parser = sub.add_parser("env-example", help="regenerate .env.example")
    env_parser.add_argument("-o", "--output", help="write to a file instead of stdout")
    env_parser.set_defaults(func=cmd_env_example)
    sub.add_parser("invariants", help="print safety invariants").set_defaults(
        func=cmd_invariants
    )

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
