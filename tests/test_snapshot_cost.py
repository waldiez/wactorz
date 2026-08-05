"""The per-event snapshot must not touch the database.

The regression this pins: ``events.snapshot()`` is broadcast on every broker
message, and its two headline totals are the only part that queries. Resolving
them costs one query per agent whose cost is not already in an MQTT frame —
``best_cost`` falls through to the ``_final_cost`` row — plus two full scans of
``kv_store``. With agents publishing continuously that is quadratic in agent
count, on the event loop, between the broker and the browser.

Counting queries rather than timing: a timing assertion passes on a fast machine
whatever the code does.
"""

import pytest

from wactorz.web import events, runtime


class _CountingCursor:
    def __init__(self, counter: list[int]) -> None:
        self._counter = counter

    def execute(self, *_args: object, **_kwargs: object) -> "_CountingCursor":
        self._counter[0] += 1
        return self

    def fetchall(self) -> list:
        return []

    def fetchone(self) -> None:
        return None


class _CountingDB:
    """Stands in for WactorzDB, counting statements rather than running them."""

    def __init__(self) -> None:
        self.queries: list[int] = [0]

    @property
    def conn(self) -> _CountingCursor:
        return _CountingCursor(self.queries)

    def kv_get(self, *_args: object, **_kwargs: object) -> None:
        self.queries[0] += 1
        return


@pytest.fixture(name="db")
def db_fixture(monkeypatch: pytest.MonkeyPatch) -> _CountingDB:
    counting = _CountingDB()
    monkeypatch.setattr(runtime, "db", counting)
    monkeypatch.setattr(runtime, "hard_resetting", False)
    monkeypatch.setattr(runtime, "registry", None)
    return counting


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(
        runtime.state,
        "agents",
        {f"id{i}": {"agent_id": f"id{i}", "name": f"agent{i}"} for i in range(25)},
    )
    monkeypatch.setitem(runtime.state, "nodes", {})
    monkeypatch.setitem(runtime.state, "alerts", [])
    monkeypatch.setitem(runtime.state, "log_feed", [])
    monkeypatch.setitem(runtime.state, "system_health", {})


class TestHotPathDoesNoQueries:
    def test_no_totals_means_no_database(self, db: _CountingDB) -> None:
        events.snapshot(include_totals=False)
        assert db.queries[0] == 0, "the per-event snapshot queried the database"

    def test_totals_do_query(self, db: _CountingDB) -> None:
        """The comparison that gives the previous assertion meaning."""
        events.snapshot(include_totals=True)
        assert db.queries[0] > 0

    def test_cost_is_not_recomputed_per_agent(self, db: _CountingDB) -> None:
        """25 agents, and the hot path stays at zero — not merely fewer."""
        events.snapshot(include_totals=False)
        assert db.queries[0] == 0


class TestShape:
    def test_omits_the_two_totals(self, db: _CountingDB) -> None:
        snap = events.snapshot(include_totals=False)
        assert "total_cost_usd" not in snap
        assert "total_messages" not in snap

    def test_omitted_rather_than_zeroed(self, db: _CountingDB) -> None:
        """Zero would overwrite the browser's figures; absent leaves them.

        ``WSClient._applyStatePatch`` assigns each total only when the key is
        present, so this distinction is the whole reason no frontend change was
        needed.
        """
        snap = events.snapshot(include_totals=False)
        assert snap.get("total_cost_usd") is None

    def test_everything_else_matches_the_full_snapshot(self, db: _CountingDB) -> None:
        trimmed = events.snapshot(include_totals=False)
        full = events.snapshot(include_totals=True)
        assert set(full) - set(trimmed) == {"total_cost_usd", "total_messages"}
        for key in trimmed:
            assert trimmed[key] == full[key], f"{key} differs between the two paths"

    def test_hard_reset_still_reports_zeroes(self, db: _CountingDB, monkeypatch) -> None:
        """A reset is the one time the browser must be told the totals *are* zero."""
        monkeypatch.setattr(runtime, "hard_resetting", True)
        snap = events.snapshot(include_totals=False)
        assert snap["total_cost_usd"] == 0
        assert snap["total_messages"] == 0


class TestTheDispatchPathAsksForNoTotals:
    """Pins the call site, not just the function.

    The tests above would still pass if the listener went back to asking for
    totals — they exercise ``snapshot`` directly. This drives the dispatch path
    that actually runs per broker message.
    """

    @pytest.fixture(autouse=True)
    def _quiet_broadcast(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        from wactorz.web import ws

        sent: list[dict] = []

        async def capture(frame: dict) -> None:
            sent.append(frame)

        monkeypatch.setattr(ws, "broadcast", capture)
        self.sent = sent
        return sent

    async def test_a_broker_message_queries_nothing(self, db: _CountingDB) -> None:
        from wactorz.web import mqtt

        await mqtt.handle_message("agents/id0/status", '{"state": "running"}')
        assert db.queries[0] == 0, "dispatching a broker message hit the database"

    async def test_the_patch_frame_carries_no_totals(self, db: _CountingDB) -> None:
        from wactorz.web import mqtt

        await mqtt.handle_message("agents/id0/status", '{"state": "running"}')
        patches = [f for f in self.sent if f.get("type") == "patch"]
        assert patches, "no patch frame was broadcast"
        assert "total_cost_usd" not in patches[0]["state"]
