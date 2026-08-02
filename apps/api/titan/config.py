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

    # ---------------------------------------------------------------- email
    email_provider: Literal["mock", "resend"] = "mock"
    resend_api_key: SecretStr | None = None
    resend_webhook_secret: SecretStr | None = None

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
    def _fail_closed_on_production(self) -> Settings:
        """Production may not run with placeholder or missing security config."""
        if self.environment is not Environment.PRODUCTION:
            return self

        missing: list[str] = []
        if self.auth_mode == "clerk" and self.clerk_issuer_url is None:
            missing.append("TITAN_CLERK_ISSUER_URL")
        if self.auth_mode == "local" and self.local_jwt_secret is None:
            missing.append("TITAN_LOCAL_JWT_SECRET")
        if self.demo_mode:
            missing.append("TITAN_DEMO_MODE must be false in production")
        if missing:
            raise ValueError(
                "Refusing to start in production with incomplete configuration: "
                + ", ".join(missing)
            )
        return self

    # ------------------------------------------------------------- helpers
    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

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
