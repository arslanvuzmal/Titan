"""Which arriving mail is worth telling the operator about.

The estate polls a mailbox, and a mailbox receives whatever the internet sends
it. Of the 676 messages taken in by 27 September, 629 matched no send of ours:
delivery reports from ``bounces.jellyfish.systems`` about subjects like
"Productivity Tips", auto-replies from 455 addresses nobody here has written to.
Backscatter from a forged envelope sender, most likely, and it grows every week.

Eighteen of the twenty-four open "needs a read" alerts were raised by that
traffic. The six real ones were underneath, and nobody reached them.

These are pure tests on the rule itself, with no database, because the rule is
the part that was wrong.
"""

from __future__ import annotations

from coldops.delivery.inbound import alerts_the_operator
from coldops.intelligence.replies import ReplyKind


def test_a_reply_to_something_we_sent_alerts():
    assert alerts_the_operator(ReplyKind.HUMAN, attributed=True)


def test_a_human_message_matching_no_send_of_ours_does_not_alert():
    """The eighteen. A stranger's mail is not a reply to anything."""
    assert not alerts_the_operator(ReplyKind.HUMAN, attributed=False)


def test_a_complaint_alerts_even_unattributed():
    """Rare, serious, and names a sending domain.

    Being wrong about whose mail it was costs an alert. Missing one costs the
    domain.
    """
    assert alerts_the_operator(ReplyKind.COMPLAINT, attributed=False)
    assert alerts_the_operator(ReplyKind.COMPLAINT, attributed=True)


def test_the_machine_classes_stay_silent_either_way():
    """Unchanged behaviour, asserted so a later edit cannot quietly restore it.

    A bounce and an out-of-office are handled completely without anybody reading
    them, and an alert apiece is how a channel becomes noise.
    """
    for kind in (
        ReplyKind.BOUNCE,
        ReplyKind.AUTO,
        ReplyKind.UNSUBSCRIBE,
        ReplyKind.NOT_A_PROSPECT,
    ):
        assert not alerts_the_operator(kind, attributed=True)
        assert not alerts_the_operator(kind, attributed=False)
