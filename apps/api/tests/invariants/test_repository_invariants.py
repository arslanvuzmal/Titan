"""Repository-level invariant tests (mission section 28).

These do not test behaviour -- they test the *shape of the codebase*, so that a
future change cannot quietly reintroduce a defect the audit already found. They
are static: an AST scan over the source tree, with no database and no network.

Each test names the invariant it defends and fails with an actionable message.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

# .../apps/api/tests/invariants/this_file.py -> repo root is 4 levels up.
REPO = pathlib.Path(__file__).resolve().parents[4]
API = REPO / "apps" / "api"
TITAN = API / "titan"
BROWSER_WORKER = REPO / "apps" / "browser-worker"


def python_sources() -> list[pathlib.Path]:
    return [
        p
        for p in TITAN.rglob("*.py")
        if "migrations" not in p.parts and "__pycache__" not in p.parts
    ]


def parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


# ==========================================================================
# Invariants 1 and 4: nothing but the outbox worker may reach a provider
# ==========================================================================
#: Modules permitted to import a concrete email provider.
PROVIDER_IMPORT_ALLOWLIST = {
    "titan/delivery/outbox_worker.py",
    "titan/delivery/providers/__init__.py",
    "titan/delivery/providers/base.py",
    "titan/delivery/providers/mock.py",
    "titan/delivery/providers/resend.py",
    "titan/delivery/providers/smartlead.py",
    "titan/delivery/providers/smtp.py",
    "titan/delivery/webhooks.py",  # verification + normalization only
    "titan/workers/outbox.py",  # the outbox worker process entrypoint
    "titan/cli.py",  # health checks and preflight
}

PROVIDER_MODULES = (
    "titan.delivery.providers.resend",
    "titan.delivery.providers.mock",
    "titan.delivery.providers.smartlead",
    "titan.delivery.providers.smtp",
)


def test_only_the_outbox_worker_imports_an_email_provider() -> None:
    """Invariant 1 and 4.

    The pre-0.2 code had a model-invocable tool that POSTed straight to
    SendGrid. This test makes reintroducing that a CI failure rather than a
    review oversight.
    """
    offenders: list[str] = []
    for path in python_sources():
        rel = path.relative_to(API).as_posix()
        if rel in PROVIDER_IMPORT_ALLOWLIST:
            continue
        modules = imported_modules(parse(path))
        if any(m in modules for m in PROVIDER_MODULES):
            offenders.append(rel)
    assert not offenders, (
        "these modules import a concrete email provider but are not the outbox "
        f"worker: {offenders}. Route the send through titan.delivery.outbox_worker."
    )


def test_no_module_calls_a_third_party_email_api_directly() -> None:
    """No raw HTTP to a known email provider outside the provider adapters."""
    email_hosts = re.compile(
        r"https?://(?:api\.)?(?:sendgrid|mailgun|postmark(?:app)?|sparkpost|"
        r"mandrillapp|brevo|sendinblue)\.",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for path in python_sources():
        text = path.read_text(encoding="utf-8")
        if email_hosts.search(text):
            offenders.append(path.relative_to(API).as_posix())
    assert not offenders, f"direct third-party email API references in {offenders}"


def test_the_deleted_sendgrid_tool_has_not_returned() -> None:
    """The specific defect from gap analysis C-01."""
    for path in python_sources():
        text = path.read_text(encoding="utf-8")
        assert "api.sendgrid.com" not in text, (
            f"{path.relative_to(API)} contains a SendGrid endpoint; the direct-send "
            "tool was removed in the 0.2 hardening pass and must not return"
        )


# ==========================================================================
# Invariant 3: arbitrary crawling only in the isolated worker
# ==========================================================================
def test_no_credentialled_module_fetches_arbitrary_urls() -> None:
    """Invariant 3.

    The control plane may call *named* providers (Resend, Places, model
    gateways) but must never fetch a URL that came from a crawled page. Those
    fetches belong in the browser worker, which holds no credentials.
    """
    banned_calls = {"urlopen", "urlretrieve"}
    offenders: list[str] = []
    for path in python_sources():
        tree = parse(path)
        modules = imported_modules(tree)
        if {"urllib.request", "requests"} & modules:
            offenders.append(f"{path.relative_to(API)} imports a blocking HTTP fetcher")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in banned_calls:
                    offenders.append(f"{path.relative_to(API)} calls {node.func.attr}")
    assert not offenders, offenders


def test_browser_worker_holds_no_delivery_or_model_credentials() -> None:
    """The browser worker is the blast radius of a browser escape."""
    forbidden = re.compile(
        r"RESEND_API_KEY|SENDGRID|DATABASE_URL|NVIDIA_API_KEY|GEMINI_API_KEY|"
        r"OPENROUTER_API_KEY|CLOUDFLARE_API_TOKEN|GOOGLE_PLACES_API_KEY",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for path in list((BROWSER_WORKER / "src").rglob("*.ts")) + list(
        (BROWSER_WORKER / "fixtures").rglob("*.ts")
    ):
        if forbidden.search(path.read_text(encoding="utf-8")):
            offenders.append(path.relative_to(REPO).as_posix())
    assert not offenders, (
        f"browser worker references a privileged credential in {offenders}; it must "
        "receive only a bounded research request"
    )


# ==========================================================================
# Invariant 21: production sending is disabled by default
# ==========================================================================
def test_production_sending_defaults_to_false() -> None:
    from titan.config import Settings

    assert Settings(environment="test").production_sending_enabled is False


def test_email_provider_defaults_to_mock() -> None:
    from titan.config import Settings

    assert Settings(environment="test").email_provider == "mock"


def test_default_settings_report_blockers_for_sending() -> None:
    from titan.config import Settings

    errors = Settings(environment="test").sending_preflight_errors()
    assert errors, "default settings claimed sending was permitted"


def test_is_deployed_covers_staging_as_well_as_production() -> None:
    """Staging is a deployed environment, not a local one.

    Controls keyed off ``is_production`` alone (the passwordless local login,
    the published OpenAPI schema) treated a networked staging host as a
    developer laptop.
    """
    from titan.config import Settings

    assert Settings(environment="local").is_deployed is False
    assert Settings(environment="test").is_deployed is False
    for deployed in ("staging", "production"):
        settings = Settings(
            environment=deployed,
            auth_mode="local",
            local_jwt_secret="not-a-real-secret",
        )
        assert settings.is_deployed is True


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_a_deployed_environment_refuses_to_start_without_an_auth_secret(
    environment: str,
) -> None:
    from pydantic import ValidationError
    from titan.config import Settings

    with pytest.raises(ValidationError, match="TITAN_LOCAL_JWT_SECRET"):
        Settings(environment=environment, auth_mode="local", local_jwt_secret=None)

    with pytest.raises(ValidationError, match="TITAN_CLERK_ISSUER_URL"):
        Settings(environment=environment, auth_mode="clerk", clerk_issuer_url=None)


def test_operating_mode_defaults_to_the_most_restrictive() -> None:
    from titan.config import OperatingMode
    from titan.db.models import Campaign, CampaignPolicy, Workspace

    assert Workspace.__table__.c.operating_mode.default.arg is OperatingMode.RESEARCH_ONLY
    assert (
        CampaignPolicy.__table__.c.operating_mode.default.arg
        is OperatingMode.RESEARCH_ONLY
    )
    assert Workspace.__table__.c.sending_authorized.default.arg is False
    assert CampaignPolicy.__table__.c.sending_authorized.default.arg is False
    _ = Campaign  # imported for the registry side effect


# ==========================================================================
# Invariant 6: a guessed address is never eligible
# ==========================================================================
def test_pattern_guess_is_absent_from_eligible_sources() -> None:
    from titan.db.enums import ELIGIBLE_CONTACT_SOURCES, ContactSource

    assert ContactSource.PATTERN_GUESS not in ELIGIBLE_CONTACT_SOURCES


def test_campaign_policy_default_sources_exclude_guesses() -> None:
    from titan.db.models import CampaignPolicy

    default = CampaignPolicy.__table__.c.allowed_contact_sources.default.arg
    # SQLAlchemy wraps a zero-argument default callable so it accepts an
    # execution context; pass a placeholder to evaluate it.
    values = default(None) if callable(default) else default
    assert "pattern_guess" not in values


# ==========================================================================
# Invariant 17: every tenant table is scoped
# ==========================================================================
def test_all_domain_tables_are_workspace_scoped() -> None:
    """Only genuinely global tables may lack workspace_id."""
    from titan.db.models import Base

    GLOBAL_TABLES = {"users", "workspaces"}
    # Checks the column, not the mixin: workspace_members declares workspace_id
    # explicitly so it can carry a composite unique constraint, and is scoped
    # just as strictly.
    unscoped = {
        mapper.class_.__tablename__
        for mapper in Base.registry.mappers
        if "workspace_id" not in mapper.class_.__table__.c
    }
    assert unscoped <= GLOBAL_TABLES, (
        f"these tables are not workspace-scoped: {sorted(unscoped - GLOBAL_TABLES)}. "
        "Add the WorkspaceScoped mixin or justify the exception here."
    )


#: Raw SQL against a scoped table that deliberately does not name workspace_id,
#: keyed by (file, table) with the reason it is safe.
#:
#: Exactly one entry, and it should stay that way. The outbox claim is genuinely
#: cross-workspace -- one worker drains every workspace's queue, which is why it
#: runs on an unscoped session and why it cannot name a single workspace.
#:
#: The sender reputation queries were briefly listed here too, on the reasoning
#: that filtering by ``sender_identity_id`` scopes them by construction. True,
#: but it exempted the whole file: any *new* query against ``messages`` in the
#: outbox worker would have inherited the exemption silently. They now name the
#: workspace explicitly and the entry is gone.
RAW_SQL_SCOPE_ALLOWLIST = {
    ("titan/delivery/outbox_worker.py", "outbox_messages"),
}


def test_raw_sql_against_a_scoped_table_names_the_workspace() -> None:
    """``workspace_session`` does not scope ``text()``. Not at all.

    Its guard is ``with_loader_criteria``, which rewrites ORM entity queries and
    has no effect on raw SQL; and the row-level security policy is permissive
    when ``titan.workspace_id`` is unset, which is how migrations, the outbox
    claim and webhook ingestion legitimately run unscoped. A raw SELECT inside a
    scoped session therefore inherits no isolation whatsoever -- it only looks as
    though it does, which is the dangerous part.

    Found the hard way: the recipient-domain history query grouped by
    ``to_domain`` with no workspace predicate, and one workspace's bounce record
    silently downgraded another workspace's lead. Nothing about the call site
    hinted at it.
    """
    from titan.db.models import Base

    scoped_tables = {
        mapper.class_.__tablename__
        for mapper in Base.registry.mappers
        if "workspace_id" in mapper.class_.__table__.c
    }
    # A text() block, from the opening paren to the closing triple quote.
    blocks = re.compile(r"text\(\s*(?:\"\"\"|''')(.*?)(?:\"\"\"|''')", re.DOTALL)

    offenders: list[str] = []
    for path in python_sources():
        rel = path.relative_to(API).as_posix()
        if rel.startswith("titan/db/migrations/"):
            continue
        for sql in blocks.findall(path.read_text(encoding="utf-8")):
            lowered = sql.lower()
            if "workspace_id" in lowered:
                continue
            for table in scoped_tables:
                if not re.search(rf"\b(?:from|join|update|into)\s+{table}\b", lowered):
                    continue
                if (rel, table) in RAW_SQL_SCOPE_ALLOWLIST:
                    continue
                offenders.append(f"{rel}: {table}")

    assert not offenders, (
        f"raw SQL touches a workspace-scoped table without naming workspace_id: "
        f"{sorted(set(offenders))}. Add the predicate to the query -- the session "
        "will not add it for you -- or allowlist it above with a reason."
    )


# ==========================================================================
# Invariant 19: no secrets in logs or responses
# ==========================================================================
def test_no_secret_is_logged_or_formatted_directly() -> None:
    """A SecretStr must never be unwrapped into a log or an f-string."""
    pattern = re.compile(r"get_secret_value\(\)")
    offenders: list[str] = []
    # Only the places that must hand a raw credential to a provider client.
    allowed = {
        "titan/delivery/providers/resend.py",
        "titan/models/providers.py",  # build_providers hands keys to clients
        "titan/providers/browser_client.py",  # bearer token for the worker
        "titan/providers/places.py",  # from_settings() builds the Places client
        "titan/providers/smartlead.py",  # from_settings() builds the Smartlead client
        "titan/api/security.py",  # signs and verifies session tokens
        "titan/workers/outbox.py",
        # Hands the mailbox password to the IMAP client, exactly as the outbox
        # worker hands the SMTP password to its provider. Both build a client at
        # startup and neither logs the value.
        "titan/workers/inbound.py",
        "titan/cli.py",
    }
    for path in python_sources():
        rel = path.relative_to(API).as_posix()
        if rel in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        f"secret values unwrapped outside the provider layer at {offenders}"
    )


def test_redaction_covers_every_provider_key_shape() -> None:
    from titan.security.redaction import redact

    payload = {
        "api_key": "sk-live-abcdefghijklmnopqrstuvwxyz",
        "authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig",
        "nested": {"resend_api_key": "re_abcdefghijklmnopqrstuvwxyz"},
        "free_text": "the key is nvapi-abcdefghijklmnopqrstuvwxyz012345",
        "safe": "an ordinary value",
    }
    redacted = redact(payload)
    flattened = str(redacted)
    for secret in ("sk-live-abc", "re_abcdefgh", "nvapi-abcdefgh", "eyJhbGciOi"):
        assert secret not in flattened, f"{secret} survived redaction"
    assert redacted["safe"] == "an ordinary value"


# ==========================================================================
# Invariant 20: LeadPilot is not a runtime dependency
# ==========================================================================
def test_leadpilot_is_not_imported() -> None:
    """Invariant 20.

    Checks imports, not prose: titan/cli.py prints the invariant list, which
    legitimately names LeadPilot. What must not exist is a code dependency.
    """
    offenders: list[str] = []
    for path in python_sources():
        for module in imported_modules(parse(path)):
            if "leadpilot" in module.lower().replace("_", ""):
                offenders.append(f"{path.relative_to(API)} imports {module}")
    assert not offenders, offenders


# ==========================================================================
# Configuration discipline (gap analysis C-13)
# ==========================================================================
def test_configuration_is_read_only_through_settings() -> None:
    """No module may read configuration via os.getenv.

    The pre-0.2 code scattered os.getenv calls with silent "" defaults, which is
    how provider settings ended up declared in .env but invisible to the running
    service.
    """
    allowed = {"titan/config.py", "titan/runtime.py"}
    offenders: list[str] = []
    for path in python_sources():
        rel = path.relative_to(API).as_posix()
        if rel in allowed:
            continue
        for node in ast.walk(parse(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"getenv", "environ"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        f"os.getenv used outside titan.config at {offenders}; add the field to "
        "Settings instead so it is declared, validated, and documented"
    )


def test_no_naive_datetime_now_in_production_code() -> None:
    """Naive UTC timestamps corrupt event ordering (gap analysis M-04)."""
    offenders: list[str] = []
    for path in python_sources():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "datetime.utcnow()" in line:
                offenders.append(f"{path.relative_to(API)}:{lineno}")
    assert not offenders, f"deprecated naive datetime.utcnow() at {offenders}"


# ==========================================================================
# Test-suite integrity
# ==========================================================================
def test_no_test_asserts_a_bare_true() -> None:
    """Guard against tests that pass vacuously."""
    offenders: list[str] = []
    for path in (API / "tests").rglob("test_*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"\s*assert\s+True\s*$", line):
                offenders.append(f"{path.relative_to(API)}:{lineno}")
    assert not offenders, f"vacuous assertions at {offenders}"


def test_loopback_escape_hatch_is_off_by_default() -> None:
    """The fixture hatch must never be enabled in a shipped configuration."""
    guard = (BROWSER_WORKER / "src" / "urlGuard.ts").read_text(encoding="utf-8")
    assert "TITAN_UNSAFE_ALLOW_LOOPBACK === '1'" in guard, (
        "the loopback hatch must require an exact opt-in value"
    )
    # And it must not appear in any deployment configuration.
    for candidate in (
        REPO / "docker-compose.yml",
        REPO / "deploy" / "docker-compose.prod.yml",
    ):
        if not candidate.exists():
            continue
        assert "TITAN_UNSAFE_ALLOW_LOOPBACK" not in candidate.read_text(
            encoding="utf-8"
        ), f"{candidate.relative_to(REPO)} enables the loopback escape hatch"


#: The only module allowed to read the raw sendable-status set. Everywhere else
#: must go through ``verification_permits_sending``.
_SENDABILITY_RULE_OWNER = "titan/db/enums.py"


def test_the_sendability_rule_has_exactly_one_implementation() -> None:
    """Verification status alone no longer decides whether an address may be mailed.

    CATCH_ALL is sendable behind first-party provenance and refused behind a
    directory listing, so the test is ``verification_permits_sending(status,
    source)`` rather than membership of ``SENDABLE_VERIFICATION_STATUSES``. Two
    call sites -- the policy engine and the contact eligibility check -- used the
    frozenset directly, and a third reading it again would reintroduce the drift
    this consolidation removed: the UI would explain a block the send gate did
    not apply, or worse, the reverse.

    Reading the set for documentation or a test fixture is fine. Branching on
    membership is what this refuses.
    """
    membership = re.compile(r"\bin\s+SENDABLE_VERIFICATION_STATUSES\b")
    offenders: list[str] = []
    for path in python_sources():
        rel = path.relative_to(API).as_posix()
        if rel == _SENDABILITY_RULE_OWNER:
            continue
        if membership.search(path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert not offenders, (
        "these modules test membership of SENDABLE_VERIFICATION_STATUSES "
        f"directly: {offenders}. Call "
        "titan.db.enums.verification_permits_sending(status, source) instead, so "
        "the catch-all provenance rule cannot be skipped."
    )


#: Everything the campaign manager must not be able to touch. Not "must not
#: touch" -- must not be *able* to, which is a property of what it imports.
FORBIDDEN_TO_THE_MANAGER = (
    "titan.delivery.suppression",
    "titan.delivery.outbox_worker",
    "titan.delivery.providers.resend",
    "titan.delivery.providers.smartlead",
    "titan.delivery.providers.smtp",
    "titan.intelligence.composer",
    "titan.intelligence.message_validator",
)


def test_the_campaign_manager_cannot_reach_a_delivery_gate() -> None:
    """Bounded autonomy, as a property of the import graph.

    Written down, the boundary is a paragraph saying the manager may optimise
    but must not bypass suppression, approval, evidence or the delivery gates.
    Meant, it is a package that cannot import any of them -- so the refusal is
    not a check that could be forgotten but a capability that does not exist.

    The bounds in ``titan.autonomy.actuator`` stop it exceeding a human's
    numbers. This stops it reaching anything else at all.
    """
    offenders: list[str] = []
    for path in python_sources():
        rel = path.relative_to(API).as_posix()
        if not rel.startswith("titan/autonomy/"):
            continue
        modules = imported_modules(parse(path))
        for forbidden in FORBIDDEN_TO_THE_MANAGER:
            if forbidden in modules:
                offenders.append(f"{rel} imports {forbidden}")

    assert not offenders, (
        "the campaign manager reached past its actuator: "
        f"{offenders}. Everything it may change goes through "
        "titan.autonomy.actuator, and everything else is not its to change."
    )


def test_the_manager_writes_to_no_column_a_human_owns() -> None:
    """The other half of the boundary. The manager has its own columns, and the
    human's numbers are the bound it is clamped against -- writing to those
    directly would make next cycle's ceiling the manager's own last answer.
    """
    from titan.autonomy.apply import _COLUMN_FOR

    assert set(_COLUMN_FOR.values()) == {
        "managed_daily_send_limit",
        "managed_min_lead_score",
    }
    forbidden = {"daily_send_limit", "min_lead_score", "sending_authorized"}
    assert not (set(_COLUMN_FOR.values()) & forbidden)


@pytest.mark.parametrize(
    "module,symbol",
    [
        ("titan.policy.engine", "evaluate_send"),
        ("titan.delivery.quotas", "reserve_all"),
        ("titan.delivery.suppression", "is_suppressed"),
        ("titan.intelligence.message_validator", "validate_message"),
        ("titan.intelligence.bounce_risk", "assess"),
        ("titan.autonomy.actuator", "evaluate"),
        ("titan.autonomy.apply", "apply_all"),
        ("titan.db.enums", "verification_permits_sending"),
        ("titan.security.url_guard", "validate_url"),
    ],
)
def test_safety_critical_entry_points_exist(module: str, symbol: str) -> None:
    """A rename that orphans one of these would silently disable a control."""
    import importlib

    assert hasattr(importlib.import_module(module), symbol)


# ==========================================================================
# RBAC: every capability a route demands must actually be grantable
# ==========================================================================
def test_every_required_capability_exists_in_the_role_vocabulary() -> None:
    """A typo in ``require(...)`` denies everyone, silently.

    ``require("delivery:read")`` compiles, imports, and serves 403 to every
    caller including the owner -- the route looks implemented and is
    unreachable. This scan makes that a build failure instead.
    """
    from titan.db.enums import ROLE_CAPABILITIES

    grantable: set[str] = set()
    for capabilities in ROLE_CAPABILITIES.values():
        grantable |= set(capabilities)

    offenders: list[str] = []
    for path in python_sources():
        tree = parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "require"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value not in grantable:
                        offenders.append(
                            f"{path.relative_to(API).as_posix()}:{arg.lineno} "
                            f"requires {arg.value!r}"
                        )

    assert not offenders, (
        "these routes demand a capability no role can hold, so they are "
        f"unreachable: {offenders}"
    )


# ==========================================================================
# Deployment: a stack that looks healthy must actually do the work
# ==========================================================================
#: Every long-running process a complete Titan needs. The API and dashboard
#: alone produce a deployment that serves the CRM and researches nothing.
REQUIRED_WORKER_COMMANDS = {
    "titan.workers.outbox",
    "titan.workers.temporal_worker",
}


def test_the_production_stack_runs_every_worker() -> None:
    """The production compose shipped for months without a single worker.

    Nothing failed: the API was healthy, the dashboard rendered, and no lead
    was ever researched and no message ever sent. A missing worker is silent
    by construction, so it needs a static check rather than a smoke test.
    """
    compose = REPO / "deploy" / "docker-compose.prod.yml"
    if not compose.exists():
        pytest.skip("no production compose in this tree")
    text = compose.read_text(encoding="utf-8")

    missing = [c for c in REQUIRED_WORKER_COMMANDS if c not in text]
    assert not missing, (
        f"the production stack starts no process running: {sorted(missing)}. "
        "It would serve the CRM and do no work."
    )
    assert "browser-worker:" in text, (
        "the production stack has no browser worker, so research cannot capture evidence"
    )


def test_the_production_stack_does_not_ship_a_default_password() -> None:
    """A committed default is a published credential.

    Required secrets use ``${VAR:?...}`` so the stack refuses to start rather
    than coming up on a value that is in a public repository.
    """
    compose = REPO / "deploy" / "docker-compose.prod.yml"
    if not compose.exists():
        pytest.skip("no production compose in this tree")

    offenders = [
        line.strip()
        for line in compose.read_text(encoding="utf-8").splitlines()
        if re.search(r"(PASSWORD|SECRET|TOKEN|_KEY)\s*[:=].*\$\{[A-Z_]+:-", line)
    ]
    assert not offenders, (
        f"these give a secret a default value instead of failing closed: {offenders}"
    )


def test_the_browser_worker_gets_no_application_credentials() -> None:
    """Invariant 3, at the deployment layer.

    The worker is the only component that fetches attacker-controlled URLs.
    Handing it the application env_file would put database and provider
    credentials one browser escape away.
    """
    compose = REPO / "deploy" / "docker-compose.prod.yml"
    if not compose.exists():
        pytest.skip("no production compose in this tree")

    text = compose.read_text(encoding="utf-8")
    start = text.index("browser-worker:")
    # Up to the next top-level service definition.
    rest = text[start:]
    end = re.search(r"\n  [a-z][a-z-]*:\n", rest)
    block = rest[: end.start()] if end else rest

    assert "env_file" not in block, (
        "the browser worker is given the application env_file; it must hold no "
        "database, email, or model credential"
    )


def test_every_cron_workflow_is_actually_scheduled() -> None:
    """A workflow declaring a cron must appear in the installer's plan.

    This is the exact failure the scheduler was written to fix, and it is one
    that recurs silently. Four workflows were registered with the worker so they
    *could* run, each carrying a cron expression and a workflow-id convention,
    and nothing ever started one -- the loop was an arc for as long as it took
    somebody to notice a report had never arrived.

    Declaring DEFAULT_CRON is the statement "this runs on a timer". Nothing else
    in the repository makes that true, so the two are checked against each other.
    """
    import uuid as _uuid

    from titan.workflows import schedules

    sources = {
        path.stem: path.read_text(encoding="utf-8")
        for path in (TITAN / "workflows").glob("*.py")
    }
    declares_cron = {
        module
        for module, text in sources.items()
        if re.search(r"^DEFAULT_CRON\s*[:=]", text, re.M)
    }
    assert declares_cron, "no cron workflows found; this invariant stopped looking"

    scheduled = {
        module
        for job in schedules.plan_schedules(_uuid.uuid4(), task_queue="q")
        for module, text in sources.items()
        if f"class {job.workflow}" in text
    }

    missing = declares_cron - scheduled
    assert not missing, (
        f"these workflow modules declare a cron but nothing installs a schedule "
        f"for them: {sorted(missing)}. A cron nobody installs is a workflow that "
        f"never runs."
    )


# ==========================================================================
# Outcome states: a state nothing can write is a state nothing can measure
# ==========================================================================
#: Lead states that describe the end of a lead's journey, and which some code
#: path must therefore be able to reach.
#:
#: ``MEETING_BOOKED`` is the one that was missing, and it is the one that
#: mattered most: the terminal success of the whole system, with no writer
#: anywhere. Everything self-tuning therefore optimised on ``replied_at``
#: instead, under which "not interested" is a win.
_TERMINAL_LEAD_OUTCOMES = (
    "MEETING_BOOKED",
    "CONTACTED",
    "REPLIED",
    "SUPPRESSED",
)


@pytest.mark.parametrize("member", _TERMINAL_LEAD_OUTCOMES)
def test_every_terminal_lead_outcome_has_a_writer(member: str) -> None:
    """An outcome state nothing assigns cannot be reached, measured or improved.

    Scans for an assignment of the member -- ``status=LeadStatus.X`` or
    ``lead.status = LeadStatus.X`` -- rather than a mere mention, because being
    named in a query's exclusion list is not the same as something being able
    to put a lead into that state.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / "titan"
    pattern = re.compile(rf"(status\s*=\s*|status=)LeadStatus\.{member}\b")

    writers = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "migrations" not in path.parts
        and pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert writers, (
        f"LeadStatus.{member} is a terminal outcome that no code can assign. "
        "Nothing can reach it, so nothing can count it, so nothing downstream "
        "can optimise for it."
    )


def test_the_success_metric_is_reply_quality_not_reply_volume() -> None:
    """The comparison and the scaling rule must read positive replies.

    Reverting either to raw ``replied`` restores the defect this guards: the
    winning variant becomes whichever phrasing provokes the most answers of any
    kind, and the easiest way to provoke an answer is to annoy somebody.
    """
    from titan.autonomy.experiments import Arm, Verdict, compare
    from titan.autonomy.health import CampaignHealth, CampaignWindow, classify
    from titan.db.enums import CampaignStatus
    from titan.delivery.deliverability import ReputationWindow

    provocative = Arm("loud", sent=800, replied=200, positive_replies=10)
    measured = Arm("measured", sent=800, replied=60, positive_replies=55)
    assert compare(measured, provocative).verdict is Verdict.CONTROL_WINS

    rejected = CampaignWindow(
        campaign_id="c",
        status=CampaignStatus.ACTIVE,
        window=ReputationWindow(sent=400, delivered=396, hard_bounced=2, complained=0),
        contacted=300,
        replied=90,
        positive_replies=1,
        configured_limit=40,
        effective_limit=20,
        leads_available=50,
    )
    assert classify(rejected) is not CampaignHealth.SCALING


def test_no_generic_offer_is_substituted_when_evidence_matches_nothing() -> None:
    """A pitch must follow from the evidence beside it.

    The fallback this guards read "enquiry capture and follow-up automation" and
    was substituted whenever a lead's evidence matched no offer in its
    industry's playbook -- so the message cited a broken booking button and then
    claimed a capability with no relationship to it. That is a performance claim
    the system invented about itself.

    Scans for the string as a *value* rather than anywhere at all, so the
    comments explaining the removal do not re-trip it.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / "titan"
    offender = re.compile(
        r"^(?!\s*#).*[=(]\s*[\"']enquiry capture and follow-up automation[\"']",
        re.MULTILINE,
    )

    guilty = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if offender.search(path.read_text(encoding="utf-8"))
    ]

    assert not guilty, (
        f"a generic offer is being substituted for real evidence in: {guilty}. "
        "select_offers returning empty means there is nothing truthful to "
        "offer; the caller must refuse rather than invent one."
    )
