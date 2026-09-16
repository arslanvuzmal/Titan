"""Re-measuring a claim before it ages out.

The send gate refuses evidence older than thirty days, and nothing acted before
that. So a draft written on day one sat in the queue until day thirty and then
became permanently unsendable: 167 of them on the live estate, plus 255 more
resting on evidence between two and four weeks old -- each a claim about a
defect that may have been fixed a fortnight ago.

Most of these guard one direction. Failing to re-measure costs a stale claim;
reopening the wrong lead writes to somebody who asked us not to, or reopens a
conversation a person is already having.
"""

from __future__ import annotations

import datetime as dt

from titan.db.enums import DraftStatus, LeadStatus
from titan.intelligence.staleness import (
    _LIVE_DRAFTS,
    _REOPENABLE,
    MAX_PER_PASS,
    STALE_AFTER,
    StalenessReport,
)
from titan.policy.engine import MAX_EVIDENCE_AGE


# ------------------------------------------------ the timing, which is the point
def test_it_acts_before_the_gate_refuses() -> None:
    """Planted violation: sweep at the same age the gate refuses.

    Acting at thirty days would be too late by definition -- the draft is
    already unsendable, the crawl and the model call that produced it are
    already wasted, and the lead has been silent for a month. The margin is the
    whole design.
    """
    assert STALE_AFTER < MAX_EVIDENCE_AGE

    margin = MAX_EVIDENCE_AGE - STALE_AFTER
    assert margin >= dt.timedelta(days=7), (
        "a re-crawl, a re-score, a re-draft, a send window and a share of the "
        "daily budget all have to fit inside the margin"
    )


def test_a_fresh_draft_is_left_alone() -> None:
    """Churning healthy drafts would re-crawl the whole estate every week."""
    assert STALE_AFTER > dt.timedelta(days=14), (
        "a draft written today must survive at least a fortnight untouched"
    )


# ------------------------------------------------ what may be reopened
def test_a_decided_lead_is_never_reopened() -> None:
    """Planted violation: reopen on evidence age alone, ignoring status.

    Suppressed means somebody asked not to hear from us. Replied means a person
    is mid-conversation. Rejected means somebody judged it. Re-crawling any of
    them is merely wasteful; re-drafting them writes to someone who is not
    waiting for it, and that is the error here that cannot be taken back.
    """
    forbidden = {
        LeadStatus.SUPPRESSED.value,
        LeadStatus.REPLIED.value,
        LeadStatus.REJECTED.value,
        LeadStatus.ARCHIVED.value,
        LeadStatus.DISQUALIFIED.value,
        LeadStatus.MEETING_BOOKED.value,
        LeadStatus.CONTACTED.value,
    }

    assert not (set(_REOPENABLE) & forbidden), (
        f"reopenable statuses must exclude decided ones: {set(_REOPENABLE) & forbidden}"
    )


def test_reopening_lands_on_a_researchable_status() -> None:
    """A lead put back in a status the planner does not select is a lead that
    stops moving entirely -- worse than the stale draft it replaced."""
    from titan.activities.orchestration import RESEARCHABLE_STATUSES

    researchable = {s.value for s in RESEARCHABLE_STATUSES}

    assert LeadStatus.QUALIFIED.value in researchable


def test_only_live_drafts_are_swept() -> None:
    """A rejected or superseded draft is a closed question.

    Re-researching its lead would reopen a decision somebody already made, and
    superseding an already-superseded draft is churn.
    """
    assert DraftStatus.REJECTED.value not in _LIVE_DRAFTS
    assert DraftStatus.SUPERSEDED.value not in _LIVE_DRAFTS
    assert DraftStatus.QUEUED.value in _LIVE_DRAFTS


# ------------------------------------------------ the bound
def test_one_pass_is_bounded() -> None:
    """Each reopened lead costs a browser crawl, and the crawler is capped at
    eight concurrent activities. Unbounded, this would swamp the same crawler
    discovery and follow-ups depend on."""
    assert 1 <= MAX_PER_PASS <= 50


# ------------------------------------------------ the report
def test_the_report_separates_the_backlog_from_the_flow() -> None:
    """167 drafts were already past the gate when this was written, and that is
    a different thing from drafts ageing towards it. A report that merged them
    would show a huge first pass and look broken afterwards."""
    report = StalenessReport(stale=25, reopened=25, already_unsendable=20, oldest_days=41)

    assert report.already_unsendable <= report.stale
    assert not report.is_noop


def test_nothing_to_do_is_reported_as_nothing() -> None:
    """It runs hourly. Once the backlog drains, the normal result is zero."""
    assert StalenessReport().is_noop
