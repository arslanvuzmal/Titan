"""What a recipient's domain says about the risk of writing to it.

Everything here is local, deterministic and free: no DNS, no network, no
purchased service. That is the point. The expensive checks -- MX in
:mod:`titan.intelligence.mx`, a verification service behind
:mod:`titan.intelligence.verifier` -- are worth paying for only after the
answers you can get for nothing have been taken.

Three questions, each catching a failure the others cannot:

* **Is this a throwaway inbox?** Disposable addresses are read by nobody and
  frequently expire between discovery and send. MX cannot see this: these
  domains have perfectly good mail servers.
* **Is this a misspelling of a large provider?** ``gmial.com`` is the one that
  matters, and it is the one MX is worst at. An unregistered typo domain fails
  the MX check harmlessly; a *squatted* one resolves, publishes MX, accepts the
  message and hands it to whoever registered it. The domains that pass every
  other check are exactly the dangerous ones.
* **Is this a free consumer mailbox?** Not a defect -- a great many small
  businesses run on one, and refusing them would discard the population Titan
  targets. It is recorded because it changes what other signals mean, and
  because it is the class of domain where a mailbox-level probe is useless.

**On completeness.** The disposable list below is curated, not exhaustive;
there are thousands of these domains and they turn over constantly. A curated
list of the common families catches the overwhelming majority of what a crawler
actually finds, and being incomplete is safe in the right direction: a missed
disposable domain is one ordinary bounce, while a false positive silently
discards a real lead. If this ever needs to be exhaustive, it should be loaded
from a maintained dataset rather than grown by hand here.
"""

from __future__ import annotations

#: Throwaway mailbox providers. Grouped by family, because most of these
#: operate dozens of interchangeable domains and recognising the family is what
#: makes a hand-maintained list worth keeping.
DISPOSABLE_DOMAINS: frozenset[str] = frozenset(
    {
        # Mailinator and friends
        "mailinator.com",
        "mailinator.net",
        "mailinator2.com",
        "sogetthis.com",
        "notmailinator.com",
        # Guerrilla Mail
        "guerrillamail.com",
        "guerrillamail.net",
        "guerrillamail.org",
        "guerrillamail.biz",
        "guerrillamail.de",
        "grr.la",
        "sharklasers.com",
        "spam4.me",
        # 10 Minute Mail family
        "10minutemail.com",
        "10minutemail.net",
        "20minutemail.com",
        "tempmail.com",
        "temp-mail.org",
        "temp-mail.io",
        "tempmailo.com",
        "tempr.email",
        "throwawaymail.com",
        "trashmail.com",
        "trashmail.de",
        "trashmail.net",
        "wegwerfmail.de",
        # Yopmail
        "yopmail.com",
        "yopmail.net",
        "yopmail.fr",
        "cool.fr.nf",
        "jetable.fr.nf",
        # Getnada / Inboxkitten / others
        "getnada.com",
        "nada.email",
        "inboxkitten.com",
        "emailondeck.com",
        "fakeinbox.com",
        "fakemailgenerator.com",
        "dispostable.com",
        "mintemail.com",
        "mytrashmail.com",
        "spamgourmet.com",
        "mailnesia.com",
        "maildrop.cc",
        "moakt.com",
        "tmpmail.org",
        "tmpeml.com",
        "burnermail.io",
        "mohmal.com",
        "linshiyouxiang.net",
        "harakirimail.com",
        "anonaddy.me",
        "mailsac.com",
        "byom.de",
        "einrot.com",
        "armyspy.com",
        "cuvox.de",
        "dayrep.com",
        "fleckens.hu",
        "gustr.com",
        "jourrapide.com",
        "rhyta.com",
        "superrito.com",
        "teleworm.us",
    }
)

#: Consumer mailbox providers. Present so that "this is a personal address, not
#: a company one" is a fact the engine can reason about rather than infer.
#:
#: Also the accept-everything set: these providers answer RCPT TO for addresses
#: that do not exist and bounce afterwards, which is half of why Titan does not
#: run its own mailbox probe (see :mod:`titan.intelligence.mx`).
FREE_MAILBOX_PROVIDERS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "outlook.co.uk",
        "hotmail.com",
        "hotmail.co.uk",
        "hotmail.fr",
        "live.com",
        "live.co.uk",
        "msn.com",
        "yahoo.com",
        "yahoo.co.uk",
        "yahoo.fr",
        "yahoo.de",
        "ymail.com",
        "rocketmail.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "protonmail.com",
        "proton.me",
        "pm.me",
        "gmx.com",
        "gmx.de",
        "gmx.net",
        "web.de",
        "mail.com",
        "mail.ru",
        "yandex.com",
        "yandex.ru",
        "zoho.com",
        "fastmail.com",
        "tutanota.com",
        "tuta.io",
        "hushmail.com",
        "comcast.net",
        "verizon.net",
        "att.net",
        "sbcglobal.net",
        "bellsouth.net",
        "cox.net",
        "btinternet.com",
        "sky.com",
        "virginmedia.com",
        "talktalk.net",
        "orange.fr",
        "wanadoo.fr",
        "free.fr",
        "libero.it",
        "bigpond.com",
        "optusnet.com.au",
        "shaw.ca",
        "rogers.com",
        "telus.net",
    }
)

#: Minimum length of the second-level label before a provider is used as a
#: reference for typo detection.
#:
#: Not arbitrary caution -- it is the difference between working and unusable.
#: ``gmx.com`` is one deletion from ``gm.com``, which is General Motors, and
#: ``aol.com`` is one deletion from ``al.com``. Short labels sit inside every
#: other short label's edit neighbourhood, so including them would flag real
#: corporate domains as misspellings of a webmail provider. Five characters is
#: where the neighbourhoods stop overlapping with anything a business would own.
MIN_TYPO_REFERENCE_LENGTH = 5

#: The domains a typo is measured against: consumer providers whose second-level
#: label is long enough to be distinctive. Sorted so the result is stable when
#: a domain sits one edit from two references.
TYPO_REFERENCE_DOMAINS: tuple[str, ...] = tuple(
    sorted(
        d
        for d in FREE_MAILBOX_PROVIDERS
        if len(d.split(".", 1)[0]) >= MIN_TYPO_REFERENCE_LENGTH
    )
)


def normalize_domain(domain: str) -> str:
    return (domain or "").strip().lower().rstrip(".").removeprefix("www.")


def is_disposable(domain: str) -> bool:
    """Whether the domain is a known throwaway mailbox provider."""
    return normalize_domain(domain) in DISPOSABLE_DOMAINS


def is_free_mailbox_provider(domain: str) -> bool:
    """Whether the domain is a consumer mailbox provider rather than a business."""
    return normalize_domain(domain) in FREE_MAILBOX_PROVIDERS


def typo_of(domain: str) -> str | None:
    """The provider this domain appears to be a misspelling of, if any.

    Returns None for a domain that *is* a known provider: ``mail.com`` is one
    insertion from ``gmail.com`` and is also a real mailbox provider serving
    real people, so membership is checked before distance.
    """
    candidate = normalize_domain(domain)
    if not candidate or candidate in FREE_MAILBOX_PROVIDERS:
        return None
    for reference in TYPO_REFERENCE_DOMAINS:
        if is_one_edit_apart(candidate, reference):
            return reference
    return None


def is_one_edit_apart(a: str, b: str) -> bool:
    """Whether two strings differ by exactly one insertion, deletion,
    substitution or adjacent transposition.

    Written as a direct test rather than a Damerau-Levenshtein matrix because
    the threshold is fixed at one. At that threshold the two agree exactly, and
    a direct test is linear, allocation-free, and legible enough to check by
    reading -- which matters for a rule whose false positives silently discard
    real leads.
    """
    if a == b:
        return False

    len_a, len_b = len(a), len(b)
    if abs(len_a - len_b) > 1:
        return False

    if len_a == len_b:
        diffs = [i for i in range(len_a) if a[i] != b[i]]
        if len(diffs) == 1:
            return True  # substitution
        if len(diffs) == 2:
            i, j = diffs
            # Adjacent transposition: "gmial" against "gmail".
            return j == i + 1 and a[i] == b[j] and a[j] == b[i]
        return False

    # One string is exactly one character longer: it is an insertion into the
    # shorter one if skipping a single character from the longer makes them
    # equal.
    shorter, longer = (a, b) if len_a < len_b else (b, a)
    i = 0
    while i < len(shorter) and shorter[i] == longer[i]:
        i += 1
    return shorter[i:] == longer[i + 1 :]


__all__ = [
    "DISPOSABLE_DOMAINS",
    "FREE_MAILBOX_PROVIDERS",
    "MIN_TYPO_REFERENCE_LENGTH",
    "TYPO_REFERENCE_DOMAINS",
    "is_disposable",
    "is_free_mailbox_provider",
    "is_one_edit_apart",
    "normalize_domain",
    "typo_of",
]
