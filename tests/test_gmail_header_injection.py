"""A draft's `to` and `subject` may not smuggle extra headers.

Both are joined with CRLF into a MIME message. A value carrying its own CRLF
ends that header and starts another — so a crafted `subject` adds `Bcc:` and the
draft quietly copies a third party, or ends the header block early and writes the
body itself. The values arrive as tool arguments, filled by a model from whatever
it was asked to send.
"""

import base64
from typing import Any

import pytest

from wactorz.core.integrations.gmail_mcp import _header_safe


def _mime(to: str, subject: str) -> str:
    """The header block exactly as `_rest_create_draft` builds it."""
    lines = [f"To: {_header_safe(to)}", f"Subject: {_header_safe(subject)}"]
    return "\r\n".join(lines) + "\r\n\r\n" + "body text"


class TestHeaderSafe:
    @pytest.mark.parametrize(
        ("hostile", "kept"),
        [
            ("a@b.com\r\nBcc: attacker@evil.com", "a@b.com"),
            ("a@b.com\nBcc: attacker@evil.com", "a@b.com"),
            ("a@b.com\rBcc: attacker@evil.com", "a@b.com"),
            ("Subject\r\n\r\nInjected body", "Subject"),
            ("\r\nBcc: first@evil.com", ""),
        ],
    )
    def test_everything_past_the_first_break_is_dropped(self, hostile: str, kept: str) -> None:
        assert _header_safe(hostile) == kept

    @pytest.mark.parametrize(
        "ordinary",
        ["Quarterly report", "user@example.com", "Re: your message — thanks!", "a" * 300],
    )
    def test_an_ordinary_value_is_untouched(self, ordinary: str) -> None:
        assert _header_safe(ordinary) == ordinary

    @pytest.mark.parametrize("value", [None, "", 42, ["a@b.com"]])
    def test_it_never_raises_on_odd_input(self, value: Any) -> None:
        _header_safe(value)  # a draft is not worth a traceback


class TestTheAssembledMessage:
    def test_no_extra_header_reaches_the_block(self) -> None:
        mime = _mime("a@b.com\r\nBcc: attacker@evil.com", "Hello")

        header_block = mime.split("\r\n\r\n", 1)[0]
        assert "Bcc" not in header_block
        assert header_block.count("\r\n") == 1  # exactly To: and Subject:

    def test_the_body_cannot_be_started_early(self) -> None:
        mime = _mime("a@b.com", "Hi\r\n\r\nInjected body that is not the body")

        assert mime.split("\r\n\r\n", 1)[1] == "body text"

    def test_it_still_encodes_for_the_api(self) -> None:
        # The caller base64s this; a mangled header block would surface there.
        mime = _mime("a@b.com", "Quarterly report")
        raw = base64.urlsafe_b64encode(mime.encode("utf-8")).decode("ascii")

        assert "To: a@b.com" in base64.urlsafe_b64decode(raw).decode("utf-8")
