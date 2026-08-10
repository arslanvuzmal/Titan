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
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    crawl_max_pages: int = Field(default=12, ge=1, le=200)
    crawl_max_depth: int = Field(default=2, ge=0, le=5)
    crawl_timeout_seconds: int = Field(default=120, ge=5, le=900)
    crawl_max_response_bytes: int = Field(default=5_000_000, ge=10_000)
    crawl_max_redirects: int = Field(default=5, ge=0, le=20)
    crawl_user_agent: str = (
        "TitanOS-Research/0.2 (+https://arslanvuzmallone.dev/bot; evidence-only)"
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
    #: validated against the live provider catalogue by `titan validate-models`.
    model_route_extraction: str = "nvidia:meta/llama-3.1-8b-instruct"
    model_route_research: str = "nvidia:nvidia/llama-3.3-nemotron-super-49b-v1"
    model_route_verification: str = "gemini:gemini-2.0-flash"
    model_route_message: str = "nvidia:moonshotai/kimi-k2-instruct"
    model_route_premium: str = "openrouter:anthropic/claude-sonnet-4"

    # --------------------------------------------------------------- budgets
    budget_workspace_daily_usd: float = Field(default=25.0, ge=0)
    budget_campaign_daily_usd: float = Field(default=10.0, ge=0)
    budget_lead_usd: float = Field(default=0.50, ge=0)
    budget_premium_share_max: float = Field(default=0.15, ge=0, le=1)
    budget_hard_stop: bool = True

    # ------------------------------------------------------------- discovery
    google_places_api_key: SecretStr | None = None
    google_places_base_url: AnyHttpUrl = AnyHttpUrl("https://places.googleapis.com/v1")
    agent_reach_api_key: SecretStr | None = None
    agent_reach_base_url: AnyHttpUrl | None = None

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

    # ---------------------------------------------------------------- email
    email_provider: Literal["mock", "resend", "smartlead", "smtp"] = "mock"
    resend_api_key: SecretStr | None = None
    resend_webhook_secret: SecretStr | None = None

    # ------------------------------------------------------------- smartlead
    #: Smartlead is a campaign platform, not a transactional ESP: it exposes no
    #: "send this message now" endpoint. Titan therefore hands each *already
    #: authorized* message to a dedicated single-step campaign, so every gate
    #: still runs here before Smartlead is involved at all. See
    #: titan.delivery.providers.smartlead for what that costs and guarantees.
    smartlead_api_key: SecretStr | None = None
    smartlead_base_url: AnyHttpUrl = AnyHttpUrl("https://server.smartlead.ai/api/v1")
    #: The campaign Titan delivers through. Its sequence must be a single step
    #: whose subject and body are the {{titan_subject}} / {{titan_body}}
    #: variables, so the text Titan validated is the text that is sent.
    smartlead_campaign_id: int | None = None
    smartlead_timeout_seconds: int = Field(default=30, ge=5, le=300)

    #: THE GLOBAL KILL SWITCH (invariant 8, 21).
    #: False means: no outbox row may ever be handed to a real provider,
    #: regardless of workspace, campaign, or user configuration.
    production_sending_enabled: bool = False

    #: Operator acknowledgement that SPF/DKIM/DMARC are configured for the
    #: sending domain. Checked at send time; cannot be set by an API request.
    email_auth_preflight_acknowledged: bool = False

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
    owner_name: str = "Arslan Vuzmal Lone"
    owner_portfolio_url: AnyHttpUrl = AnyHttpUrl("https://arslanvuzmallone.dev")
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
    @field_validator(
        "clerk_issuer_url",
        "agent_reach_base_url",
        "otel_exporter_otlp_endpoint",
        "browser_worker_token",
        "resend_api_key",
        "resend_webhook_secret",
        "smartlead_api_key",
        "smtp_host",
        "smtp_username",
        "smtp_password",
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


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that a misconfiguration fails once, loudly, at first access rather
    than intermittently deep inside a request.
    """
    return Settings()
