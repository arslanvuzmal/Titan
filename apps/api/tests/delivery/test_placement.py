"""Placement is measured, not inferred.

944 messages produced no reply and nothing in the estate could say whether the
offer was wrong or the mail unread. A hand-run seed test settled it in four
minutes -- spam at Gmail, junk at Outlook, three of three -- which is two
months later than it needed to be.

No provider reports the folder it filed a stranger's mail into. A "delivered"
webhook means the receiving server accepted it, which is equally true of a
message dropped straight into junk. Probing a mailbox you own is the only
instrument there is.
"""

from __future__ import annotations

import pytest
from titan.delivery import placement


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("someone@gmail.com", "gmail"),
        ("someone@googlemail.com", "gmail"),
        ("someone@outlook.com", "outlook"),
        ("someone@hotmail.com", "outlook"),
        ("someone@live.com", "outlook"),
        ("someone@yahoo.co.uk", "yahoo"),
        ("hello@somedentalpractice.co.uk", "other"),
    ],
)
def test_addresses_group_by_the_filter_that_judges_them(
    address: str, expected: str
) -> None:
    """Grouped by filter, not brand: hotmail and outlook share an engine."""
    assert placement.provider_of(address) == expected


def test_a_probe_token_is_unpredictable_and_unique() -> None:
    """Two rounds in one day must not collide, and a guessable token in a
    subject line is the sort of pattern a filter learns."""
    tokens = {placement.new_probe_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(t.startswith("tp-") for t in tokens)


def test_missing_is_a_result_and_unknown_is_not() -> None:
    """The distinction the whole table exists for.

    `missing` means it was accepted and then filed nowhere the recipient will
    look -- a real, bad answer. `unknown` means nobody has checked. A schema
    that collapses them cannot tell an unmeasured probe from a lost one, which
    is the exact confusion that let 944 messages go out unexamined.
    """
    assert "missing" in placement.FOLDERS
    assert "unknown" in placement.FOLDERS
    assert "missing" not in placement.REACHED
    assert "unknown" not in placement.REACHED


def test_only_the_inbox_counts_as_reaching_someone() -> None:
    """Promotions is delivery, not readership.

    Cold outreach in Gmail's Promotions tab is not opened. Counting it as
    success would report a working channel while the campaign produced
    nothing, which is the failure mode this module exists to prevent.
    """
    assert placement.REACHED == {"inbox"}
    assert "promotions" in placement.FOLDERS


@pytest.mark.asyncio
async def test_an_unknown_folder_is_refused_rather_than_stored() -> None:
    """A typo must not become a data point.

    Nothing else validates this column, and a series is only readable if every
    reading uses the same vocabulary.
    """
    with pytest.raises(ValueError, match="folder must be one of"):
        await placement.record_result(
            session=None,  # type: ignore[arg-type]
            workspace_id=None,  # type: ignore[arg-type]
            probe_token="tp-deadbeef",
            seed_address="a@gmail.com",
            folder="INBOX",  # right idea, wrong vocabulary
            checked_by="manual",
        )
