"""The API a voice agent calls while it is on the phone.

A cold call has no second draft. The agent has a few seconds to say something
the practice recognises as true about itself, and everything it says has to
come from the same evidence the rest of the estate is built on -- otherwise a
receptionist checks the page, finds it fine, and the call is over along with
any chance of a later one.

So nothing here generates claims. Every sentence the agent is given is
assembled from a finding that a crawler measured, with the page URL attached so
the person on the phone can look at it while they are listening. The projects
it may mention are read from the same registry the email composer uses, which
is a file of the operator's own work -- no client names, no invented outcomes.

**Why this is an API rather than a prompt.** A static prompt goes stale the
moment a practice fixes its booking page, and a voice agent asserting a defect
that was repaired last week is worse than one that never rang. Fetching at dial
time means the agent is told what is true now, and told nothing at all when the
evidence has aged past the point where it can be asserted down a telephone.

**What it refuses to do.** It will not hand back a lead that is suppressed,
already called, or whose evidence is stale -- the gate runs here rather than in
whatever dials, because the dialler is the component most likely to be replaced
and least likely to reimplement the rules correctly.
"""

from __future__ import annotations

import datetime as dt
import hmac
import uuid
from collections import defaultdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from titan.config import get_settings
from titan.db.session import get_sessionmaker
from titan.intelligence import case_studies as case_registry
from titan.intelligence.call_list import TIER_POINTS, CallTarget

router = APIRouter(prefix="/api/v1/calls", tags=["calls"])


# --------------------------------------------------------------------- auth
async def agent_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """A shared secret, not a user session.

    The caller is a voice runtime, not a person: there is no login, no refresh
    and nobody to prompt. A bearer token it holds is the honest shape. It is
    compared in full rather than by prefix so a truncated copy fails closed.
    """
    settings = get_settings()
    expected = settings.call_agent_token
    if expected is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "calling API is not configured; set TITAN_CALL_AGENT_TOKEN",
        )
    presented = (authorization or "").removeprefix("Bearer ").strip()
    # Constant-time. A plain != returns on the first differing byte, which
    # leaks the token a character at a time to anybody willing to time the
    # responses -- and this token is the whole authentication for an endpoint
    # that hands out lead names and phone numbers.
    if not presented or not hmac.compare_digest(
        presented, expected.get_secret_value()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad agent token")


# ------------------------------------------------------------------ schemas
class CallCandidate(BaseModel):
    lead_id: str
    practice: str
    phone: str
    website: str | None
    tier: str
    score: float
    opener: str
    ask: str
    evidence_url: str | None
    observed: str


class ProjectOut(BaseModel):
    name: str
    summary: str
    url: str


class Briefing(BaseModel):
    """Everything the agent may say, and nothing it may not."""

    lead_id: str
    practice: str
    phone: str
    website: str | None
    #: The one fact the call opens with.
    opener: str
    ask: str
    evidence_url: str | None
    observed: str
    tier: str
    evidence_age_days: int | None
    review_count: int | None
    rating: float | None
    #: Real work of the operator's that answers this defect. Empty rather than
    #: padded when nothing matches: a credential that does not fit the problem
    #: is worse than none, and the composer learned that the expensive way.
    projects: list[ProjectOut]
    #: Pasted into the agent's system prompt at dial time.
    guidance: str


class OutcomeIn(BaseModel):
    lead_id: str
    phone: str
    stage: int = Field(default=1, ge=1, le=2)
    outcome: str
    duration_seconds: int | None = None
    contact_name: str | None = None
    contact_role: str | None = None
    contact_email: str | None = None
    consent_to_email: bool = False
    bant_budget: int | None = Field(default=None, ge=0, le=3)
    bant_authority: int | None = Field(default=None, ge=0, le=3)
    bant_need: int | None = Field(default=None, ge=0, le=3)
    bant_timing: int | None = Field(default=None, ge=0, le=3)
    knew_about_defect: bool | None = None
    callback_at: dt.datetime | None = None
    notes: str | None = None


VALID_OUTCOMES = {
    "no_answer", "gatekeeper", "reached_dm", "callback",
    "not_interested", "do_not_call", "wrong_number", "interested",
}


# ------------------------------------------------------------------ helpers
def _workspace_id() -> uuid.UUID:
    """Which estate the agent is calling for.

    Reuses TITAN_IMAP_WORKSPACE_ID rather than inventing a second setting for
    the same fact: the inbound worker already resolves replies against it, and
    two settings naming one workspace is two chances to disagree.
    """
    configured = get_settings().imap_workspace_id
    if not configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "no workspace configured; set TITAN_IMAP_WORKSPACE_ID",
        )
    return uuid.UUID(configured)


_CANDIDATE_SQL = """
SELECT l.id::text AS lead_id, o.display_name AS practice, o.phone_e164 AS phone,
       o.website_url AS website, o.industry, f.issue_type, f.page_url,
       coalesce(f.observed_value,'') AS observed,
       o.review_count, o.rating,
       EXTRACT(day FROM now() - f.created_at)::int AS age_days
  FROM leads l
  JOIN organizations o  ON o.id = l.organization_id
  JOIN campaigns c      ON c.id = l.campaign_id
  JOIN audit_findings f ON f.lead_id = l.id
       AND f.confidence >= 0.7 AND f.contradicted IS NOT TRUE
       AND f.created_at > now() - interval '14 days'
 WHERE l.workspace_id = :ws
   AND c.status = 'active'
   AND o.phone_e164 IS NOT NULL
   AND l.replied_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM call_suppressions s
                    WHERE s.workspace_id = l.workspace_id
                      AND s.phone_e164 = o.phone_e164)
   AND NOT EXISTS (SELECT 1 FROM call_outcomes co
                    WHERE co.lead_id = l.id AND co.stage = 1)
   {extra}
"""


async def _candidates(extra: str = "", params: dict[str, Any] | None = None):
    """Best-tier target per practice, gate applied, ranked."""
    sm = get_sessionmaker()
    async with sm() as session:
        rows = (
            await session.execute(
                text(_CANDIDATE_SQL.format(extra=extra)),
                {"ws": _workspace_id(), **(params or {})},
            )
        ).mappings().all()

    grouped: dict[str, list[tuple[CallTarget, str]]] = defaultdict(list)
    for r in rows:
        target = CallTarget(
            lead_id=r["lead_id"], practice=r["practice"], phone=r["phone"],
            website=r["website"], issue_type=r["issue_type"],
            page_url=r["page_url"], observed=r["observed"],
            review_count=r["review_count"],
            rating=float(r["rating"]) if r["rating"] is not None else None,
            evidence_age_days=r["age_days"],
        )
        grouped[r["lead_id"]].append((target, r["industry"]))

    # Best *tier* per practice, not highest severity. Severity and callability
    # are different axes: an accessibility violation is high severity and worth
    # nothing on the phone, while an eleven-field form is medium and is the
    # whole call.
    best = [
        max(v, key=lambda pair: TIER_POINTS[pair[0].tier]) for v in grouped.values()
    ]
    callable_ = [(t, ind) for t, ind in best if t.worth_calling]
    callable_.sort(key=lambda pair: -pair[0].score)
    return callable_


def _projects_for(issue_type: str, industry: str | None) -> list[ProjectOut]:
    settings = get_settings()
    studies = case_registry.registry(settings.case_studies_path)
    chosen = case_registry.select(
        studies, industry=industry, family=None, issue_type=issue_type
    )
    if chosen is None:
        return []
    return [ProjectOut(name=chosen.name, summary=chosen.summary, url=str(chosen.url))]


def _guidance(target: CallTarget, projects: list[ProjectOut]) -> str:
    """The rules the agent is held to, written where it will read them.

    Stated as constraints rather than encouragement. A voice model given a
    persuasion brief will invent the evidence it needs to be persuasive, and
    invented evidence about somebody's own website is discovered in seconds --
    by the person who owns it, while you are still talking.
    """
    project_line = (
        f"If asked what you have built: {projects[0].name} -- {projects[0].summary} "
        f"({projects[0].url})."
        if projects
        else "If asked what you have built, say you can send examples by email."
    )
    return "\n".join(
        [
            "You are an AI assistant calling on behalf of Arslan Vuzmal Lone.",
            "Say so in your first sentence. Never imply you are a person.",
            "",
            f"The practice is {target.practice}.",
            f"The one fact you may assert: {target.opener()}",
            f"Evidence they can open right now: {target.page_url or target.website or 'their website'}",
            "",
            "Rules that do not bend:",
            "- Assert nothing about their website except the fact above.",
            "- If they say it is already fixed, thank them, apologise briefly,",
            "  and offer to take them off the list. Do not argue.",
            "- Do not quote prices. Do not promise timelines.",
            "- Do not claim clients or results. Only the project named below.",
            "- If they ask to be removed, agree immediately and end the call.",
            "",
            f"Your goal is one thing: {target.ask()}",
            "Get a name and an email address. That is the whole call.",
            "Do not ask for a meeting. Do not pitch. Forty seconds is plenty.",
            "",
            "If they say yes to the recording, confirm the email address back to",
            "them letter by letter, then say it will arrive within the hour.",
            "",
            project_line,
        ]
    )


# ------------------------------------------------------------------- routes
@router.get("/next", response_model=list[CallCandidate])
async def next_calls(
    limit: int = Query(default=20, ge=1, le=100),
    country: str | None = Query(default=None, description="ISO-3166-1 alpha-2"),
    _: None = Depends(agent_auth),
) -> list[CallCandidate]:
    """The ranked list, gate already applied."""
    extra, params = "", {}
    if country:
        extra = "AND upper(coalesce(o.country_code, '')) = :cc"
        params["cc"] = country.upper()
    rows = await _candidates(extra, params)
    return [
        CallCandidate(
            lead_id=t.lead_id, practice=t.practice, phone=t.phone,
            website=t.website, tier=t.tier, score=t.score,
            opener=t.opener(), ask=t.ask(),
            evidence_url=t.page_url, observed=t.observed,
        )
        for t, _ind in rows[:limit]
    ]


@router.get("/briefing", response_model=Briefing)
async def briefing(
    lead_id: str = Query(...),
    _: None = Depends(agent_auth),
) -> Briefing:
    """Everything the agent may say about one practice, fetched at dial time."""
    rows = await _candidates("AND l.id = :lead", {"lead": uuid.UUID(lead_id)})
    if not rows:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no callable evidence for this lead -- it is suppressed, already "
            "called, or its evidence is too old to assert on a call",
        )
    target, industry = rows[0]
    projects = _projects_for(target.issue_type, industry)
    return Briefing(
        lead_id=target.lead_id, practice=target.practice, phone=target.phone,
        website=target.website, opener=target.opener(), ask=target.ask(),
        evidence_url=target.page_url, observed=target.observed, tier=target.tier,
        evidence_age_days=target.evidence_age_days,
        review_count=target.review_count, rating=target.rating,
        projects=projects, guidance=_guidance(target, projects),
    )


@router.post("/outcome", status_code=status.HTTP_201_CREATED)
async def record_outcome(
    body: OutcomeIn,
    _: None = Depends(agent_auth),
) -> dict[str, Any]:
    """Write what the call bought, and honour a refusal immediately."""
    if body.outcome not in VALID_OUTCOMES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"outcome must be one of {sorted(VALID_OUTCOMES)}",
        )
    ws = _workspace_id()
    sm = get_sessionmaker()
    async with sm() as session:
        await session.execute(
            text(
                """
                INSERT INTO call_outcomes
                  (workspace_id, lead_id, phone_e164, stage, called_at,
                   duration_seconds, outcome, contact_name, contact_role,
                   contact_email, consent_to_email, bant_budget, bant_authority,
                   bant_need, bant_timing, knew_about_defect, callback_at, notes)
                VALUES
                  (:ws, :lead, :phone, :stage, now(),
                   :dur, :outcome, :name, :role,
                   :email, :consent, :b, :a, :n, :t, :knew, :cb, :notes)
                """
            ),
            {
                "ws": ws, "lead": uuid.UUID(body.lead_id), "phone": body.phone,
                "stage": body.stage, "dur": body.duration_seconds,
                "outcome": body.outcome, "name": body.contact_name,
                "role": body.contact_role, "email": body.contact_email,
                "consent": body.consent_to_email, "b": body.bant_budget,
                "a": body.bant_authority, "n": body.bant_need,
                "t": body.bant_timing, "knew": body.knew_about_defect,
                "cb": body.callback_at, "notes": body.notes,
            },
        )
        # A refusal is acted on here, not queued for later. Somebody asking not
        # to be rung again has said the one thing that must take effect before
        # the next dial, and a suppression written by a separate nightly job is
        # a suppression that rings them once more first.
        if body.outcome == "do_not_call":
            await session.execute(
                text(
                    """
                    INSERT INTO call_suppressions
                        (workspace_id, phone_e164, reason, note)
                    VALUES (:ws, :phone, 'requested_on_call', :note)
                    ON CONFLICT (workspace_id, phone_e164) DO NOTHING
                    """
                ),
                {"ws": ws, "phone": body.phone, "note": body.notes},
            )
        await session.commit()
    return {"recorded": True, "suppressed": body.outcome == "do_not_call"}


@router.post("/suppress", status_code=status.HTTP_201_CREATED)
async def suppress(
    phone: str = Query(...),
    reason: str = Query(default="manual"),
    _: None = Depends(agent_auth),
) -> dict[str, Any]:
    """Never ring this number again."""
    sm = get_sessionmaker()
    async with sm() as session:
        await session.execute(
            text(
                """
                INSERT INTO call_suppressions (workspace_id, phone_e164, reason)
                VALUES (:ws, :phone, :reason)
                ON CONFLICT (workspace_id, phone_e164) DO NOTHING
                """
            ),
            {"ws": _workspace_id(), "phone": phone, "reason": reason[:40]},
        )
        await session.commit()
    return {"suppressed": phone}


__all__ = ["router"]
