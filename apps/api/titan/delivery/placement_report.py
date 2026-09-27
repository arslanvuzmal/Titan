"""One screen that answers: is our mail being seen, and by whom is it not?

The rows underneath this are counts. A count is not an answer, and the gap
between the two is where two months went: 944 messages, a table that could
have said "nobody has looked", and nobody looking.

So this renders three things and refuses to render a fourth:

* **Per mailbox and provider**, because they differ. Five addresses on one
  domain, and on 27 September the one that reached an inbox was the least-used
  of them. A domain average would have called that domain half-working and
  left the actionable question -- which address to stop sending from --
  unanswerable.
* **Unchecked counted separately from bad.** A probe nobody looked at is not
  evidence of anything, and folding it into a denominator turns silence into
  a reassuring percentage.
* **The trend, not the latest reading.** Reputation moves over weeks. One
  round is noise; the direction is the finding.

What it will not render is a single headline number for the estate. There is
no honest one: "62% inbox" across two providers and five mailboxes describes
nothing anybody can act on, and the only thing it is good for is feeling
informed.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Folders that mean the recipient plausibly saw it. Promotions is excluded
#: deliberately and is the most consequential line in this module: a message
#: in Gmail's Promotions tab is delivered, unread, and counted as success by
#: every tool that treats "not spam" as "inbox".
SEEN = frozenset({"inbox"})


@dataclass(frozen=True, slots=True)
class Placement:
    """How one mailbox is doing at one provider."""

    from_email: str
    provider: str
    probes: int
    inbox: int
    promotions: int
    spam: int
    missing: int
    unchecked: int

    @property
    def measured(self) -> int:
        """Probes somebody actually looked at. The only honest denominator."""
        return self.probes - self.unchecked

    @property
    def reach(self) -> float | None:
        """Share that reached the inbox, or None when nothing was measured.

        None rather than zero. Zero is a finding -- every probe was junked --
        and "we have not looked" must never render as that.
        """
        if self.measured <= 0:
            return None
        return self.inbox / self.measured

    @property
    def verdict(self) -> str:
        reach = self.reach
        if reach is None:
            return "not measured"
        if reach >= 0.9:
            return "landing"
        if reach >= 0.5:
            return "patchy"
        if self.spam > 0:
            return "filtered"
        return "not landing"


def placements(rows: list[dict[str, object]]) -> list[Placement]:
    """Rows from :func:`titan.delivery.placement.by_mailbox`, typed."""
    return [
        Placement(
            from_email=str(r["from_email"]),
            provider=str(r["provider"]),
            probes=int(r["probes"] or 0),
            inbox=int(r["inbox"] or 0),
            promotions=int(r["promotions"] or 0),
            spam=int(r["spam"] or 0),
            missing=int(r["missing"] or 0),
            unchecked=int(r["unchecked"] or 0),
        )
        for r in rows
    ]


def worst_first(items: list[Placement]) -> list[Placement]:
    """Ordered so the thing to act on is the first thing read.

    Unmeasured pairings sort to the top, above even total failure. A mailbox
    nobody has probed is a worse position than a mailbox known to be in spam:
    one of them you can decide about.
    """

    def key(p: Placement) -> tuple[int, float, str, str]:
        unmeasured = 0 if p.reach is None else 1
        return (
            unmeasured,
            p.reach if p.reach is not None else 0.0,
            p.from_email,
            p.provider,
        )

    return sorted(items, key=key)


def render(items: list[Placement], *, days: int) -> str:
    """The report, as plain text for a terminal or an email body."""
    if not items:
        return (
            f"No probes in the last {days} days.\n"
            "Nothing here is evidence that placement is fine -- it is evidence "
            "that nothing has been measured. Check the probe schedule is running."
        )

    lines = [
        f"Inbox placement, last {days} days",
        "",
        f"  {'mailbox':<34}{'provider':<10}{'seen':>6}{'promo':>7}{'spam':>6}{'lost':>6}{'?':>4}  verdict",
    ]
    for p in worst_first(items):
        reach = "  --  " if p.reach is None else f"{p.reach * 100:5.0f}%"
        lines.append(
            f"  {p.from_email:<34}{p.provider:<10}{reach:>6}"
            f"{p.promotions:>7}{p.spam:>6}{p.missing:>6}{p.unchecked:>4}  {p.verdict}"
        )

    unchecked = sum(p.unchecked for p in items)
    if unchecked:
        lines += [
            "",
            f"  {unchecked} probe(s) sent and never looked at. Those are not "
            f"counted above:",
            "  a probe nobody checked is silence, not a result.",
        ]

    promoted = sum(p.promotions for p in items)
    if promoted:
        lines += [
            "",
            f"  {promoted} landed in Gmail's Promotions tab. Delivered, not in spam,",
            "  and in the folder cold outreach goes to be ignored. Not counted as seen.",
        ]

    return "\n".join(lines)


__all__ = ["SEEN", "Placement", "placements", "render", "worst_first"]
