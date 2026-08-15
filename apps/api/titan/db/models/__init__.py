"""All ORM models.

Importing this package registers every table on ``Base.metadata``. Alembic's
env.py imports exactly this module, so a model file that is not re-exported here
will silently be missing from migrations -- guarded by
tests/db/test_model_registry.py.
"""

from __future__ import annotations

from titan.db.base import Base
from titan.db.models.campaign import Campaign, CampaignPolicy, IndustryPlaybook
from titan.db.models.compliance import (
    AuditLog,
    QuotaCounter,
    SuppressionEntry,
    UsageLedger,
)
from titan.db.models.identity import (
    ProviderConnection,
    SenderHealthSnapshot,
    SenderIdentity,
    User,
    Workspace,
    WorkspaceMember,
)
from titan.db.models.lead import (
    Contact,
    ContactChannel,
    ContactVerification,
    Lead,
    LeadScore,
    LeadSource,
    Organization,
    OrganizationDomain,
    OrganizationLocation,
)
from titan.db.models.messaging import (
    EmailSequence,
    InboundMessage,
    Message,
    MessageApproval,
    MessageDraft,
    OutboxMessage,
    ProviderEvent,
    ReplyClassification,
    SequenceStep,
)
from titan.db.models.ops import (
    Meeting,
    ModelRun,
    PromptVersion,
    Task,
    WorkflowEvent,
    WorkflowRun,
)
from titan.db.models.research import (
    AuditFinding,
    BrowserArtifact,
    BusinessOpportunity,
    CrawlRun,
    FindingEvidence,
    Page,
    ResearchRun,
    SolutionRecommendation,
)
from titan.db.models.smartlead import (
    SmartleadImportBatch,
    SmartleadWebhookEvent,
)

__all__ = [
    "AuditFinding",
    "AuditLog",
    "Base",
    "BrowserArtifact",
    "BusinessOpportunity",
    "Campaign",
    "CampaignPolicy",
    "Contact",
    "ContactChannel",
    "ContactVerification",
    "CrawlRun",
    "EmailSequence",
    "FindingEvidence",
    "InboundMessage",
    "IndustryPlaybook",
    "Lead",
    "LeadScore",
    "LeadSource",
    "Meeting",
    "Message",
    "MessageApproval",
    "MessageDraft",
    "ModelRun",
    "Organization",
    "OrganizationDomain",
    "OrganizationLocation",
    "OutboxMessage",
    "Page",
    "PromptVersion",
    "ProviderConnection",
    "ProviderEvent",
    "QuotaCounter",
    "ReplyClassification",
    "ResearchRun",
    "SenderHealthSnapshot",
    "SenderIdentity",
    "SequenceStep",
    "SmartleadImportBatch",
    "SmartleadWebhookEvent",
    "SolutionRecommendation",
    "SuppressionEntry",
    "Task",
    "UsageLedger",
    "User",
    "WorkflowEvent",
    "WorkflowRun",
    "Workspace",
    "WorkspaceMember",
]
