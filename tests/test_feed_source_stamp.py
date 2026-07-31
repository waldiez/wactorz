"""Every feed entry carries a ``source`` discriminator.

The unified feed view renders two kinds of entry — broker-derived agent activity
and application log records — and needs to tell them apart without guessing from
the other fields.

The regression pinned here is narrower than "the field exists": stamping happens
in ``add_log`` rather than at its seven call sites, so a call site added later
cannot omit it, and the value is a **constant**. This feed already crosses the
wire to every connected browser, so deriving new fields from broker payloads
would widen what browsers receive — the projection-not-enrichment rule.
"""

import pytest

from wactorz.web import events, runtime


@pytest.fixture(autouse=True)
def _isolate_feed():
    original = list(runtime.state.get("log_feed", []))
    runtime.state["log_feed"] = []
    yield
    runtime.state["log_feed"] = original


def feed() -> list[dict]:
    return runtime.state["log_feed"]


class TestSourceStamp:
    def test_entry_is_stamped(self) -> None:
        events.add_log({"type": "status", "agent_id": "a1"})
        assert feed()[0]["source"] == "agent"

    def test_every_entry_is_stamped(self) -> None:
        for i in range(5):
            events.add_log({"type": "log", "agent_id": f"a{i}"})
        assert all(entry["source"] == "agent" for entry in feed())

    def test_explicit_source_is_kept(self) -> None:
        """Application-log entries will arrive already labelled."""
        events.add_log({"type": "log", "source": "app"})
        assert feed()[0]["source"] == "app"


class TestProjectionNotEnrichment:
    def test_no_other_field_is_added(self) -> None:
        """``source`` is the only addition — nothing is read out of the payload."""
        entry = {"type": "chat", "agent_id": "a1", "content": "hello", "timestamp": 1.0}
        events.add_log(dict(entry))
        assert set(feed()[0]) == set(entry) | {"source"}

    def test_existing_values_untouched(self) -> None:
        events.add_log({"type": "chat", "agent_id": "a1", "content": "hello"})
        stored = feed()[0]
        assert stored["type"] == "chat"
        assert stored["agent_id"] == "a1"
        assert stored["content"] == "hello"


class TestStillBounded:
    def test_cap_unchanged(self) -> None:
        for i in range(150):
            events.add_log({"type": "log", "n": i})
        assert len(feed()) == 100

    def test_newest_first(self) -> None:
        events.add_log({"type": "log", "n": 1})
        events.add_log({"type": "log", "n": 2})
        assert [e["n"] for e in feed()] == [2, 1]
