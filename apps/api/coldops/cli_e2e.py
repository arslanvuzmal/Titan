"""``coldops e2e`` -- one real lead, through the whole pipeline, to your own inbox.

    coldops e2e start  --workspace titan --site https://arslanvuzmallone.com \\
                       --to you@example.com            # shows the plan
    coldops e2e start  ... --apply                     # does it
    coldops e2e send   --workspace titan               # after you approve the draft
    coldops e2e status --workspace titan               # where it has got to

What it proves is the path, not a shortcut around it. ``start`` makes one lead
for the site, in a campaign of its own that stays paused (so the campaign loop
never discovers, drafts or follows up from it), records the inbox you named as
its address, and starts the same ``LeadResearchWorkflow`` every lead goes
through: crawl, findings, score, draft. You approve the draft in the CRM like
any other. ``send`` hands that approval to the waiting workflow, which queues
it; the outbox worker sends it under the same rules as cold mail. Then you open
it, click the link and reply, and ``status`` -- and the Pipeline and Activity
pages -- show delivered, seen, replied and classified as they happen.

The inbox must be listed in ``COLDOPS_TEST_RECIPIENTS`` first. That listing is
what excuses this one message from the placement gate and the paused campaign
(see :mod:`coldops.delivery.operator_test`); nothing else is excused.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import uuid
from urllib.parse import urlparse

from sqlalchemy import select, text, update

from coldops.config import get_settings

#: The campaign every test lead lives in. Paused, always.
CAMPAIGN_SLUG = "coldops-e2e-test"
CAMPAIGN_NAME = "ColdOps end-to-end test"


def _domain(site: str) -> tuple[str, str]:
    """The canonical domain and a crawlable URL for what the operator typed."""
    url = site if "://" in site else f"https://{site}"
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host, url


async def _workspace_id(slug: str) -> uuid.UUID | None:
    from coldops.db.models import Workspace
    from coldops.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.slug == slug))
        ).scalar_one_or_none()


async def _test_campaign_id(workspace_id: uuid.UUID) -> uuid.UUID | None:
    from coldops.db.models import Campaign
    from coldops.db.session import workspace_session

    async with workspace_session(workspace_id) as session:
        return (
            await session.execute(
                select(Campaign.id).where(Campaign.slug == CAMPAIGN_SLUG)
            )
        ).scalar_one_or_none()


# --------------------------------------------------------------------- start
async def _start(args: argparse.Namespace) -> int:
    from coldops.db.enums import (
        CampaignStatus,
        ContactSource,
        DraftStatus,
        LeadStatus,
        VerificationStatus,
    )
    from coldops.db.models import (
        Campaign,
        CampaignPolicy,
        CampaignSender,
        Contact,
        ContactChannel,
        ContactVerification,
        Lead,
        Message,
        MessageDraft,
        Organization,
        SenderIdentity,
        Workspace,
    )
    from coldops.db.session import workspace_session, workspace_unit_of_work
    from coldops.delivery import operator_test
    from coldops.delivery.inbound import _warmup_and_seed_addresses
    from coldops.delivery.suppression import is_suppressed

    settings = get_settings()
    to = args.to.strip().lower()
    domain, site = _domain(args.site)
    if not domain:
        print(f"{args.site!r} is not a website address.")
        return 1

    if not operator_test.listed(settings, to):
        print(f"{to} is not listed as one of your test inboxes. Add it to .env first:")
        print()
        print(f"  COLDOPS_TEST_RECIPIENTS={to}")
        print()
        print("then restart the api, outbox and temporal workers so they all read it,")
        print("and run this again.")
        return 1

    workspace_id = await _workspace_id(args.workspace)
    if workspace_id is None:
        print(f"no workspace with slug {args.workspace!r}")
        return 1

    async with workspace_session(workspace_id) as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        senders = list(
            (
                await session.execute(
                    select(SenderIdentity).order_by(SenderIdentity.from_email)
                )
            )
            .scalars()
            .all()
        )
        suppressed = await is_suppressed(session, workspace_id=workspace_id, email=to)
        mode = workspace.operating_mode
        authorized = workspace.sending_authorized

    ours = {s.from_email.strip().lower() for s in senders} | {
        (s.reply_to_email or "").strip().lower() for s in senders
    }
    ours |= _warmup_and_seed_addresses()
    if to in ours:
        print(
            f"{to} is one of ColdOps's own mailboxes, test inboxes or warm-up partners. "
            "Mail to it is never treated as a lead's. Use an inbox outside the system."
        )
        return 1
    if suppressed is not None:
        print(f"{to} is on the do-not-contact list ({suppressed.reason.value}).")
        return 1

    # Switched off for cold mail is fine: the test is excused from that, and
    # from nothing else a mailbox can be refused for.
    from coldops.db.models.identity import SENDER_INACTIVE

    def _blocking(s: SenderIdentity) -> list[str]:
        return [e for e in s.authorization_errors() if e != SENDER_INACTIVE]

    usable = [s for s in senders if not _blocking(s)]
    print(f"End-to-end test: {domain} -> {to}")
    print()
    print("What has to be true for it to send:")
    print(
        f"  sending switched on (COLDOPS_PRODUCTION_SENDING_ENABLED)  {_yn(settings.production_sending_enabled)}"
    )
    print(
        f"  carrier                                                 {settings.email_provider}"
    )
    print(
        f"  workspace may send                                      {_yn(authorized)}  (mode {mode.value})"
    )
    print(
        f"  mailboxes that may send                                 {len(usable)} of {len(senders)}"
    )
    for s in senders:
        errors = _blocking(s)
        state = "; ".join(errors) if errors else "ready"
        if not errors and not s.is_active:
            state = "ready (paused for cold mail; this one message is excused)"
        print(f"    {s.from_email:<40} {state}")
    if settings.placement_gate_enabled:
        print(
            "  placement gate                                          on; this one message is excused"
        )
    blockers = []
    if not settings.production_sending_enabled:
        blockers.append("sending is switched off for the whole system")
    if not authorized:
        blockers.append("the workspace is not authorized to send")
    if not usable:
        blockers.append("no mailbox passes its authentication checks")
    print()
    if blockers:
        print("It would be drafted but could not send, because:")
        for b in blockers:
            print(f"  - {b}")
        print()

    if not args.apply:
        print("Plan, nothing changed yet:")
        print(
            f"  1. campaign '{CAMPAIGN_NAME}', paused, sending from the mailboxes above"
        )
        print(f"  2. one lead for {domain}, its address {to} (entered by hand)")
        print(f"  3. research it: crawl {site}, findings, score, draft")
        print("  4. you approve the draft in the CRM (Approvals), then: coldops e2e send")
        print()
        print("Run again with --apply to do it.")
        return 0

    now = dt.datetime.now(dt.UTC)
    async with workspace_unit_of_work(workspace_id) as session:
        campaign = (
            await session.execute(select(Campaign).where(Campaign.slug == CAMPAIGN_SLUG))
        ).scalar_one_or_none()
        if campaign is None:
            campaign = Campaign(
                workspace_id=workspace_id,
                name=CAMPAIGN_NAME,
                slug=CAMPAIGN_SLUG,
                # Paused, and kept paused: the campaign loop works only active
                # campaigns, so nothing but this command ever touches it.
                status=CampaignStatus.PAUSED,
                paused_at=now,
                offer_summary="End-to-end test to the operator's own inbox.",
            )
            session.add(campaign)
            await session.flush()
            session.add(
                CampaignPolicy(
                    workspace_id=workspace_id,
                    campaign_id=campaign.id,
                    operating_mode=mode,
                    sending_authorized=True,
                    auto_approve=False,
                    # Whatever the site scores, it is drafted: the test is of
                    # the path, and your own site is not a prospect.
                    min_lead_score=0,
                    require_verified_email=False,
                    daily_send_limit=5,
                    recipient_domain_daily_limit=5,
                    min_spacing_seconds=0,
                    max_followups=0,
                    respect_quiet_hours=False,
                    allowed_contact_sources=[ContactSource.MANUAL_ENTRY.value],
                )
            )
            for s in usable or senders:
                session.add(
                    CampaignSender(
                        workspace_id=workspace_id,
                        campaign_id=campaign.id,
                        sender_identity_id=s.id,
                    )
                )
            print(f"created campaign '{CAMPAIGN_NAME}' (paused)")

        org = (
            await session.execute(
                select(Organization).where(Organization.canonical_domain == domain)
            )
        ).scalar_one_or_none()
        if org is None:
            org = Organization(
                workspace_id=workspace_id,
                display_name=domain,
                normalized_name=domain,
                canonical_domain=domain,
                website_url=site,
            )
            session.add(org)
            await session.flush()

        lead = (
            await session.execute(
                select(Lead).where(
                    Lead.campaign_id == campaign.id, Lead.organization_id == org.id
                )
            )
        ).scalar_one_or_none()
        if lead is not None:
            sent = (
                await session.execute(
                    select(Message.id).where(
                        Message.lead_id == lead.id, Message.sent_at.is_not(None)
                    )
                )
            ).first()
            if sent is not None and not args.again:
                print(
                    "This lead has already been sent its test email. "
                    "`coldops e2e status` shows how far it got; pass --again to start over."
                )
                return 1
            lead.status = LeadStatus.DISCOVERED
            lead.status_reason = "end-to-end test restarted"
            # The earlier draft never went out; a fresh one replaces it, so
            # nothing -- the housekeeping sweep included -- queues the old one.
            await session.execute(
                update(MessageDraft)
                .where(
                    MessageDraft.lead_id == lead.id,
                    MessageDraft.status.in_(
                        (
                            DraftStatus.GENERATED,
                            DraftStatus.AWAITING_APPROVAL,
                            DraftStatus.APPROVED,
                        )
                    ),
                    ~select(Message.id)
                    .where(Message.draft_id == MessageDraft.id)
                    .exists(),
                )
                .values(status=DraftStatus.SUPERSEDED)
            )
        else:
            lead = Lead(
                workspace_id=workspace_id,
                campaign_id=campaign.id,
                organization_id=org.id,
                status=LeadStatus.DISCOVERED,
                status_reason="end-to-end test",
            )
            session.add(lead)
            await session.flush()

        channel = (
            (
                await session.execute(
                    select(ContactChannel).where(
                        ContactChannel.normalized_value == to,
                        ContactChannel.channel_type == "email",
                    )
                )
            )
            .scalars()
            .first()
        )
        if channel is not None and channel.source != ContactSource.MANUAL_ENTRY:
            print(
                f"{to} is already on file as a lead's published address "
                f"({channel.source.value}); use a different inbox for the test."
            )
            return 1
        if channel is None:
            contact = Contact(workspace_id=workspace_id, organization_id=org.id)
            session.add(contact)
            await session.flush()
            channel = ContactChannel(
                workspace_id=workspace_id,
                contact_id=contact.id,
                channel_type="email",
                value=args.to.strip(),
                normalized_value=to,
                value_domain=to.split("@", 1)[1],
                source=ContactSource.MANUAL_ENTRY,
                source_url=None,
                discovered_at=now,
                verification_status=VerificationStatus.PROVIDER_VERIFIED,
                confidence=1.0,
                is_active=True,
            )
            session.add(channel)
            await session.flush()
            # The send gate refuses an address nobody ever checked. This one
            # was checked by the only party who can: its owner.
            session.add(
                ContactVerification(
                    workspace_id=workspace_id,
                    channel_id=channel.id,
                    provider="operator",
                    result=VerificationStatus.PROVIDER_VERIFIED,
                    mx_present=True,
                    detail={"check": "the operator owns this inbox (coldops e2e)"},
                    verified_at=now,
                )
            )
        channel.is_active = True
        lead.primary_contact_channel_id = channel.id
        campaign_id, lead_id = campaign.id, lead.id

    from temporalio.exceptions import WorkflowAlreadyStartedError

    from coldops.workers.temporal_worker import RESEARCH_QUEUE, connect
    from coldops.workflows.research import research_workflow_id
    from coldops.workflows.types import ResearchLeadInput

    workflow_id = research_workflow_id(str(workspace_id), str(campaign_id), str(lead_id))
    client = await connect()
    # A restart: the earlier run may still be waiting for an approval of the
    # draft just retired. Its name is the lead's, so it has to end first.
    try:
        from temporalio.client import WorkflowExecutionStatus

        handle = client.get_workflow_handle(workflow_id)
        if (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
            await handle.terminate(reason="coldops e2e restarted")
            print("ended the earlier research run for this lead")
    except Exception:  # noqa: S110 - no earlier run is the usual case
        pass
    try:
        await client.start_workflow(
            "LeadResearchWorkflow",
            ResearchLeadInput(
                workspace_id=str(workspace_id),
                campaign_id=str(campaign_id),
                lead_id=str(lead_id),
                run_key=f"e2e:{lead_id}:{now:%Y%m%dT%H%M%S}",
                seed_url=site,
            ),
            id=workflow_id,
            task_queue=RESEARCH_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        print("Research for this lead is already running; not started twice.")
    print()
    print(f"Started. Lead {lead_id}.")
    print("Research takes a few minutes. Then:")
    print("  1. `coldops e2e status` until it says the draft is waiting for you")
    print("  2. approve it in the CRM: Approvals")
    print("  3. `coldops e2e send`")
    return 0


def _yn(value: bool) -> str:
    return "yes" if value else "NO"


# ---------------------------------------------------------------------- send
async def _send(args: argparse.Namespace) -> int:
    from temporalio.client import WorkflowExecutionStatus

    from coldops.activities.pipeline import queue_message
    from coldops.db.session import workspace_session
    from coldops.workers.temporal_worker import connect
    from coldops.workflows.research import research_workflow_id
    from coldops.workflows.types import ApprovalDecisionSignal, QueueActivityInput

    workspace_id = await _workspace_id(args.workspace)
    campaign_id = await _test_campaign_id(workspace_id) if workspace_id else None
    if workspace_id is None or campaign_id is None:
        print("No end-to-end test here yet. Run `coldops e2e start` first.")
        return 1

    async with workspace_session(workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT d.id AS draft_id, d.lead_id, d.status,
                           a.id AS approval_id, a.decided_by
                      FROM message_drafts d
                      LEFT JOIN LATERAL (
                           SELECT id, decided_by FROM message_approvals
                            WHERE draft_id = d.id AND decision = 'approved'
                            ORDER BY decided_at DESC LIMIT 1) a ON true
                     WHERE d.workspace_id = :ws AND d.campaign_id = :c
                     ORDER BY d.created_at DESC
                     LIMIT 1
                    """
                ),
                {"ws": workspace_id, "c": campaign_id},
            )
        ).first()

    if row is None:
        print("No draft yet. `coldops e2e status` shows where research has got to.")
        return 1
    if row.approval_id is None:
        print(f"The draft is {row.status}, and nobody has approved it yet.")
        print("Approve it in the CRM (Approvals), then run this again.")
        return 1

    # The workflow that wrote the draft is waiting for exactly this decision;
    # handing it over lets it queue the message itself, as for any lead.
    workflow_id = research_workflow_id(
        str(workspace_id), str(campaign_id), str(row.lead_id)
    )
    client = await connect()
    handle = client.get_workflow_handle(workflow_id)
    try:
        running = (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
    except Exception:
        running = False
    if running:
        await handle.signal(
            "approval_decision",
            ApprovalDecisionSignal(
                decision="approved",
                approval_id=str(row.approval_id),
                decided_by=str(row.decided_by) if row.decided_by else None,
            ),
        )
        print("Approval handed to the research workflow; it queues the email now.")
        print("The outbox sends within a minute or two. `coldops e2e status` to watch.")
        return 0

    # The workflow is gone (the approval window ran out, or a restart): queue
    # it the way the housekeeping sweep would, through the same gates.
    result = await queue_message(
        QueueActivityInput(
            workspace_id=str(workspace_id),
            draft_id=str(row.draft_id),
            approval_id=str(row.approval_id),
            idempotency_key=f"e2e:{row.draft_id}",
        )
    )
    if not result.queued:
        print("Not queued:")
        for reason in result.refused_reasons:
            print(f"  - {reason}")
        return 1
    print(
        "Queued. The outbox sends within a minute or two. `coldops e2e status` to watch."
    )
    return 0


# -------------------------------------------------------------------- status
async def _status(args: argparse.Namespace) -> int:
    from coldops.db.session import workspace_session

    workspace_id = await _workspace_id(args.workspace)
    campaign_id = await _test_campaign_id(workspace_id) if workspace_id else None
    if workspace_id is None or campaign_id is None:
        print("No end-to-end test here yet. Run `coldops e2e start` first.")
        return 1

    params = {"ws": workspace_id, "c": campaign_id}
    async with workspace_session(workspace_id) as session:

        async def rows(sql: str, **extra: object) -> list:
            return list((await session.execute(text(sql), {**params, **extra})).all())

        leads = await rows(
            """
            SELECT l.id, o.canonical_domain, l.status, l.status_reason, l.latest_score,
                   ch.normalized_value AS to_email
              FROM leads l
              JOIN organizations o ON o.id = l.organization_id
              LEFT JOIN contact_channels ch ON ch.id = l.primary_contact_channel_id
             WHERE l.workspace_id = :ws AND l.campaign_id = :c
             ORDER BY l.created_at DESC
            """
        )
        if not leads:
            print("The test campaign has no lead. Run `coldops e2e start --apply`.")
            return 1
        for lead in leads:
            print(f"{lead.canonical_domain} -> {lead.to_email}")
            print(
                f"  lead       {lead.status}  score {lead.latest_score}  {lead.status_reason or ''}"
            )
            lp = {"lead": lead.id}
            for r in await rows(
                """
                SELECT status, failure_reason, started_at FROM research_runs
                 WHERE workspace_id = :ws AND lead_id = :lead
                 ORDER BY started_at DESC LIMIT 1
                """,
                **lp,
            ):
                print(
                    f"  research   {r.status}  {_at(r.started_at)}  {r.failure_reason or ''}"
                )
            findings = await rows(
                """
                SELECT count(*) AS n FROM audit_findings
                 WHERE workspace_id = :ws AND lead_id = :lead
                """,
                **lp,
            )
            print(f"  findings   {findings[0].n}")
            for r in await rows(
                """
                SELECT d.status, d.subject, d.validation_passed, d.created_at,
                       (SELECT decision FROM message_approvals a WHERE a.draft_id = d.id
                         ORDER BY decided_at DESC LIMIT 1) AS decision
                  FROM message_drafts d
                 WHERE d.workspace_id = :ws AND d.lead_id = :lead
                 ORDER BY d.created_at DESC LIMIT 1
                """,
                **lp,
            ):
                print(f'  draft      {r.status}  {_at(r.created_at)}  "{r.subject}"')
                if not r.validation_passed:
                    print("             failed message validation; it cannot be approved")
                print(f"  approval   {r.decision or 'waiting for you (CRM: Approvals)'}")
            for r in await rows(
                """
                SELECT status, attempt_count, last_error, blocked_reason, next_attempt_at
                  FROM outbox_messages
                 WHERE workspace_id = :ws AND lead_id = :lead
                 ORDER BY created_at DESC LIMIT 1
                """,
                **lp,
            ):
                note = r.blocked_reason or r.last_error or ""
                print(f"  outbox     {r.status}  attempts {r.attempt_count}  {note}")
                if r.status == "deferred":
                    print(f"             next try {_at(r.next_attempt_at)}")
            for r in await rows(
                """
                SELECT from_email, state, sent_at, delivered_at, bounced_at
                  FROM messages
                 WHERE workspace_id = :ws AND lead_id = :lead
                 ORDER BY created_at DESC LIMIT 1
                """,
                **lp,
            ):
                print(f"  email      {r.state}  from {r.from_email}")
                print(
                    f"             sent {_at(r.sent_at)}  delivered {_at(r.delivered_at)}"
                    + (f"  BOUNCED {_at(r.bounced_at)}" if r.bounced_at else "")
                )
            seen = await rows(
                """
                SELECT grade, kind, occurred_at FROM engagement_events
                 WHERE workspace_id = :ws AND lead_id = :lead
                 ORDER BY occurred_at
                """,
                **lp,
            )
            if seen:
                for r in seen:
                    print(f"  seen       {r.grade}  ({r.kind})  {_at(r.occurred_at)}")
            else:
                print("  seen       not yet -- open the email and click its link")
            replies = await rows(
                """
                SELECT i.received_at, c.reply_class, c.confidence
                  FROM inbound_messages i
                  LEFT JOIN reply_classifications c ON c.inbound_message_id = i.id
                 WHERE i.workspace_id = :ws AND i.lead_id = :lead
                 ORDER BY i.received_at
                """,
                **lp,
            )
            if replies:
                for r in replies:
                    print(
                        f"  reply      {_at(r.received_at)}  read as {r.reply_class} ({r.confidence:.0%})"
                    )
            else:
                print("  reply      none yet -- reply to it from that inbox")
            print()
    return 0


def _at(value: dt.datetime | None) -> str:
    return value.strftime("%d %b %H:%M UTC") if value else "-"


# -------------------------------------------------------------------- parser
def add_e2e_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = sub.add_parser(
        "e2e", help="one real lead through the whole pipeline, to your own inbox"
    )
    e2e = parser.add_subparsers(dest="e2e_command", required=True)

    start = e2e.add_parser("start", help="make the test lead and start its research")
    start.add_argument("--workspace", default="titan")
    start.add_argument("--site", required=True, help="the website to research")
    start.add_argument(
        "--to", required=True, help="your own inbox, listed in COLDOPS_TEST_RECIPIENTS"
    )
    start.add_argument(
        "--apply", action="store_true", help="do it; without, show the plan"
    )
    start.add_argument(
        "--again", action="store_true", help="start over on a lead already sent to"
    )
    start.set_defaults(func=cmd_e2e)

    send = e2e.add_parser("send", help="queue the approved test draft")
    send.add_argument("--workspace", default="titan")
    send.set_defaults(func=cmd_e2e)

    status = e2e.add_parser("status", help="how far the test has got")
    status.add_argument("--workspace", default="titan")
    status.set_defaults(func=cmd_e2e)


def cmd_e2e(args: argparse.Namespace) -> int:
    from coldops.db.session import dispose_engine
    from coldops.runtime import configure_event_loop

    handler = {"start": _start, "send": _send, "status": _status}[args.e2e_command]

    async def run() -> int:
        try:
            return await handler(args)
        finally:
            await dispose_engine()

    configure_event_loop()
    return asyncio.run(run())


__all__ = ["CAMPAIGN_SLUG", "add_e2e_parser", "cmd_e2e"]
