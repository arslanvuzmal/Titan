"""Domain enumerations.

These are stored as native PostgreSQL enums so that an invalid value is rejected
by the database, not merely by the application layer. Adding a member requires a
migration -- deliberately, because several of these drive send authorization.
"""

from __future__ import annotations

import enum


class WorkspaceRole(enum.StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    RESEARCHER = "researcher"
    REVIEWER = "reviewer"
    OPERATOR = "operator"
    VIEWER = "viewer"


#: Capability matrix. Checked server-side on every mutating route (H-11).
ROLE_CAPABILITIES: dict[WorkspaceRole, frozenset[str]] = {
    WorkspaceRole.OWNER: frozenset(
        {
            "workspace:read",
            "workspace:write",
            "campaign:read",
            "campaign:write",
            "research:read",
            "research:run",
            "draft:read",
            "draft:write",
            "approval:decide",
            "sending:enable",
            "suppression:read",
            "suppression:write",
            "provider:configure",
            "contact:export",
            "workflow:cancel",
            "quota:configure",
        }
    ),
    WorkspaceRole.ADMIN: frozenset(
        {
            "workspace:read",
            "workspace:write",
            "campaign:read",
            "campaign:write",
            "research:read",
            "research:run",
            "draft:read",
            "draft:write",
            "approval:decide",
            "suppression:read",
            "suppression:write",
            "provider:configure",
            "workflow:cancel",
            "quota:configure",
        }
    ),
    WorkspaceRole.RESEARCHER: frozenset(
        {
            "workspace:read",
            "campaign:read",
            "research:read",
            "research:run",
            "draft:read",
        }
    ),
    WorkspaceRole.REVIEWER: frozenset(
        {
            "workspace:read",
            "campaign:read",
            "research:read",
            "draft:read",
            "draft:write",
            "approval:decide",
            "suppression:read",
        }
    ),
    WorkspaceRole.OPERATOR: frozenset(
        {
            "workspace:read",
            "campaign:read",
            "research:read",
            "draft:read",
            "suppression:read",
            "suppression:write",
            "workflow:cancel",
        }
    ),
    WorkspaceRole.VIEWER: frozenset(
        {"workspace:read", "campaign:read", "research:read", "draft:read"}
    ),
}

#: Capabilities that always produce an audit_log row (mission section 18).
SENSITIVE_CAPABILITIES = frozenset(
    {
        "sending:enable",
        "provider:configure",
        "approval:decide",
        "quota:configure",
        "suppression:write",
        "contact:export",
        "workflow:cancel",
    }
)


class CampaignStatus(enum.StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class Region(enum.StrEnum):
    """The market a campaign is aimed at.

    Coarser than a country and deliberately so: it is the level at which
    outreach actually differs. Working hours, public holidays, the shape of a
    business day and what counts as an acceptable approach all vary by market
    far more than they vary between neighbouring countries inside one.

    ``target_country_code`` is not a substitute. It holds one country, so a
    campaign aimed at Europe cannot be expressed by it at all, and a great many
    campaigns leave it empty.
    """

    USA = "usa"
    CANADA = "canada"
    UK = "uk"
    EUROPE = "europe"
    AUSTRALIA = "australia"
    MIDDLE_EAST = "middle_east"
    #: Declared, and outside the six. Distinct from UNSPECIFIED: somebody chose
    #: this, so a campaign in it is configured rather than forgotten.
    OTHER = "other"
    #: Not declared. The default, and what every campaign that predates the
    #: column carries unless a country code implied otherwise.
    UNSPECIFIED = "unspecified"


class SubRegion(enum.StrEnum):
    """A timezone band inside a market that spans several.

    Only defined for the markets where the country tells you nothing about the
    clock. The USA is the case that matters -- a business in California and one
    in Georgia share a country, a market and a working week, and are three hours
    apart -- and Canada and Australia have the same shape.

    Europe is deliberately absent. Its zones follow national borders closely
    enough that ``target_country_code`` already distinguishes them, and a second
    vocabulary saying the same thing would be one more place to disagree.
    """

    #: Arizona is its own band because it does not observe daylight saving.
    #: Folding it into Mountain is correct for four months of the year and an
    #: hour wrong for the other eight.
    US_EASTERN = "us_eastern"
    US_CENTRAL = "us_central"
    US_MOUNTAIN = "us_mountain"
    US_ARIZONA = "us_arizona"
    US_PACIFIC = "us_pacific"
    US_ALASKA = "us_alaska"
    US_HAWAII = "us_hawaii"

    CA_ATLANTIC = "ca_atlantic"
    CA_EASTERN = "ca_eastern"
    CA_CENTRAL = "ca_central"
    CA_MOUNTAIN = "ca_mountain"
    CA_PACIFIC = "ca_pacific"

    AU_EASTERN = "au_eastern"
    AU_CENTRAL = "au_central"
    AU_WESTERN = "au_western"

    #: Not segmented, or not known. The market default applies.
    UNSPECIFIED = "unspecified"


#: Regions with enough shared working conventions to schedule against. OTHER and
#: UNSPECIFIED are absent by design -- there is no local business day for
#: "somewhere", and pretending otherwise is how mail gets sent at 3am.
SCHEDULABLE_REGIONS = frozenset(
    {
        Region.USA,
        Region.CANADA,
        Region.UK,
        Region.EUROPE,
        Region.AUSTRALIA,
        Region.MIDDLE_EAST,
    }
)


class LeadStatus(enum.StrEnum):
    DISCOVERED = "discovered"
    RESEARCHING = "researching"
    RESEARCHED = "researched"
    QUALIFIED = "qualified"
    MANUAL_REVIEW = "manual_review"
    REJECTED = "rejected"
    DRAFTED = "drafted"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    CONTACTED = "contacted"
    REPLIED = "replied"
    MEETING_BOOKED = "meeting_booked"
    DISQUALIFIED = "disqualified"
    SUPPRESSED = "suppressed"
    ARCHIVED = "archived"


#: Statuses from which no further outreach may be generated or sent.
TERMINAL_LEAD_STATUSES = frozenset(
    {
        LeadStatus.REJECTED,
        LeadStatus.REPLIED,
        LeadStatus.MEETING_BOOKED,
        LeadStatus.DISQUALIFIED,
        LeadStatus.SUPPRESSED,
        LeadStatus.ARCHIVED,
    }
)


class FindingCategory(enum.StrEnum):
    TECHNICAL = "technical"
    CONVERSION = "conversion"
    REPUTATION = "reputation"
    RETENTION = "retention"
    FOLLOW_UP = "follow_up"
    BOOKING = "booking"
    AUTOMATION = "automation"
    ACCESSIBILITY = "accessibility"
    PERFORMANCE = "performance"
    SECURITY = "security"
    CONTENT = "content"


class Severity(enum.StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_WEIGHT: dict[Severity, float] = {
    Severity.INFO: 0.0,
    Severity.LOW: 0.25,
    Severity.MEDIUM: 0.5,
    Severity.HIGH: 0.8,
    Severity.CRITICAL: 1.0,
}


class VerificationMethod(enum.StrEnum):
    """How a finding was established. Drives the confidence ceiling."""

    BROWSER_NAVIGATION = "browser_navigation"
    HTTP_RESPONSE = "http_response"
    DOM_ASSERTION = "dom_assertion"
    LIGHTHOUSE_METRIC = "lighthouse_metric"
    AXE_RULE = "axe_rule"
    HEADER_INSPECTION = "header_inspection"
    STRUCTURED_DATA = "structured_data"
    PROVIDER_FIELD = "provider_field"
    MODEL_INFERENCE = "model_inference"


#: A model-inferred finding can never reach the confidence of a measured one,
#: and is never sufficient on its own to justify a claim in an email.
CONFIDENCE_CEILING: dict[VerificationMethod, float] = {
    VerificationMethod.BROWSER_NAVIGATION: 1.0,
    VerificationMethod.HTTP_RESPONSE: 1.0,
    VerificationMethod.DOM_ASSERTION: 1.0,
    VerificationMethod.LIGHTHOUSE_METRIC: 0.95,
    VerificationMethod.AXE_RULE: 0.95,
    VerificationMethod.HEADER_INSPECTION: 1.0,
    VerificationMethod.STRUCTURED_DATA: 0.9,
    VerificationMethod.PROVIDER_FIELD: 0.85,
    VerificationMethod.MODEL_INFERENCE: 0.6,
}

PITCHABLE_METHODS = frozenset(
    m for m in VerificationMethod if m is not VerificationMethod.MODEL_INFERENCE
)


class ContactSource(enum.StrEnum):
    FIRST_PARTY_WEBSITE = "first_party_website"
    PUBLIC_DIRECTORY = "public_directory"
    GOOGLE_PLACES = "google_places"
    VERIFIED_ENRICHMENT = "verified_enrichment"
    EXISTING_CRM_RELATIONSHIP = "existing_crm_relationship"
    PUBLIC_ROLE_ADDRESS = "public_role_address"
    MANUAL_ENTRY = "manual_entry"
    #: Present so guessed addresses can be *recorded and refused* rather than
    #: silently discarded. Never eligible for sending (invariant 6).
    PATTERN_GUESS = "pattern_guess"


#: Sources from which a message may be sent. PATTERN_GUESS is deliberately absent.
ELIGIBLE_CONTACT_SOURCES = frozenset(
    {
        ContactSource.FIRST_PARTY_WEBSITE,
        ContactSource.PUBLIC_DIRECTORY,
        ContactSource.GOOGLE_PLACES,
        ContactSource.VERIFIED_ENRICHMENT,
        ContactSource.EXISTING_CRM_RELATIONSHIP,
        ContactSource.PUBLIC_ROLE_ADDRESS,
        ContactSource.MANUAL_ENTRY,
    }
)


class VerificationStatus(enum.StrEnum):
    UNVERIFIED = "unverified"
    PUBLISHED_FIRST_PARTY = "published_first_party"
    PROVIDER_VERIFIED = "provider_verified"
    RISKY = "risky"
    #: The domain accepts mail for every local part, so the server's acceptance
    #: of *this* address proves nothing about it. Distinct from RISKY and from
    #: UNKNOWN: something conclusive was learned, and what was learned is that
    #: no mailbox-level answer is obtainable from this domain at any price.
    CATCH_ALL = "catch_all"
    INVALID = "invalid"
    UNKNOWN = "unknown"


#: Statuses that permit sending on their own, whatever the provenance.
SENDABLE_VERIFICATION_STATUSES = frozenset(
    {
        VerificationStatus.PUBLISHED_FIRST_PARTY,
        VerificationStatus.PROVIDER_VERIFIED,
    }
)

#: Provenances strong enough to carry a CATCH_ALL address. See
#: :func:`verification_permits_sending` for why this exists at all.
#: Deliberately narrower than ELIGIBLE_CONTACT_SOURCES. A directory listing or
#: a Places record is a third party asserting an address; only these three are
#: someone with direct knowledge of the mailbox putting it in front of us.
_CATCH_ALL_TRUSTED_SOURCES = frozenset(
    {
        ContactSource.FIRST_PARTY_WEBSITE,
        ContactSource.MANUAL_ENTRY,
        ContactSource.EXISTING_CRM_RELATIONSHIP,
    }
)


def verification_permits_sending(
    status: VerificationStatus, source: ContactSource
) -> bool:
    """Whether verification and provenance together permit a send.

    A frozenset was enough while every status could be judged alone. CATCH_ALL
    cannot be, and collapsing it into either bucket gets a real population
    wrong:

    * Treat it as sendable and Titan mails guessed addresses at domains that
      accept everything and bounce nothing -- the mail lands in a spam trap or
      vanishes, and neither outcome is visible as a bounce, so the list quality
      problem never surfaces.
    * Treat it as unsendable and Titan refuses ``enquiries@`` addresses that the
      business published on its own contact page. Catch-all is the default on
      most small-business hosting, which is precisely the population Titan
      targets: this would discard a large share of legitimate leads for a
      property of their mail server they have never thought about.

    What separates the two is not the mail server -- it is identical in both
    cases -- but who put the address in front of us. A human publishing an
    address on their own website is asserting that somebody reads it. That
    assertion is the evidence; the catch-all configuration merely means the
    server will not confirm it independently.

    So provenance decides, which is the same rule invariant 6 already applies to
    the address itself.
    """
    if status in SENDABLE_VERIFICATION_STATUSES:
        return True
    if status is VerificationStatus.CATCH_ALL:
        return source in _CATCH_ALL_TRUSTED_SOURCES
    return False


class DraftStatus(enum.StrEnum):
    GENERATED = "generated"
    VALIDATION_FAILED = "validation_failed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    QUEUED = "queued"


class OutboxStatus(enum.StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    DEFERRED = "deferred"
    SENT = "sent"
    FAILED_PERMANENT = "failed_permanent"
    CANCELLED = "cancelled"


class MessageState(enum.StrEnum):
    """Delivery lifecycle. Monotonic -- see DELIVERY_RANK."""

    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    DEFERRED = "deferred"
    OPENED = "opened"
    CLICKED = "clicked"
    BOUNCED = "bounced"
    COMPLAINED = "complained"
    UNSUBSCRIBED = "unsubscribed"
    FAILED = "failed"


#: Rank used to prevent a delayed webhook from regressing final state
#: (invariant 13). A lower-ranked event never overwrites a higher-ranked one.
#: Terminal negative outcomes outrank engagement signals because they carry
#: compliance consequences that must not be masked by a late 'opened' event.
DELIVERY_RANK: dict[MessageState, int] = {
    MessageState.QUEUED: 0,
    MessageState.DEFERRED: 10,
    MessageState.SENT: 20,
    MessageState.DELIVERED: 30,
    MessageState.OPENED: 40,
    MessageState.CLICKED: 50,
    MessageState.FAILED: 60,
    MessageState.BOUNCED: 70,
    MessageState.UNSUBSCRIBED: 80,
    MessageState.COMPLAINED: 90,
}


class SuppressionReason(enum.StrEnum):
    UNSUBSCRIBE = "unsubscribe"
    COMPLAINT = "complaint"
    HARD_BOUNCE = "hard_bounce"
    REPEATED_SOFT_BOUNCE = "repeated_soft_bounce"
    MANUAL = "manual"
    ROLE_ADDRESS_POLICY = "role_address_policy"
    LEGAL_REQUEST = "legal_request"
    NOT_INTERESTED = "not_interested"


#: Reasons that may never be lifted by an ordinary operator action, and that
#: survive contact deletion (mission section 24).
PERMANENT_SUPPRESSION_REASONS = frozenset(
    {
        SuppressionReason.UNSUBSCRIBE,
        SuppressionReason.COMPLAINT,
        SuppressionReason.HARD_BOUNCE,
        SuppressionReason.LEGAL_REQUEST,
    }
)


class ReplyClass(enum.StrEnum):
    INTERESTED = "interested"
    WANTS_MORE_INFO = "wants_more_info"
    WANTS_PRICING = "wants_pricing"
    WANTS_CALL = "wants_call"
    REFERRAL = "referral"
    NOT_NOW = "not_now"
    NOT_INTERESTED = "not_interested"
    OBJECTION = "objection"
    WRONG_PERSON = "wrong_person"
    OUT_OF_OFFICE = "out_of_office"
    UNSUBSCRIBE = "unsubscribe"
    COMPLAINT = "complaint"
    AUTOMATED = "automated"
    BOUNCE = "bounce"
    UNKNOWN = "unknown"


#: Classes that represent a real human being at the other end. Any of these
#: halts the sequence immediately (invariant 15).
HUMAN_REPLY_CLASSES = frozenset(
    {
        ReplyClass.INTERESTED,
        ReplyClass.WANTS_MORE_INFO,
        ReplyClass.WANTS_PRICING,
        ReplyClass.WANTS_CALL,
        ReplyClass.REFERRAL,
        ReplyClass.NOT_NOW,
        ReplyClass.NOT_INTERESTED,
        ReplyClass.OBJECTION,
        ReplyClass.WRONG_PERSON,
        ReplyClass.UNSUBSCRIBE,
        ReplyClass.COMPLAINT,
    }
)

#: Classes that immediately and permanently suppress the address.
SUPPRESSING_REPLY_CLASSES = {
    ReplyClass.UNSUBSCRIBE: SuppressionReason.UNSUBSCRIBE,
    ReplyClass.COMPLAINT: SuppressionReason.COMPLAINT,
    ReplyClass.NOT_INTERESTED: SuppressionReason.NOT_INTERESTED,
}


class Industry(enum.StrEnum):
    LAW_FIRM = "law_firm"
    GYM_FITNESS = "gym_fitness"
    RESTAURANT = "restaurant"
    REAL_ESTATE = "real_estate"
    HVAC_HOME_SERVICES = "hvac_home_services"
    MED_SPA = "med_spa"
    DENTIST = "dentist"
    GENERAL = "general"


class ModelTask(enum.StrEnum):
    EXTRACTION = "extraction"
    RESEARCH = "research"
    VERIFICATION = "verification"
    MESSAGE = "message"
    PREMIUM = "premium"


class WorkflowRunStatus(enum.StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    TERMINATED = "terminated"


# ==========================================================================
# Smartlead lead import
# ==========================================================================
# Recovered from the deployed database on 2026-08-11, where they existed as
# PostgreSQL enum types created by a migration that was never pushed to this
# repository. The labels below are the live labels, verified against pg_enum
# rather than reconstructed from memory: a StrEnum whose values differ from the
# type in the database produces a LookupError on read, and only for the rows
# that happen to use the mismatched label.
#
# These describe the *lead-import* path, in which leads are pushed into a
# Smartlead campaign and their state tracked there. It is not how this
# repository sends today -- titan.delivery.providers.smartlead hands Smartlead
# one already-authorized message at a time -- but 295 leads carry a status and
# 17 were genuinely imported, so the vocabulary is history worth keeping
# readable.


class SmartleadImportStatus(enum.StrEnum):
    """Where a lead stands with respect to Smartlead."""

    #: The default, and the value 278 of 295 leads hold: never sent there.
    NOT_IMPORTED = "not_imported"
    #: Claimed by a batch that has not finished. Distinguishes "in flight"
    #: from "never attempted" after a crash.
    RESERVED = "reserved"
    IMPORTED = "imported"
    #: Deliberately not sent, with the reason in ``smartlead_skipped_reason``.
    SKIPPED = "skipped"
    FAILED = "failed"


class SmartleadBatchStatus(enum.StrEnum):
    """Outcome of one import run."""

    #: Counted and reported what it *would* do, without writing to Smartlead.
    DRY_RUN = "dry_run"
    RUNNING = "running"
    COMPLETED = "completed"
    #: Some leads imported and some failed. Kept distinct from FAILED because
    #: the two need different follow-up: retry the remainder, or investigate.
    PARTIAL = "partial"
    FAILED = "failed"


class SmartleadEventType(enum.StrEnum):
    """Normalized Smartlead webhook event.

    ``UNKNOWN`` is deliberate: an event type this code does not recognise is
    still recorded, because discarding it would hide a provider change until
    somebody noticed the silence.
    """

    SENT = "sent"
    OPENED = "opened"
    CLICKED = "clicked"
    REPLIED = "replied"
    BOUNCED = "bounced"
    UNSUBSCRIBED = "unsubscribed"
    UNKNOWN = "unknown"
