"""Comparing two written forms of the same telephone number.

The estate stores numbers as Google Places hands them over, with the formatting
stripped: ``01611234567``, ``7868128622``. None of the 4,851 numbers on the
estate is in E.164, despite the column being named ``phone_e164`` -- stripping
punctuation does not add a country code, and ``nationalPhoneNumber`` never had
one.

That is survivable for a CRM and fatal for a do-not-call list. A suppression is
written with whatever the caller passed, and anything that dials will pass
``+441611234567`` because diallers require E.164. The candidate query compares
with ``=``. So the one instruction that must take effect before the next dial
-- *do not ring me again* -- silently matches nothing and the number stays in
rotation.

No calls have been made yet, so nothing has gone wrong. It would have gone
wrong on the first refusal.

**Why a suffix and not a conversion.** Turning ``01611234567`` into
``+441611234567`` needs to know the country and its trunk-prefix rule, and the
rules genuinely differ across the nine countries in this estate: Britain,
Ireland, Australia, Romania and the Emirates drop a leading zero, Poland has no
trunk prefix to drop, and North America has none either. Getting that wrong
produces a number that is not merely unusable but *different*, and somebody
eventually rings it. That conversion needs a real library and belongs with the
dialler. Matching a suppression does not need it.
"""

from __future__ import annotations

#: How much of the tail two numbers must share to be treated as the same line.
#:
#: Nine is the length of the national significant number across the estate's
#: countries once the country code and any trunk prefix are gone -- Manchester
#: ``161 123 4567`` is nine after the area code's leading digit, a North
#: American number is ten and its last nine are unique within an area code.
#:
#: Short enough that every written form of one number agrees, long enough that
#: two different numbers colliding inside a single workspace is remote. And the
#: cost of a collision is asymmetric on purpose: a false match means not ringing
#: somebody, which is the harmless direction for a list of people who asked not
#: to be rung.
SIGNIFICANT_DIGITS = 9


def dial_key(raw: str | None) -> str | None:
    """The part of a number that identifies the line, whatever the format.

    ``+44 161 123 4567``, ``0161 123 4567`` and ``(0161) 123-4567`` all return
    ``611234567``. Returns ``None`` for anything with too few digits to be a
    telephone number, so an empty field never matches an empty field.
    """
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < SIGNIFICANT_DIGITS:
        return None
    return digits[-SIGNIFICANT_DIGITS:]


#: The widest value ``organizations.phone_e164`` can hold.
COLUMN_LIMIT = 20


def strip_formatting(raw: str | None) -> str | None:
    """Digits and a leading ``+``, or nothing.

    What both write paths should have been doing and only one was.
    ``discover_leads`` normalised; ``python -m coldops.seed`` wrote the Places
    string verbatim, which is why the estate holds ``0161 912 6200`` with the
    spaces still in it.

    Anything still over the column width after stripping is dropped rather than
    trimmed. A truncated telephone number is not a shorter telephone number, it
    is a different one, and somebody eventually rings it. An over-long value
    also used to raise ``StringDataRightTruncation`` inside the discovery unit
    of work and fail the whole batch, losing every business in that search.

    This does **not** produce E.164; see the module docstring. It produces a
    stable written form, which is what :func:`dial_key` and the CRM need.
    """
    if not raw:
        return None
    cleaned = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    if not cleaned or cleaned == "+":
        return None
    return cleaned if len(cleaned) <= COLUMN_LIMIT else None


def same_line(left: str | None, right: str | None) -> bool:
    """Whether two written numbers are the same line.

    ``None`` never matches, including ``None`` against ``None``: two missing
    numbers are not the same number.
    """
    a = dial_key(left)
    return a is not None and a == dial_key(right)


#: The same rule in SQL, for the candidate query, which has to apply it across
#: a join rather than a pair of Python strings.
#:
#: Kept beside :func:`dial_key` so the two cannot drift. Unindexed, and that is
#: fine at the size of this estate -- 4,851 organisations against a suppression
#: list in the dozens. If either grows by two orders of magnitude this wants a
#: stored column and an index, not a cleverer expression.
DIAL_KEY_SQL = "right(regexp_replace({col}, '[^0-9]', '', 'g'), 9)"


def dial_key_sql(column: str) -> str:
    """``DIAL_KEY_SQL`` for one column, e.g. ``o.phone_e164``."""
    return DIAL_KEY_SQL.format(col=column)


__all__ = [
    "DIAL_KEY_SQL",
    "SIGNIFICANT_DIGITS",
    "dial_key",
    "dial_key_sql",
    "same_line",
]
