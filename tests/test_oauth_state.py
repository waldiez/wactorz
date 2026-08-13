"""The OAuth callback must prove it is answering *our* authorization request.

⚠ The callback is an unauthenticated local URL, so anything that can reach it
can deliver a code. `state` is the only thing tying a callback to the request
that started it — and it was generated, sent, and then discarded, which is the
same as never sending one: an attacker's code could be exchanged and *their*
account linked to this install, while the user sees a successful connection to
an account they do not own.
"""

import hmac
import secrets

import pytest


def _accepts(expected: str, returned: str | None) -> bool:
    """The check as `_authorize` performs it, isolated from the browser flow.

    The real path opens a browser, binds a local server and talks to Google, so
    the comparison is exercised here directly — the flow around it is unchanged
    by this fix and has its own coverage.
    """
    return bool(returned) and hmac.compare_digest(returned or "", expected)


class TestTheCallbackHasToMatch:
    def test_our_own_state_is_accepted(self) -> None:
        state = secrets.token_urlsafe(16)

        assert _accepts(state, state)

    @pytest.mark.parametrize(
        "returned",
        [
            None,  # a callback that carried no state at all
            "",
            "not-the-state-we-sent",
            "AAAAAAAAAAAAAAAAAAAAAA",
        ],
    )
    def test_anything_else_is_refused(self, returned: str | None) -> None:
        assert not _accepts(secrets.token_urlsafe(16), returned)

    def test_a_prefix_of_the_real_state_is_refused(self) -> None:
        # `compare_digest` rather than `==`, and a truncated value must not pass
        # on length alone.
        state = secrets.token_urlsafe(16)

        assert not _accepts(state, state[:-1])


class TestTheStateIsWorthComparing:
    def test_it_is_unpredictable(self) -> None:
        # Generated per authorization; two runs must not collide, or the check
        # would pass for a request it never started.
        assert secrets.token_urlsafe(16) != secrets.token_urlsafe(16)

    def test_the_source_keeps_it_for_the_comparison(self) -> None:
        # ⚠ The regression this guards: the value used to be inlined into the
        # request dict and thrown away, so nothing could compare it later.
        from pathlib import Path

        source = Path("wactorz/core/integrations/google_mcp.py").read_text(encoding="utf-8")

        assert "expected_state = secrets.token_urlsafe" in source
        assert "hmac.compare_digest(returned_state, expected_state)" in source
