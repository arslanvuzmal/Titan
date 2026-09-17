"""Titan-OS runtime configuration.

Design rules (see docs/audits/PRODUCTION-GAP-ANALYSIS.md, finding C-13):

1. Every environment variable the runtime reads is declared here. Nothing else in
   the codebase may call ``os.getenv`` for configuration -- an invariant test
   enforces this.
2. Security-relevant values have **no silent defaults**. A missing value produces
   a startup failure with an explicit message, never a degraded-but-running
   service.
3. Outbound sending is disabled by default at every level. Enabling it requires
   deliberate, independent configuration in the environment *and* the workspace
   *and* the campaign *and* a verified sender identity (see titan.policy).
"""

from __future__ import annotations

import enum
import functools
from typing import Annotated, Any, Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(enum.StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class OperatingMode(enum.StrEnum):
    """Global ceiling on what Titan is permitted to do.

    A workspace or campaign may be *more* restrictive than the global mode but
    never less. See docs/PRODUCT-SPEC.md section 3 and titan.policy.modes.
    """

    RESEARCH_ONLY = "research_only"
    DRAFT_ONLY = "draft_only"
    APPROVAL_REQUIRED = "approval_required"
    CONTROLLED_AUTOPILOT = "controlled_autopilot"


#: Ordering used for "is mode A at least as permissive as mode B" comparisons.
MODE_RANK: dict[OperatingMode, int] = {
    OperatingMode.RESEARCH_ONLY: 0,
    OperatingMode.DRAFT_ONLY: 1,
    OperatingMode.APPROVAL_REQUIRED: 2,
    OperatingMode.CONTROLLED_AUTOPILOT: 3,
}

Port = Annotated[int, Field(ge=1, le=65535)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TITAN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ---------------------------------------------------------------- runtime
    environment: Environment = Environment.LOCAL
    service_name: str = "titan-api"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    frontend_url: AnyHttpUrl = AnyHttpUrl("http://localhost:3000")
    #: Additional exact origins allowed to call the API with credentials.
    #: Comma-separated. Use for a custom domain alongside the canonical one.
    extra_cors_origins: list[str] = Field(default_factory=list)
    #: Vercel gives every preview deployment a unique hostname, so a preview
    #: cannot be listed in advance. Setting this to the project's Vercel scope
    #: (e.g. "titan-os-arslanvuzmal") allows
    #: https://<anything>-<scope>.vercel.app -- and nothing else. Left unset,
    #: no preview origin is allowed, which is the safe default: an attacker who
    #: can deploy to *.vercel.app must not inherit a credentialled origin.
    vercel_preview_scope: str | None = None

    # --------------------------------------------------------------- database
    database_url: str = (
        "postgresql+psycopg://titan:titan_dev_password@localhost:5432/titan"
    )
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=5, ge=0, le=100)
    database_statement_timeout_ms: int = Field(default=15_000, ge=100)

    # --------------------------------------------------------------- temporal
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_tls: bool = False

    # ------------------------------------------------------- browser evidence
    #: URL of the isolated browser worker. This service holds NO credentials for
    #: email, models, or the database (threat model: browser escape).
    browser_worker_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8800")
    browser_worker_token: SecretStr | None = None
    #: How many crawls the browser worker will serve at once. Must match the
    #: worker's own ``BROWSER_WORKER_CONCURRENCY``: this side uses it to stop
    #: sending more crawls than there are lanes to run them, and a number
    #: larger than the worker's means asking for 503s on purpose.
    browser_worker_concurrency: int = Field(default=4, ge=1, le=64)
    #: Raised from 12 alongside the contact-page paths in
    #: :data:`titan.intelligence.playbooks.CONTACT_PATHS`, and the two numbers
    #: have to move together. A playbook now asks for up to eighteen paths --
    #: its own findings pages plus the pages that carry an address -- and the
    #: crawler stops at this ceiling however many were requested. Adding the
    #: paths without raising the budget would not have improved contact yield;
    #: it would only have traded the evidence pages away for it, which is the
    #: worse half of the trade for a system that cannot write without evidence.
    crawl_max_pages: int = Field(default=18, ge=1, le=200)
    crawl_max_depth: int = Field(default=2, ge=0, le=5)
    #: Moved with the page budget above so the per-page allowance stays sane:
    #: eighteen pages inside the old 120s left about six seconds each, and a
    #: slow site would have hit the wall mid-crawl and returned partial evidence.
    crawl_timeout_seconds: int = Field(default=180, ge=5, le=900)
    crawl_max_response_bytes: int = Field(default=5_000_000, ge=10_000)
    crawl_max_redirects: int = Field(default=5, ge=0, le=20)
    #: Sent to every site Titan crawls, and the one place the system names
    #: itself to a stranger. The URL has to resolve to something a person can
    #: read to decide whether to allow the crawler -- a domain the owner does
    #: not use makes the identification worthless and the crawl anonymous in
    #: practice.
    crawl_user_agent: str = (
        "TitanOS-Research/0.2 (+https://arslanvuzmallone.com/bot; evidence-only)"
    )
    crawl_respect_robots: bool = True

    # ------------------------------------------------------------ ai models
    model_gateway_timeout_seconds: int = Field(default=60, ge=5, le=600)
    nvidia_api_key: SecretStr | None = None
    nvidia_base_url: AnyHttpUrl = AnyHttpUrl("https://integrate.api.nvidia.com/v1")
    gemini_api_key: SecretStr | None = None
    gemini_base_url: AnyHttpUrl = AnyHttpUrl(
        "https://generativelanguage.googleapis.com/v1beta"
    )
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: AnyHttpUrl = AnyHttpUrl("https://openrouter.ai/api/v1")
    cloudflare_account_id: str | None = None
    cloudflare_gateway_id: str | None = None
    cloudflare_api_token: SecretStr | None = None

    #: Model IDs are configuration, never hardcoded assumptions. They are
    #: checked by `titan validate-models`, which *calls* each route rather than
    #: looking it up -- see :meth:`ModelGateway.validate_models` for why the
    #: cheaper catalogue check was not enough.
    #:
    #: **Every route below is a model that expires.** On 2026-08-26 NVIDIA
    #: retired ``meta/llama-3.1-8b-instruct`` and
    #: ``nvidia/llama-3.3-nemotron-super-49b-v1`` on the same day; OpenRouter
    #: had meanwhile run ``anthropic/claude-sonnet-4`` down to zero credits and
    #: moved ``minimax/minimax-m3:free`` behind payment, and Gemini was
    #: answering 503. All five routes were dead at once and nothing said so for
    #: fifteen days, because every call site degrades quietly by design. The
    #: routes here are the ones that answered a real JSON-schema call on
    #: 2026-09-10; treat them as perishable and let the scheduled validator be
    #: what notices, not a person.
    model_route_extraction: str = "nvidia:nvidia/nemotron-3-super-120b-a12b"
    model_route_research: str = "nvidia:nvidia/nemotron-3-super-120b-a12b"
    model_route_verification: str = "nvidia:nvidia/nemotron-3-super-120b-a12b"
    #: The model writes the prose. It does not decide what is true: the rewriter
    #: hands it one sentence and the claim that sentence must preserve, and
    #: discards anything that comes back having lost the evidenced specific
    #: (titan.intelligence.rewriter).
    #:
    #: Deliberately on a *different provider* from the three above. The message
    #: path is the one whose absence is invisible -- a failed rewrite still
    #: sends, just in the deterministic words -- so it gets the route least
    #: likely to die in the same instant as the rest, and the gateway's
    #: cross-provider fallback then covers each direction with the other.
    model_route_message: str = "openrouter:nvidia/nemotron-3-super-120b-a12b:free"
    #: The expensive tier -- and, honestly, there is not one at the moment.
    #:
    #: This route was ``openrouter:anthropic/claude-sonnet-4`` against an
    #: account at zero credits, so every premium call returned 402 and no
    #: caller noticed. Nothing in the pipeline asks for PREMIUM today, but the
    #: route still has to answer: it is what ``_is_premium_route`` compares
    #: against, so it must stay *distinct* from the routes above or every
    #: ordinary fallback onto them would be metered against the premium share
    #: and eventually refused.
    #:
    #: On Cloudflare, which is the third provider and until now an unused one:
    #: the account had a healthy gateway with 2,784 models reachable through it
    #: and not one route pointed at it. That matters beyond this line -- the
    #: gateway falls back across *providers*, so with everything on NVIDIA and
    #: OpenRouter a bad afternoon at either took most of the estate with it.
    #:
    #: Chosen by measurement against a 60s cap, JSON schema included:
    #:
    #: =========================================  ====================
    #: ``cloudflare:...llama-3.3-70b-fp8-fast``   1.3-2.2s, 2 of 2
    #: ``nvidia:openai/gpt-oss-20b``              7-39s, then two
    #:                                            consecutive timeouts
    #: ``nvidia:...nemotron-3-ultra-550b``        503 overloaded
    #: ``openrouter:...ultra-550b:free``          33-50s, 2 of 3
    #: ``nvidia:deepseek-v4-pro``                 122-155s
    #: =========================================  ====================
    #:
    #: A route an hourly alarm depends on has to be boring, and a 70B model
    #: answering in two seconds is the only candidate here that is.
    model_route_premium: str = (
        "cloudflare:workers-ai/@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    )

    # --------------------------------------------------------------- budgets
    budget_workspace_daily_usd: float = Field(default=25.0, ge=0)
    budget_campaign_daily_usd: float = Field(default=10.0, ge=0)
    budget_lead_usd: float = Field(default=0.50, ge=0)
    budget_premium_share_max: float = Field(default=0.15, ge=0, le=1)
    budget_hard_stop: bool = True

    #: Let a model rephrase the sentences the deterministic composer produced.
    #:
    #: Off by default, and the default is the safe direction rather than a
    #: placeholder: with it off Titan sends the text it has always sent, which
    #: passes every rule. Turning it on adds phrasing, never facts -- the claim
    #: map is preserved sentence by sentence and a rewrite that drops an
    #: evidenced specific is discarded (titan.intelligence.rewriter).
    #:
    #: Requires a model provider key. Without one the gateway raises, the
    #: rewrite is abandoned, and the deterministic text is used -- so enabling
    #: this on an unconfigured deployment costs a failed call per draft rather
    #: than a failed draft.
    model_rewrites_enabled: bool = False

    # ------------------------------------------------------------- discovery
    google_places_api_key: SecretStr | None = None
    google_places_base_url: AnyHttpUrl = AnyHttpUrl("https://places.googleapis.com/v1")
    agent_reach_api_key: SecretStr | None = None
    agent_reach_base_url: AnyHttpUrl | None = None

    # --------------------------------------------------- contact verification
    #: Which mailbox verification service the bounce reduction engine may ask.
    #:
    #: "null" is the honest default: it answers UNKNOWN for every address, which
    #: cannot mark one sendable and cannot condemn one either. Titan does not run
    #: its own SMTP probe -- titan.intelligence.mx explains why -- so a
    #: mailbox-level answer requires buying one, and that is a purchasing
    #: decision rather than a default.
    #:
    #: "deterministic" is a seeded fake for tests and local development. The
    #: validator below refuses it in a deployed environment, because an answer
    #: derived from a hash of the address is indistinguishable from a real one
    #: once it is stored on the contact.
    mailbox_verifier: Literal["null", "deterministic", "instantly", "smtp_probe"] = "null"
    #: What the probe introduces itself as, and who a complaint goes to.
    #:
    #: Both are required for TITAN_MAILBOX_VERIFIER=smtp_probe and neither
    #: has a default, deliberately. A probe that invents a HELO name or
    #: uses a null sender is the behaviour that gets a host blocklisted,
    #: and a plausible default would make that the easy path. The hostname
    #: should resolve, and the address should be a mailbox somebody reads.
    smtp_probe_helo: str | None = None
    smtp_probe_mail_from: str | None = None
    smtp_probe_timeout_seconds: int = Field(default=12, ge=3, le=60)
    #: Domains probed at once. Low on purpose: this is verification during
    #: discovery, not a race, and every unit of parallelism here is another
    #: connection somebody else's mail server has to account for.
    smtp_probe_concurrency: int = Field(default=4, ge=1, le=32)

    # ------------------------------------------------------------------ smtp
    #: Used both for a real mailbox and for Mailpit, the local capture server
    #: that accepts everything and delivers nothing. See
    #: titan.delivery.providers.smtp for what SMTP cannot guarantee.
    smtp_host: str | None = None
    smtp_port: Port = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    #: "ssl" (implicit TLS, port 465), "starttls" (port 587), or "none".
    #: "none" is refused unless the host is loopback -- see the validator.
    smtp_security: Literal["ssl", "starttls", "none"] = "ssl"
    smtp_timeout_seconds: int = Field(default=30, ge=5, le=300)

    # -------------------------------------------------------------- operator
    #: Chat webhook (Slack/Discord-compatible) for operator alerts. Optional by
    #: design: every notification is written to the tasks table regardless, and
    #: this only decides whether one also arrives somewhere noisier. A missing
    #: value degrades the channel, never the record.
    #:
    #: Only the headline is pushed. The body of a reply stays in the CRM rather
    #: than being copied into a third-party chat system the prospect was never
    #: told about.
    operator_webhook_url: AnyHttpUrl | None = None

    # ------------------------------------------------------------------ imap
    #: The mailbox replies arrive in, read by titan.workers.inbound. Separate
    #: from the SMTP block on purpose: sending and receiving are frequently
    #: different hosts, and a deployment may legitimately do one without the
    #: other. No "none" security option, unlike SMTP -- there is no local-capture
    #: equivalent of Mailpit for *reading* mail, so plaintext here would only
    #: ever put a real mailbox password and the full text of every reply on the
    #: wire in clear.
    imap_host: str | None = None
    imap_port: Port = 993
    imap_username: str | None = None
    imap_password: SecretStr | None = None
    imap_security: Literal["ssl", "starttls"] = "ssl"
    imap_folder: str = "INBOX"
    imap_poll_seconds: int = Field(default=60, ge=15, le=3600)
    imap_batch_size: int = Field(default=50, ge=1, le=500)
    #: Where to record a reply that matches no message Titan sent -- somebody
    #: writing in cold, or answering from an address we never wrote to. Every
    #: row in the schema is workspace-scoped, so without this such a message
    #: cannot be stored at all and is left in the mailbox unread.
    imap_workspace_id: str | None = None

    # ---------------------------------------------------------------- email
    email_provider: Literal[
        "mock", "resend", "smartlead", "smtp", "smtp_pool", "instantly"
    ] = "mock"
    #: Where the per-mailbox credentials live, for "smtp_pool". A file
    #: rather than more TITAN_SMTP_* variables: the pool holds one
    #: credential per sending address, and a password in the environment is
    #: a password in `docker compose config`, in shell history and in every
    #: screenshot of a terminal. See titan.delivery.mailboxes.
    mailbox_file: str | None = None
    resend_api_key: SecretStr | None = None
    resend_webhook_secret: SecretStr | None = None

    # ------------------------------------------------------------- smartlead
    #: Smartlead is a campaign platform, not a transactional ESP: it exposes no
    #: "send this message now" endpoint. Titan therefore hands each *already
    #: authorized* message to a dedicated single-step campaign, so every gate
    #: still runs here before Smartlead is involved at all. See
    #: titan.delivery.providers.smartlead for what that costs and guarantees.
    smartlead_api_key: SecretStr | None = None
    #: Instantly API v2. A second carrier behind the same EmailProvider port;
    #: see titan.delivery.providers.instantly for what is and is not proven.
    instantly_api_key: SecretStr | None = None
    instantly_campaign_id: str | None = None
    instantly_webhook_secret: SecretStr | None = None
    smartlead_base_url: AnyHttpUrl = AnyHttpUrl("https://server.smartlead.ai/api/v1")
    #: The campaign Titan delivers through. Its sequence must be a single step
    #: whose subject and body are the {{titan_subject}} / {{titan_body}}
    #: variables, so the text Titan validated is the text that is sent.
    smartlead_campaign_id: int | None = None
    smartlead_timeout_seconds: int = Field(default=30, ge=5, le=300)

    # The five below were recovered from the deployed service's environment,
    # which sets them against a build that was never pushed to this repository.
    # `extra="ignore"` meant this Settings model read straight past them, so a
    # deploy of this tree would silently run with Smartlead disabled and every
    # message going to the production campaign rather than the sandbox --
    # failing open, quietly, in the one direction that matters.
    #
    # Defaults are the safe end of each, not the deployed value: recovering the
    # *name* of a switch is not a reason to arrive with it flipped on.

    #: Sandbox campaign. Messages land in Smartlead without going to a stranger,
    #: which is the only way to exercise the delivery path end to end.
    smartlead_sandbox_campaign_id: int | None = None
    #: Route to the production campaign rather than the sandbox. Off by default.
    smartlead_production_enabled: bool = False
    #: Allow importing leads into Smartlead. Off by default: an import writes
    #: recipient data to a third party and is not undone by disabling it later.
    smartlead_import_enabled: bool = False
    #: Addresses that may receive sandbox sends. Anything not listed here is
    #: refused while production routing is off, so a misconfigured test cannot
    #: reach a real prospect.
    #:
    #: Accepts a comma-separated string, which is the form the deployed service
    #: actually sets.
    #:
    #: `NoDecode` is load-bearing, not decoration. pydantic-settings decodes a
    #: complex field from the environment by calling json.loads *inside the env
    #: source*, before any field validator runs -- so a plain
    #: `tuple[str, ...]` raises SettingsError on boot against the deployed
    #: value and no validator can rescue it. NoDecode hands the raw string to
    #: the validator below instead. Found by loading Settings against the real
    #: recovered environment; reading the code would not have shown it.
    smartlead_test_recipients: Annotated[tuple[str, ...], NoDecode] = ()
    #: HMAC secret for verifying Smartlead webhook callbacks. Without it the
    #: webhook route must fail closed -- an unverified callback can mark a
    #: message replied and stop a sequence.
    smartlead_webhook_secret: SecretStr | None = None

    #: THE GLOBAL KILL SWITCH (invariant 8, 21).
    #: False means: no outbox row may ever be handed to a real provider,
    #: regardless of workspace, campaign, or user configuration.
    production_sending_enabled: bool = False

    #: Operator acknowledgement that SPF/DKIM/DMARC are configured for the
    #: sending domain. Checked at send time; cannot be set by an API request.
    email_auth_preflight_acknowledged: bool = False

    #: Which host this is, for the sending claim. Must be stable across
    #: container recreation, so not the container's hostname -- an identity
    #: that changed on restart would make the real sender refuse its own claim
    #: and stop sending entirely. Set it in .env, once, per host.
    #:
    #: Empty means this host cannot prove which one it is, and it will not
    #: send. See titan/delivery/sending_claim.py for why that fails closed.
    sender_host_id: str = ""
    sender_host_label: str = ""

    outbox_lease_seconds: int = Field(default=60, ge=5, le=600)
    outbox_batch_size: int = Field(default=10, ge=1, le=200)
    outbox_max_attempts: int = Field(default=6, ge=1, le=50)
    outbox_poll_interval_seconds: float = Field(default=2.0, ge=0.1, le=60)

    # ---------------------------------------------------------------- quotas
    quota_workspace_daily: int = Field(default=50, ge=0)
    quota_campaign_daily: int = Field(default=25, ge=0)
    quota_sender_daily: int = Field(default=50, ge=0)
    quota_recipient_domain_daily: int = Field(default=2, ge=0)
    quota_min_spacing_seconds: int = Field(default=90, ge=0)
    quiet_hours_enabled: bool = True
    quiet_hours_start: int = Field(default=20, ge=0, le=23)
    quiet_hours_end: int = Field(default=8, ge=0, le=23)

    # ------------------------------------------------------------- identity
    auth_mode: Literal["clerk", "local"] = "local"
    clerk_issuer_url: AnyHttpUrl | None = None
    local_jwt_secret: SecretStr | None = None
    session_ttl_seconds: int = Field(default=3600, ge=60, le=86_400)

    #: Consecutive failures before the account is locked. This is what makes a
    #: short passcode defensible: six digits is a million guesses offline, but
    #: five online attempts before a fifteen-minute wall is roughly a century.
    login_max_attempts: int = Field(default=5, ge=1, le=100)
    login_lockout_seconds: int = Field(default=900, ge=0, le=86_400)
    #: Enforced by `titan set-passcode`, not at login -- a floor raised later
    #: must not lock out an operator whose existing passcode is still valid.
    min_passcode_length: int = Field(default=6, ge=6, le=128)

    # ---------------------------------------------------------------- owner
    #: The operator's own mailbox, so inbound can tell us from a prospect.
    #:
    #: Not a sender identity and that is the point: the damage was done by
    #: delivery tests sent to a personal Gmail, which threaded back and retired
    #: a real lead permanently. The pool's own addresses are read from the
    #: database; this is the human behind it, and nothing else knows it.
    operator_email: str | None = None

    #: Whether a measured absence may be written about, or only counted.
    #:
    #: False while the population is being measured. Absence findings are
    #: detected, stored and scored either way -- what this decides is whether
    #: the composer may build a sentence out of one, which is the moment it
    #: starts changing what real businesses receive.
    #:
    #: The question it exists to answer first: how many leads does selling the
    #: absence actually make sendable? That number is knowable before the
    #: message changes, and it should be known.
    absence_pitching_enabled: bool = False

    #: The external watchdog's ping URL, from healthchecks.io or equivalent.
    #:
    #: No process can report its own absence, and the absence is the failure
    #: this estate actually suffers -- five Docker outages and five stalled
    #: schedules so far, each found days later by a person going looking. The
    #: daily report pings this after a mail genuinely goes out; if the ping
    #: stops arriving, the watchdog raises the alarm from outside.
    #:
    #: Unset means the ping is skipped and everything else still works, so the
    #: report can ship before the URL exists.
    healthcheck_ping_url: str | None = None

    #: Which mailbox the operator's own report is sent from.
    #:
    #: Named rather than pooled: this is 1:1 mail to a person who knows the
    #: sender, not outreach, and it must not be routed by the pool's health
    #: logic -- a report explaining that every mailbox is blocked cannot be
    #: held back by every mailbox being blocked.
    report_from_email: str | None = None

    owner_name: str = "Arslan Vuzmal Lone"
    #: Used in message signatures and the portfolio claims a draft may make.
    #: Recovered from the deployed environment along with the smartlead block.
    owner_title: str = "AI & Full-Stack Systems Engineer"
    #: Appears in outbound copy, so it is a factual claim about the sender and
    #: is deliberately configuration rather than a hardcoded string that goes
    #: stale without anyone noticing.
    owner_years_experience: int = Field(default=2, ge=0, le=80)
    owner_portfolio_url: AnyHttpUrl = AnyHttpUrl("https://arslanvuzmallone.com")
    # ---- contact discovery of last resort --------------------------------
    #
    # The provider credentials for this already exist above -- openrouter_api_key,
    # cloudflare_api_token and the gateway id feed titan.models.providers, which
    # is the one place a model client is constructed. Nothing new is declared
    # here; what is new is the ceiling, because this lane runs per lead rather
    # than per draft and both accounts are on free tiers with daily caps.
    #
    # The lane is used only where the crawler recorded no address at all: on
    # the live workspace that is 1,120 organisations whose pages carry a
    # contact form and nothing else. It is never used to improve on an address
    # that was found -- text the crawler read verbatim is better evidence than
    # anything a model can say about it.
    #
    # Invariant 6 is unchanged and unchangeable here. A model may only point at
    # text already present in the page evidence; anything it returns that does
    # not appear verbatim in the crawled text is discarded, because a model
    # asked for an email address will produce a well-formed one whether or not
    # the page contained one. That check is the whole safety argument for
    # letting a model near this at all.
    #
    #: Hard ceiling on contact-extraction model calls per day, across providers.
    contact_model_calls_per_day: int = 200

    #: The route the contact lane will use.
    #:
    #: Free tiers move, and this line is the proof: every model it has ever
    #: named has since stopped being free. ``meta-llama/llama-3.3-70b-instruct:free``
    #: began answering "This model is unavailable for free";
    #: ``google/gemma-4-31b-it:free`` answers 429 under load; and
    #: ``minimax/minimax-m3:free``, verified live on 2026-08-27 and left
    #: unchecked, was answering the same "unavailable for free" 404 by
    #: 2026-09-10. The lesson is not a better guess at a durable free model --
    #: there is no such thing -- it is that ``titan validate-models`` has to run
    #: on a schedule, which it now does (``housekeeping``).
    model_route_contact: str = "openrouter:nvidia/nemotron-3-super-120b-a12b:free"

    #: A one-page PDF attached to every message.
    #:
    #: Unset attaches nothing, which is the shipped state. An attachment is a
    #: deliberate act: an unsolicited one from an unknown sender is a strong
    #: spam signal and some corporate gateways strip document types by policy,
    #: so this is set knowingly or not at all. ``delivery/deliverability.py``
    #: bounds what may go out -- one file, PDF, under 400 KB, never executable
    #: content whatever it is named.
    one_pager_attachment_path: str | None = None

    #: What share of messages carry the one-page brief, 0-100.
    #:
    #: Zero by default, which is the whole point: an attachment is a new signal
    #: to receivers, and this went in while two of three mailboxes were
    #: recovering from bounces. A trial that silently became a launch because
    #: nobody set a number is the failure this default prevents.
    #:
    #: The sample is taken on the outbox row's id, so a message is always in or
    #: always out -- a retry cannot flip it, and raising the percentage later
    #: only adds messages to the treated set rather than reshuffling it.
    one_pager_sample_percent: int = 0

    #: A one-page summary of the approach, linked from the references block.
    #:
    #: A link rather than an attachment, deliberately. An unsolicited PDF from
    #: an unknown sender is a strong spam signal and corporate gateways strip
    #: them, so a share of recipients would be told to read something that had
    #: been removed in transit. A hosted page also produces a click, which is
    #: the engagement signal this system is otherwise missing entirely.
    #:
    #: Unset renders no line at all.
    one_pager_url: str | None = None

    #: JSON file of real previous projects, cited in the credibility paragraph.
    #:
    #: Unset by default and unset is a working state: with no file the message
    #: keeps the generic credential sentence. There is deliberately no bundled
    #: default -- see ``titan.intelligence.case_studies`` for why an invented
    #: case study is the one thing in this system that cannot be walked back.
    case_studies_path: str | None = None

    #: Shared with the unsubscribe endpoint on the portfolio, which verifies it.
    #:
    #: Without it Titan cannot sign an opt-out link, and an unsigned link would
    #: let anybody unsubscribe anybody by editing the address in the URL. Absent
    #: rather than defaulted: a placeholder secret produces links that every
    #: recipient finds broken at the moment they have decided to leave, and the
    #: only symptom is a 403 nobody sees.
    unsubscribe_secret: SecretStr | None = None
    owner_portfolio_fallback_url: AnyHttpUrl = AnyHttpUrl(
        "https://arslanvuzmallone.vercel.app"
    )
    #: Required in every commercial email footer (CAN-SPAM 5(a)(5)). No default:
    #: an unset value blocks sending rather than emitting a placeholder address.
    sender_mailing_address: str | None = None

    # -------------------------------------------------------- observability
    otel_exporter_otlp_endpoint: AnyHttpUrl | None = None
    otel_traces_enabled: bool = False
    metrics_enabled: bool = True
    metrics_port: Port = 9464

    # ---------------------------------------------------------------- misc
    demo_mode: bool = False
    rate_limit_redis_url: str | None = None

    # ------------------------------------------------------------ validators
    @field_validator("smartlead_test_recipients", mode="before")
    @classmethod
    def _comma_separated_recipients(cls, value: Any) -> Any:
        """Accept `a@x.com,b@y.com` as well as a JSON array.

        One environment variable cannot hold a list, so every deployment
        encodes one somehow. pydantic-settings assumes JSON; the deployed
        service uses commas. Supporting both means the recovered environment
        loads, and a JSON array still works for anyone who writes one.
        """
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if stripped.startswith("["):
            # Parsed here rather than deferred to pydantic: pydantic-settings
            # decodes JSON for values arriving from the environment but not for
            # ones passed directly, and the field should not behave differently
            # depending on which of those a caller used.
            import json

            try:
                return tuple(json.loads(stripped))
            except (ValueError, TypeError):
                return value  # let the field's own validation report it
        return tuple(part.strip() for part in stripped.split(",") if part.strip())

    @field_validator(
        "clerk_issuer_url",
        "agent_reach_base_url",
        "otel_exporter_otlp_endpoint",
        "browser_worker_token",
        "resend_api_key",
        "resend_webhook_secret",
        "smartlead_api_key",
        "smartlead_webhook_secret",
        "smtp_host",
        "smtp_username",
        "smtp_password",
        "imap_host",
        "imap_username",
        "imap_password",
        "imap_workspace_id",
        "operator_webhook_url",
        "nvidia_api_key",
        "gemini_api_key",
        "openrouter_api_key",
        "cloudflare_api_token",
        "cloudflare_account_id",
        "cloudflare_gateway_id",
        "google_places_api_key",
        "agent_reach_api_key",
        "local_jwt_secret",
        "sender_mailing_address",
        mode="before",
    )
    @classmethod
    def _empty_string_is_unset(cls, value: object) -> object:
        """Treat an empty environment variable as absent.

        Docker Compose renders an unset ``${VAR:-}`` as the empty string, not as
        a missing key. Without this, every optional URL and secret fails
        validation and the container will not start -- which is exactly how the
        first real `docker compose run migrate` failed.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        if not v.startswith("postgresql+psycopg://"):
            raise ValueError(
                "database_url must use the postgresql+psycopg:// driver "
                "(SQLAlchemy 2.x async). Got: " + v.split("://", 1)[0] + "://..."
            )
        return v

    @field_validator("crawl_user_agent")
    @classmethod
    def _require_identifiable_agent(cls, v: str) -> str:
        if "http" not in v:
            raise ValueError(
                "crawl_user_agent must contain a contact URL so site owners can "
                "identify and block the crawler"
            )
        return v

    @model_validator(mode="after")
    def _unencrypted_smtp_only_to_loopback(self) -> Settings:
        """Cleartext SMTP is permitted only to a local capture server.

        Mailpit speaks plain SMTP on loopback and that is fine -- nothing
        leaves the machine. The same setting pointed at a real mail host would
        put the mailbox password and every recipient address on the wire in
        clear, so it is refused rather than warned about.
        """
        if self.smtp_security != "none" or self.smtp_host is None:
            return self
        host = self.smtp_host.strip().lower()
        if host in {"localhost", "127.0.0.1", "::1", "mailpit", "titan-mailpit"}:
            return self
        raise ValueError(
            f"TITAN_SMTP_SECURITY='none' is only allowed for a loopback capture "
            f"server; host is {self.smtp_host!r}. Use 'ssl' (port 465) or "
            "'starttls' (port 587) for a real mail server."
        )

    @model_validator(mode="after")
    def _fail_closed_when_deployed(self) -> Settings:
        """A deployed environment may not run with placeholder or missing config.

        Staging is included: it is not production, but it is on a network with
        real data behind it, so it may no more run without a configured identity
        provider than production may.
        """
        if not self.is_deployed:
            return self

        missing: list[str] = []
        if self.auth_mode == "clerk" and self.clerk_issuer_url is None:
            missing.append("TITAN_CLERK_ISSUER_URL")
        if self.auth_mode == "local" and self.local_jwt_secret is None:
            missing.append("TITAN_LOCAL_JWT_SECRET")
        if self.demo_mode and self.is_production:
            missing.append("TITAN_DEMO_MODE must be false in production")
        if self.mailbox_verifier == "deterministic":
            # The fake derives its verdict from a hash of the address. Stored on
            # a contact channel it is indistinguishable from a purchased answer,
            # and it can reach PROVIDER_VERIFIED -- which sends. A fabricated
            # verification is worse than none at all, so it is refused here
            # rather than left to a deployment note nobody reads.
            missing.append(
                "TITAN_MAILBOX_VERIFIER='deterministic' is a test fake and must "
                "not run in a deployed environment; use 'null'"
            )
        if self.mailbox_verifier == "smtp_probe":
            # Both, or neither. A probe that cannot introduce itself with a
            # real hostname and cannot be complained to at a real address is
            # indistinguishable from the abusive kind, and the cost of that
            # falls on the mail servers being asked -- not on Titan, which is
            # why it must not be possible to arrive at by omission.
            if not (self.smtp_probe_helo or "").strip():
                missing.append("TITAN_SMTP_PROBE_HELO (a hostname that resolves to us)")
            if "@" not in (self.smtp_probe_mail_from or ""):
                missing.append("TITAN_SMTP_PROBE_MAIL_FROM (a mailbox somebody reads)")
        if missing:
            raise ValueError(
                f"Refusing to start in {self.environment.value} with incomplete "
                "configuration: " + ", ".join(missing)
            )
        return self

    # ------------------------------------------------------------- helpers
    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def is_deployed(self) -> bool:
        """Whether this process is reachable by anyone other than its developer.

        Controls that exist to keep a stranger out -- the passwordless local
        login, the published OpenAPI schema -- must key off this rather than off
        :attr:`is_production`. Keying them off production alone left a staging
        host as an unguarded way in, holding the same data.
        """
        return self.environment in (Environment.STAGING, Environment.PRODUCTION)

    def sending_preflight_errors(self) -> list[str]:
        """Reasons the *process* is not allowed to deliver mail.

        This is the outermost of four independent gates. Workspace, campaign,
        sender-identity, and per-message gates are evaluated separately by
        :mod:`titan.policy.engine`. All four must pass.
        """
        errors: list[str] = []
        if not self.production_sending_enabled:
            errors.append(
                "TITAN_PRODUCTION_SENDING_ENABLED is false (global kill switch)"
            )
        if self.email_provider == "mock":
            errors.append("TITAN_EMAIL_PROVIDER is 'mock'; no real provider configured")
        if self.email_provider == "resend" and self.resend_api_key is None:
            errors.append("TITAN_RESEND_API_KEY is not set")
        if self.email_provider == "smtp" and self.smtp_host is None:
            errors.append("TITAN_SMTP_HOST is not set")
        if self.email_provider == "smtp_pool":
            # Read here, not merely checked for existence. A file that
            # parses at boot and not at send time is a worker that looks
            # ready and refuses every message once the queue opens.
            if not self.mailbox_file:
                errors.append("TITAN_MAILBOX_FILE is not set")
            else:
                from titan.delivery.mailboxes import (
                    MailboxConfigError,
                    load_mailboxes,
                )

                try:
                    registry = load_mailboxes(self.mailbox_file)
                except MailboxConfigError as exc:
                    errors.append(f"TITAN_MAILBOX_FILE is unusable: {exc}")
                else:
                    if not registry:
                        errors.append(f"{self.mailbox_file} lists no enabled mailboxes")
        if self.email_provider == "smartlead":
            if self.smartlead_api_key is None:
                errors.append("TITAN_SMARTLEAD_API_KEY is not set")
            if self.smartlead_campaign_id is None:
                errors.append(
                    "TITAN_SMARTLEAD_CAMPAIGN_ID is not set (Titan will not create "
                    "a sending campaign implicitly)"
                )
        if not self.email_auth_preflight_acknowledged:
            errors.append(
                "TITAN_EMAIL_AUTH_PREFLIGHT_ACKNOWLEDGED is false "
                "(SPF/DKIM/DMARC not acknowledged)"
            )
        if not self.sender_mailing_address:
            errors.append(
                "TITAN_SENDER_MAILING_ADDRESS is not set (required in the footer "
                "of every commercial message)"
            )
        return errors

    def reply_collection_errors(self) -> list[str]:
        """Reasons the reply poller cannot run.

        Deliberately a startup failure rather than an idle loop. A poller that
        starts with no credentials looks healthy in every dashboard and reads
        nothing, so the first sign of trouble is a complaint from somebody who
        asked to be removed three weeks ago and kept hearing from us. Refusing
        to boot puts the failure where it can be seen.
        """
        errors: list[str] = []
        if not self.imap_host:
            errors.append("TITAN_IMAP_HOST is not set")
        if not self.imap_username:
            errors.append("TITAN_IMAP_USERNAME is not set")
        if self.imap_password is None:
            errors.append("TITAN_IMAP_PASSWORD is not set")
        return errors


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that a misconfiguration fails once, loudly, at first access rather
    than intermittently deep inside a request.
    """
    return Settings()
