"""The pixel, its signature, and the reason it is switched off.

Two of these guard a number rather than a behaviour. An open count is the kind
of figure people quote in meetings, and there are exactly two ways to make it
permanently meaningless: let anyone forge one, or let a proxy's second fetch
overwrite the first. Both are cheap to prevent and impossible to undo.
"""

from __future__ import annotations

import uuid

from titan.config import Settings
from titan.delivery.open_tracking import (
    PIXEL,
    PIXEL_CONTENT_TYPE,
    open_token,
    pixel_html,
    verify_open_token,
)

SECRET = "not-the-real-one"
MESSAGE = uuid.uuid4()


class TestItIsOffUntilSomebodyDecidesOtherwise:
    def test_open_tracking_is_disabled_by_default(self) -> None:
        """A pixel is a spam signal, and this domain reached one inbox on
        27 September after six readings of spam. Defaulting this on would
        spend that on a number Apple pre-fetches anyway."""
        assert Settings().open_tracking_enabled is False

    def test_the_setting_records_the_condition_for_turning_it_on(self) -> None:
        """Written down so the decision is not left to whoever is next in the
        file, which is how the rewriter flag ended up true on a false belief."""
        import pathlib

        source = pathlib.Path("titan/config.py").read_text(encoding="utf-8")
        window = source.split("open_tracking_enabled")[0][-1400:]

        assert "placement holding at inbox for a week" in window


class TestTheTokenCannotBeForged:
    def test_a_token_round_trips(self) -> None:
        assert verify_open_token(open_token(MESSAGE, SECRET), SECRET) == MESSAGE

    def test_a_raw_message_id_is_refused(self) -> None:
        """A bare id in an image URL is an invitation to walk the range and
        mark every message in the estate as opened."""
        assert verify_open_token(str(MESSAGE), SECRET) is None

    def test_a_token_signed_with_another_secret_is_refused(self) -> None:
        assert verify_open_token(open_token(MESSAGE, "other"), SECRET) is None

    def test_a_tampered_signature_is_refused(self) -> None:
        good = open_token(MESSAGE, SECRET)
        tampered = good[:-1] + ("0" if good[-1] != "0" else "1")

        assert verify_open_token(tampered, SECRET) is None

    def test_a_tampered_message_id_is_refused(self) -> None:
        """Keeping the signature and swapping the id is the obvious attack."""
        _, _, signature = open_token(MESSAGE, SECRET).partition(".")

        assert verify_open_token(f"{uuid.uuid4()}.{signature}", SECRET) is None

    def test_rubbish_is_refused_rather_than_raising(self) -> None:
        """The endpoint serves a pixel either way; it must not 500 on a crawler."""
        for junk in ("", ".", "not-a-uuid.abcdef", "....", "x"):
            assert verify_open_token(junk, SECRET) is None

    def test_two_messages_do_not_share_a_token(self) -> None:
        assert open_token(uuid.uuid4(), SECRET) != open_token(uuid.uuid4(), SECRET)


class TestTheTagItself:
    def test_the_pixel_is_a_real_transparent_gif(self) -> None:
        assert PIXEL.startswith(b"GIF89a")
        assert PIXEL_CONTENT_TYPE == "image/gif"
        assert len(PIXEL) < 100

    def test_the_tag_carries_the_signed_token_not_the_id(self) -> None:
        tag = pixel_html("https://titan.example", MESSAGE, SECRET)

        assert open_token(MESSAGE, SECRET) in tag

    def test_the_tag_is_hidden_from_a_screen_reader(self) -> None:
        """A blind reader should not have to listen to an untitled image
        announced in the middle of the message."""
        tag = pixel_html("https://titan.example", MESSAGE, SECRET)

        assert 'alt=""' in tag
        assert 'aria-hidden="true"' in tag

    def test_the_size_is_set_as_attributes_as_well_as_style(self) -> None:
        """Several clients drop inline CSS, and a pixel that renders at its
        natural size is a visible broken-image box in the middle of the copy."""
        tag = pixel_html("https://titan.example", MESSAGE, SECRET)

        assert 'width="1"' in tag and 'height="1"' in tag

    def test_a_trailing_slash_on_the_base_url_does_not_double(self) -> None:
        with_slash = pixel_html("https://titan.example/", MESSAGE, SECRET)

        assert "//o/" not in with_slash
