"""Erasing what a business never asked us to keep.

Titan holds personal data about people who did not ask to hear from us: an
address scraped from their own website, the message written to them, and the
text of the pages crawled to justify it. Keeping that indefinitely is the part
of a cold-outreach system that is hardest to defend, and nothing in this
codebase expired any of it -- ``messages.body_retained_until`` has existed
since the first migration, carrying the comment *"Body retained only until the
retention window expires"*, and **0 of 807 rows had ever been stamped**.

**What is erased.** The words: the drafted message body, and the crawled page
text behind it. That is the bulk of what is held about a person and the part
with no continuing purpose once they have not replied.

**What is deliberately kept, and this is the whole design.** The address, on
the contact channel and on any suppression entry. Erasing those looks like the
stronger privacy position and is the opposite of one: suppression matches on
the plaintext address, so an erased suppression is a business we would write to
again, and an erased contact channel is a business discovery would find,
re-add and re-mail as though it had never been contacted. The person who most
wants to be left alone is the person that would harm. Data minimisation means
keeping the least that achieves the purpose -- and "never contact them again"
is a purpose that requires knowing who they are.

So this erases content and keeps identity, which is the shape a retention
policy for outreach has to have.

**Never the ones who replied.** A conversation is a relationship, and deleting
half of one is worse than keeping it. A lead with any inbound message is out of
scope entirely, whatever its age.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: How long after the last message to a business its content is kept.
#:
#: Thirty days, the operator's own number and the same window the reputation
#: and evidence rules already use. By this point the message has either
#: produced a reply or it has not, and the follow-up sequence -- four deep at
#: most -- has finished.
RETENTION_DAYS = 30

#: How many leads one pass erases.
#:
#: Bounded because this runs inside the hourly housekeeping pass beside work
#: that matters more, and because an unbounded DELETE over a growing table is
#: how a maintenance job becomes an outage.
DEFAULT_BATCH = 200


@dataclasses.dataclass(frozen=True, slots=True)
class ErasureReport:
    """What one pass erased, in the terms an auditor would ask for."""

    leads_examined: int
    drafts_erased: int
    pages_erased: int
    #: Leads skipped because somebody there wrote back. Counted rather than
    #: silently excluded: "we erase everyone who ignored us" and "we erase
    #: everyone" are different policies, and the difference should be visible
    #: in the numbers rather than only in the code.
    kept_for_reply: int = 0

    @property
    def erased_anything(self) -> bool:
        return bool(self.drafts_erased or self.pages_erased)


#: Leads whose last message is older than the window and who never replied.
#:
#: ``NOT EXISTS`` on inbound rather than a join, so a lead with several inbound
#: messages is excluded once rather than multiplying the row.
_DUE = text(
    """
    SELECT l.id
      FROM leads l
     WHERE l.workspace_id = :ws
       AND EXISTS (
             SELECT 1 FROM messages m
              WHERE m.lead_id = l.id
                AND m.sent_at IS NOT NULL
           )
       AND NOT EXISTS (
             SELECT 1 FROM messages m
              WHERE m.lead_id = l.id
                AND m.sent_at IS NOT NULL
                AND m.sent_at > :cutoff
           )
       AND NOT EXISTS (
             SELECT 1 FROM inbound_messages i
              WHERE i.lead_id = l.id
           )
     ORDER BY l.id
     LIMIT :limit
    """
)

_COUNT_REPLIED = text(
    """
    SELECT count(*)
      FROM leads l
     WHERE l.workspace_id = :ws
       AND EXISTS (SELECT 1 FROM inbound_messages i WHERE i.lead_id = l.id)
       AND NOT EXISTS (
             SELECT 1 FROM messages m
              WHERE m.lead_id = l.id
                AND m.sent_at IS NOT NULL
                AND m.sent_at > :cutoff
           )
    """
)

#: The words of the message. Emptied rather than the row deleted: the row
#: carries the validation report and the claim map, which are the record of
#: *why* something was sent, and destroying that would remove the only evidence
#: that the message was justified at the time.
_ERASE_DRAFTS = text(
    """
    UPDATE message_drafts
       SET body_text = '', body_html = NULL
     WHERE workspace_id = :ws
       AND lead_id = ANY(:leads)
       AND (body_text <> '' OR body_html IS NOT NULL)
    """
)

#: The crawled page text. The URL and the HTTP status stay, so a finding can
#: still name the page it was measured on.
_ERASE_PAGES = text(
    """
    UPDATE pages p
       SET text_excerpt = NULL
      FROM crawl_runs c
      JOIN research_runs r ON r.id = c.research_run_id
     WHERE p.crawl_run_id = c.id
       AND p.workspace_id = :ws
       AND r.lead_id = ANY(:leads)
       AND p.text_excerpt IS NOT NULL
    """
)

_STAMP = text(
    """
    UPDATE messages
       SET body_retained_until = :until
     WHERE workspace_id = :ws
       AND lead_id = ANY(:leads)
       AND body_retained_until IS NULL
    """
)


async def erase_expired(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    now: dt.datetime,
    limit: int = DEFAULT_BATCH,
    retention_days: int = RETENTION_DAYS,
) -> ErasureReport:
    """Erase the content held about businesses that never replied.

    Idempotent by construction: the updates are guarded on the field already
    being non-empty, so a second pass over the same leads erases nothing and
    reports zero rather than repeating the work.
    """
    cutoff = now - dt.timedelta(days=retention_days)
    params = {"ws": workspace_id, "cutoff": cutoff, "limit": limit}

    leads = [row[0] for row in (await session.execute(_DUE, params)).all()]
    kept = int(
        await session.scalar(_COUNT_REPLIED, {"ws": workspace_id, "cutoff": cutoff}) or 0
    )
    if not leads:
        return ErasureReport(0, 0, 0, kept)

    scoped = {"ws": workspace_id, "leads": leads}
    drafts = (await session.execute(_ERASE_DRAFTS, scoped)).rowcount or 0
    pages = (await session.execute(_ERASE_PAGES, scoped)).rowcount or 0
    await session.execute(_STAMP, {**scoped, "until": now})

    return ErasureReport(len(leads), int(drafts), int(pages), kept)


__all__ = ["DEFAULT_BATCH", "RETENTION_DAYS", "ErasureReport", "erase_expired"]
