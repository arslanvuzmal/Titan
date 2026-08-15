"""The set of campaigns, seen as one thing.

Campaigns already carry a target country and their own policy, and each one can
be inspected on its own. What has never existed is the layer above: a way to ask
which markets Titan is actually working, how much of the week's sending each
took, and which of them is worth more of it. That question was answered by
holding six campaign pages open at once.

**A slice is a region, not a campaign.** Campaigns multiply -- one per industry
per city is normal -- and a list of forty is not a portfolio, it is the same
problem in a longer form. The region is the level at which the answers actually
differ, and the level at which an operator can do something about them.

**Share of sending is reported, not share of leads.** Sending capacity is the
scarce resource: mailbox volume is finite, warm-up bounds it, and every message
one region sends is one another did not. Leads are cheap to discover and mean
very little until somebody is written to.

**Nothing here reallocates anything.** Reading which region earns its volume is
the input to that decision; making it is the campaign manager's, and a reporting
module that quietly moved capacity would be an autonomous change nobody asked
for and nobody could see.
"""

from __future__ import annotations

from dataclasses import dataclass

from titan.db.enums import Region

#: ISO 3166-1 alpha-2 to market. Not exhaustive and not meant to be: it covers
#: the countries Titan's campaigns actually name, and anything absent resolves
#: to OTHER rather than guessing. A wrong region is worse than an unset one --
#: it schedules a business day in the wrong hemisphere.
_COUNTRY_TO_REGION: dict[str, Region] = {
    "US": Region.USA,
    "CA": Region.CANADA,
    "GB": Region.UK,
    "UK": Region.UK,  # not ISO, and written by humans often enough to accept
    "IE": Region.EUROPE,
    "DE": Region.EUROPE,
    "FR": Region.EUROPE,
    "ES": Region.EUROPE,
    "PT": Region.EUROPE,
    "IT": Region.EUROPE,
    "NL": Region.EUROPE,
    "BE": Region.EUROPE,
    "LU": Region.EUROPE,
    "AT": Region.EUROPE,
    "CH": Region.EUROPE,
    "DK": Region.EUROPE,
    "SE": Region.EUROPE,
    "NO": Region.EUROPE,
    "FI": Region.EUROPE,
    "PL": Region.EUROPE,
    "CZ": Region.EUROPE,
    "GR": Region.EUROPE,
    "RO": Region.EUROPE,
    "HU": Region.EUROPE,
    "AU": Region.AUSTRALIA,
    "NZ": Region.AUSTRALIA,
    "AE": Region.MIDDLE_EAST,
    "SA": Region.MIDDLE_EAST,
    "QA": Region.MIDDLE_EAST,
    "KW": Region.MIDDLE_EAST,
    "BH": Region.MIDDLE_EAST,
    "OM": Region.MIDDLE_EAST,
    "JO": Region.MIDDLE_EAST,
    "IL": Region.MIDDLE_EAST,
}


def region_for_country(code: str | None) -> Region:
    """The market a country belongs to.

    An unset code gives UNSPECIFIED and an unrecognised one gives OTHER. Those
    are different facts: the first is a campaign nobody has told us about, the
    second is one aimed somewhere the schedule has no opinion about.
    """
    if not code or not code.strip():
        return Region.UNSPECIFIED
    return _COUNTRY_TO_REGION.get(code.strip().upper(), Region.OTHER)


def disagrees_with_country(region: Region, code: str | None) -> bool:
    """Whether a declared region contradicts the campaign's own country code.

    Not an error and not corrected automatically. A campaign can legitimately
    declare EUROPE while naming Germany as its first country, and silently
    rewriting either one would destroy a deliberate choice. It is surfaced so a
    genuine mistake -- USA on a campaign targeting GB -- is visible.
    """
    if region in (Region.UNSPECIFIED, Region.OTHER):
        return False
    derived = region_for_country(code)
    if derived in (Region.UNSPECIFIED, Region.OTHER):
        return False
    if region is Region.EUROPE and derived is Region.EUROPE:
        return False
    return derived is not region


@dataclass(frozen=True, slots=True)
class RegionSlice:
    """One market's share of the portfolio."""

    region: Region
    campaigns: int = 0
    active_campaigns: int = 0
    leads: int = 0
    contacted: int = 0
    sent: int = 0
    bounced: int = 0
    replied: int = 0

    @property
    def bounce_rate(self) -> float:
        return self.bounced / self.sent if self.sent else 0.0

    @property
    def reply_rate(self) -> float:
        return self.replied / self.contacted if self.contacted else 0.0

    @property
    def is_working(self) -> bool:
        """Whether this market is doing anything at all.

        A region with active campaigns and no sends is not working -- it is
        configured. The distinction is the whole value of the view: that is
        exactly the state nobody notices, because the campaign page looks fine.
        """
        return self.sent > 0


@dataclass(frozen=True, slots=True)
class Portfolio:
    slices: tuple[RegionSlice, ...] = ()

    @property
    def sent(self) -> int:
        return sum(s.sent for s in self.slices)

    @property
    def working(self) -> tuple[RegionSlice, ...]:
        return tuple(s for s in self.slices if s.is_working)

    @property
    def idle(self) -> tuple[RegionSlice, ...]:
        """Configured markets that sent nothing. The point of the whole view."""
        return tuple(
            s for s in self.slices if not s.is_working and s.active_campaigns > 0
        )

    def share_of_sending(self, region: Region) -> float:
        total = self.sent
        if not total:
            return 0.0
        return sum(s.sent for s in self.slices if s.region is region) / total


def summarise(slices: list[RegionSlice]) -> Portfolio:
    """Order the markets by how much of the week's sending each took.

    Busiest first, because share of capacity is the question the view exists to
    answer, and a market doing nothing is best read at the bottom next to the
    other markets doing nothing.
    """
    return Portfolio(
        slices=tuple(sorted(slices, key=lambda s: (-s.sent, -s.leads, s.region.value)))
    )


def describe(slice_: RegionSlice, portfolio: Portfolio) -> str:
    """One line per market, for the weekly report."""
    if not slice_.is_working:
        if slice_.active_campaigns:
            return (
                f"{slice_.active_campaigns} active campaign(s), nothing sent -- "
                f"{slice_.leads} lead(s) discovered"
            )
        return f"{slice_.campaigns} campaign(s), none active"

    share = portfolio.share_of_sending(slice_.region)
    parts = [
        f"{share:.0%} of sending",
        f"{slice_.sent} sent",
        f"{slice_.bounce_rate:.0%} bounced",
        f"{slice_.replied} repl(ies)",
    ]
    return ", ".join(parts)


__all__ = [
    "Portfolio",
    "RegionSlice",
    "describe",
    "disagrees_with_country",
    "region_for_country",
    "summarise",
]
