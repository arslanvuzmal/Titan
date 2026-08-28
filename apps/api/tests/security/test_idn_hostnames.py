"""Internationalised domains must resolve, and must not smuggle anything in.

The guard's label pattern is ASCII-only, which is right -- A-labels are what
DNS carries. What was missing was the conversion *into* that form, so every
accented domain was refused outright as ``invalid_hostname``. Observed in the
worker log: ``strafverteidigungmünchen.de: invalid_hostname``. Titan sells into
Germany, Poland, the Netherlands and the Nordics, so this was not an edge case;
it was a standing refusal to look at a slice of the target market.

The half that needs holding down is the *ordering*. Blocklists, the metadata
host list and the suffix list all match ASCII. Screening a Unicode name and
encoding it afterwards would mean the check read one string and the connection
used another -- so a homograph whose punycode form is ``metadata.google.internal``
would pass the screen and then be resolved. Encoding happens first, and the
tests below pin that down from both directions.
"""

from __future__ import annotations

import pytest

from titan.security.url_guard import BlockReason, to_ascii_hostname, validate_url


def _resolver(addresses: list[str]):
    def resolve(host: str, port: int) -> list[str]:
        return addresses

    return resolve


PUBLIC = _resolver(["93.184.216.34"])


class TestEncoding:
    @pytest.mark.parametrize(
        "unicode_host, ascii_host",
        [
            ("strafverteidigungmünchen.de", "xn--strafverteidigungmnchen-tpc.de"),
            ("zahnärzte-münchen.de", "xn--zahnrzte-mnchen-3kb82b.de"),
            ("café.fr", "xn--caf-dma.fr"),
            ("møbler.no", "xn--mbler-vua.no"),
        ],
    )
    def test_accented_domains_become_a_labels(
        self, unicode_host: str, ascii_host: str
    ) -> None:
        assert to_ascii_hostname(unicode_host) == ascii_host

    def test_ascii_hosts_pass_through_untouched(self) -> None:
        """Not round-tripped: the codecs disagree with the regex at the edges."""
        for host in ("example.com", "sub.example.co.uk", "xn--caf-dma.fr"):
            assert to_ascii_hostname(host) is host

    def test_unencodable_hostnames_are_refused(self) -> None:
        """A label that cannot be represented as an A-label has no ASCII form."""
        assert to_ascii_hostname("ü" * 100 + ".de") is None


class TestTheGuardAcceptsThem:
    def test_an_idn_url_is_allowed_and_pinned_to_its_ascii_form(self) -> None:
        verdict = validate_url(
            "https://strafverteidigungmünchen.de/kontakt", resolver=PUBLIC
        )
        assert verdict.allowed
        # The caller pins its connection to this name; it must be the one that
        # was actually resolved, not the display form.
        assert verdict.hostname == "xn--strafverteidigungmnchen-tpc.de"

    def test_an_unencodable_host_is_refused_as_invalid(self) -> None:
        verdict = validate_url("https://" + "ü" * 100 + ".de/", resolver=PUBLIC)
        assert not verdict.allowed
        assert verdict.reason is BlockReason.INVALID_HOSTNAME


class TestOrderingIsNotExploitable:
    """Encoding first is what makes the ASCII blocklists still mean something."""

    def test_a_unicode_spelling_of_a_metadata_host_is_still_blocked(self) -> None:
        """Fullwidth characters map to ASCII under UTS-46.

        ``ｍｅｔａｄａｔａ.google.internal`` is not the same string as
        ``metadata.google.internal``, so a blocklist consulted before encoding
        would wave it through and the resolver would then be handed the real
        name.
        """
        verdict = validate_url(
            "https://ｍｅｔａｄａｔａ.google.internal/", resolver=PUBLIC
        )
        assert not verdict.allowed
        assert verdict.reason in {
            BlockReason.METADATA_HOST,
            BlockReason.BLOCKED_SUFFIX,
        }

    def test_a_unicode_spelling_of_localhost_is_still_blocked(self) -> None:
        verdict = validate_url("https://ｌｏｃａｌｈｏｓｔ/", resolver=PUBLIC)
        assert not verdict.allowed
        assert verdict.reason is BlockReason.BLOCKED_HOSTNAME

    def test_an_idn_resolving_privately_is_still_refused(self) -> None:
        """Encoding buys reach, not trust. The address checks still run."""
        verdict = validate_url(
            "https://münchen.de/", resolver=_resolver(["127.0.0.1"])
        )
        assert not verdict.allowed
        assert verdict.reason is BlockReason.PRIVATE_ADDRESS
