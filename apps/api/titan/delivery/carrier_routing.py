"""Which carrier campaign a message is handed to, and therefore when it is sent.

Both carriers Titan speaks to hold **one clock per campaign**. Smartlead's
schedule names a timezone, a working week and a sending window; Instantly's
does the same. Whichever campaign a message is handed to is the campaign whose
clock decides what hour it lands in somebody's morning.

So this module answers one question -- *which carrier campaign?* -- and the
answer is a scheduling decision wearing an id.

**Routed by the recipient's market, not the sender's campaign.** The old rule
read the id off the Titan campaign. That is exactly right while a campaign is
one city, because then the campaign is the market. It stops being right the
moment a campaign is a business type: "dentists worth writing to" spans London,
Dubai, Sydney and Los Angeles, and one column can name one carrier. Every lead
would ride whichever market's clock happened to be recorded there.

**A market is not a clock, and the gap is measurable.** Routing by market
alone means the market's *representative* zone answers for everything inside
it. On this workspace that is 539 leads of 2,733 -- one in five -- scheduled on
an hour that is not theirs: Toronto answering for Vancouver, New York for Los
Angeles, Dubai for Riyadh, Berlin for Dublin. So a carrier campaign may be
provisioned for a specific clock, and the recipient's own zone is tried first.

**Falling back is allowed; guessing is not.** Four answers are possible and
they are not the same:

* the carrier provisioned for the recipient's exact timezone, where one exists;
* the market's own carrier, when the recipient's country resolves to a market
  that has one;
* the Titan campaign's recorded carrier, when the recipient's market cannot be
  established. This is precisely today's behaviour, so a lead with no location
  is routed exactly as it was before this module existed;
* nothing, which lets the provider fall back to its configured default.

What must never happen is a fifth: a recipient whose market *is* known being
routed to a different market's carrier because that one was easier to reach. A
message sent on the wrong clock is not a slightly worse message. It arrives at
midnight, or on a Friday in the Gulf, and it burns the address.

**Nothing here sends, schedules, or writes.** It reads two rows and returns a
decision with its reason attached, so the decision trail can record *why* a
message went where it went rather than only where.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import Region
from titan.db.models import CarrierCampaign
from titan.intelligence.portfolio import region_for_country

#: Markets whose name carries no working week. ``UNSPECIFIED`` is "nobody has
#: said"; ``OTHER`` is "somewhere the schedule has no opinion about". Neither
#: can select a clock, and a carrier row for either would be a clock chosen by
#: accident.
UNROUTABLE: frozenset[Region] = frozenset({Region.UNSPECIFIED, Region.OTHER})


@dataclass(frozen=True, slots=True)
class CarrierRoute:
    """Where a message goes, and on whose authority."""

    #: The carrier campaign id, or None to let the provider use its default.
    campaign_id: str | None
    #: The market the id was chosen for, when it was chosen for one.
    region: Region | None
    #: Short, stable, and written into the decision trail.
    reason: str
    #: The recipient's own timezone, when a carrier was provisioned for it.
    timezone: str | None = None

    @property
    def routed_by_market(self) -> bool:
        return self.region is not None


def _fallback(campaign_carrier_id: int | None, reason: str) -> CarrierRoute:
    return CarrierRoute(
        campaign_id=(
            str(campaign_carrier_id) if campaign_carrier_id is not None else None
        ),
        region=None,
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class CarrierMap:
    """What this workspace can route to on one carrier.

    Two lookups, kept apart on purpose. Merging them into one dictionary keyed
    by "clock or market" would make the precedence between them an accident of
    how the key was built, and the precedence is the whole rule.
    """

    #: IANA timezone to carrier campaign, for carriers provisioned for a clock.
    by_clock: dict[str, str]
    #: Market to carrier campaign, for carriers provisioned for a whole market.
    by_market: dict[Region, str]

    @property
    def is_empty(self) -> bool:
        return not self.by_clock and not self.by_market


async def carriers_for_workspace(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    provider: str,
) -> CarrierMap:
    """Everything this workspace can route to on this carrier.

    Read as a whole rather than one lookup per message: a workspace has at most
    a handful of carrier campaigns, and the outbox drains in batches.
    """
    rows = (
        await session.execute(
            select(
                CarrierCampaign.region,
                CarrierCampaign.timezone,
                CarrierCampaign.carrier_campaign_id,
            ).where(
                CarrierCampaign.workspace_id == workspace_id,
                CarrierCampaign.provider == provider,
            )
        )
    ).all()
    by_clock: dict[str, str] = {}
    by_market: dict[Region, str] = {}
    for region, timezone, carrier_id in rows:
        if timezone:
            by_clock[timezone] = carrier_id
        else:
            by_market[region] = carrier_id
    return CarrierMap(by_clock=by_clock, by_market=by_market)


def route_for_market(
    *,
    carriers: CarrierMap,
    recipient_country_code: str | None,
    recipient_timezone: str | None = None,
    campaign_carrier_id: int | None,
) -> CarrierRoute:
    """Pick the carrier campaign for one message.

    ``recipient_country_code`` and ``recipient_timezone`` both come off the
    organisation's own location, which discovery records from Places -- every
    location Titan holds has a real IANA zone, stamped from the metro it was
    found in.

    The order is deliberate and is not a scoring function. The recipient's own
    clock wins, then their own market. Everything after that is a fallback that
    reproduces the behaviour this replaced -- never a substitute market.
    """
    if recipient_timezone:
        exact = carriers.by_clock.get(recipient_timezone)
        if exact is not None:
            return CarrierRoute(
                campaign_id=exact,
                region=region_for_country(recipient_country_code),
                timezone=recipient_timezone,
                reason=f"routed to the {recipient_timezone} carrier",
            )

    region = region_for_country(recipient_country_code)

    if region in UNROUTABLE:
        # No location, or a country the schedule has no working week for. The
        # campaign's own carrier is the honest answer: it is what this lead
        # would have used yesterday, and it is somebody's stated intent rather
        # than a market picked for it.
        return _fallback(
            campaign_carrier_id,
            f"recipient market {region.value}; using the campaign's carrier",
        )

    carrier_id = carriers.by_market.get(region)
    if carrier_id is not None:
        return CarrierRoute(
            campaign_id=carrier_id,
            region=region,
            reason=f"routed to the {region.value} carrier by recipient location",
        )

    # The market is known and has no carrier campaign. Deliberately *not*
    # substituting a neighbour: this lead's working day is known to differ from
    # every carrier available, so the campaign's own id -- or the provider
    # default -- is the only answer that is not an invention. Visible in the
    # reason, because the fix is to provision the market, not to reroute it.
    return _fallback(
        campaign_carrier_id,
        f"no carrier campaign provisioned for {region.value}",
    )


__all__ = [
    "UNROUTABLE",
    "CarrierMap",
    "CarrierRoute",
    "carriers_for_workspace",
    "route_for_market",
]
