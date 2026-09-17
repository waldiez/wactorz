"""Where the spend figures come from, and how an upgrade seeds them.

`tests/test_cost_limit_enforcement.py` covers the cap stopping a call. This
covers the accounting behind it against a real database: spend accrues to every
period and to an all-time counter, a runtime override beats the configured
limit, and an install upgraded from a build that kept only per-agent totals has
its counters raised to those totals once — and never again, so a deliberate
reset stays a reset.
"""

import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from wactorz import config
from wactorz.agents.llm import cost
from wactorz.core.persistence import PickleStore, WactorzDB
from wactorz.core.persistence.stores import install_stores


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WactorzDB]:
    db = WactorzDB(str(tmp_path / "wactorz.db"))
    install_stores(db, PickleStore(str(tmp_path / "pickles")))
    monkeypatch.setattr(
        config,
        "CONFIG",
        replace(config.CONFIG, llm_cost_limit_usd=0.0, llm_cost_limit_period="monthly"),
    )
    yield db


class _Broken:
    """A database whose every call fails."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"{name} unavailable")


class TestPeriods:
    @pytest.mark.parametrize(
        ("period", "expected"),
        [("daily", "2026-01-01"), ("weekly", "2026-W01"), ("monthly", "2026-01")],
    )
    def test_period_keys(self, monkeypatch: pytest.MonkeyPatch, period: str, expected: str) -> None:
        class _Clock:
            @staticmethod
            def now() -> datetime:
                return datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(cost, "datetime", _Clock)

        assert cost._period_key(period) == expected


class TestAccrual:
    def test_spend_accrues_to_every_period_and_all_time(self, db: WactorzDB) -> None:
        cost.accumulate_global_cost(0.25)
        cost.accumulate_global_cost(0.0)
        cost.accumulate_global_cost(0.5)

        info = cost.get_global_cost_info()
        assert info["spend_usd"] == 0.75
        assert info["limit_usd"] is None and info["pct_used"] is None
        assert cost.get_global_alltime_cost() == 0.75
        for period in ("daily", "weekly"):
            assert db.kv_get("_system", cost._global_cost_kv_key(period)) == 0.75

    def test_without_a_database_nothing_is_recorded(self) -> None:
        cost.accumulate_global_cost(1.0)

        assert cost.get_global_alltime_cost() == 0.0
        assert cost.get_global_cost_info()["spend_usd"] == 0.0

    def test_a_runtime_override_beats_the_configured_limit(self, db: WactorzDB) -> None:
        cost.set_cost_limit(1.0, "daily")
        cost.accumulate_global_cost(0.85)

        info = cost.get_global_cost_info()

        assert (info["period"], info["limit_usd"], info["pct_used"]) == ("daily", 1.0, 85.0)
        assert info["warning"] is True and info["limit_reached"] is False

    def test_an_invalid_period_or_missing_database_is_refused(self) -> None:
        with pytest.raises(ValueError, match="daily, weekly, or monthly"):
            cost.set_cost_limit(1.0, "hourly")
        with pytest.raises(RuntimeError, match="Database not available"):
            cost.set_cost_limit(1.0, "daily")
        with pytest.raises(RuntimeError, match="Database not available"):
            cost.reset_global_cost()

    def test_a_reset_zeroes_every_counter(self, db: WactorzDB) -> None:
        cost.accumulate_global_cost(2.0)

        info = cost.reset_global_cost()

        assert info["spend_usd"] == 0.0
        assert cost.get_global_alltime_cost() == 0.0

    def test_a_failing_database_reads_as_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cost, "get_db", lambda: _Broken())

        cost.accumulate_global_cost(1.0)

        assert cost.get_global_alltime_cost() == 0.0
        assert cost.get_global_cost_info()["spend_usd"] == 0.0


class TestUpgradeSeeding:
    @staticmethod
    def _old_install(db: WactorzDB) -> None:
        db.kv_set("weather", "_final_cost", {"cost_usd": 1.5})
        db.kv_set("gone", "_final_cost", {"cost_usd": "not a number"})
        db.kv_set("_system", "_lifetime_cost_ledger", {"a": 1.0, "b": 2.25})

    def test_counters_are_raised_to_the_best_known_total_once(self, db: WactorzDB) -> None:
        self._old_install(db)

        info = cost.get_global_cost_info()

        assert info["spend_usd"] == 3.25
        assert cost.get_global_alltime_cost() == 3.25

        cost.reset_global_cost()
        assert cost.get_global_cost_info()["spend_usd"] == 0.0, "a reset is not undone"

    def test_a_counter_already_higher_is_left_alone(self, db: WactorzDB) -> None:
        self._old_install(db)
        cost.accumulate_global_cost(10.0)

        assert cost.get_global_cost_info()["spend_usd"] == 10.0

    def test_unreadable_durable_totals_count_as_nothing(self) -> None:
        assert cost._known_persisted_cost_total(_Broken()) == 0.0

    def test_seeding_against_a_failing_database_does_not_raise(self) -> None:
        cost._bootstrap_active_global_cost(_Broken(), "monthly")
        cost._seed_alltime_cost(_Broken())

    def test_a_failing_write_during_seeding_is_tolerated(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._old_install(db)

        def _refuse(*_args: Any) -> None:
            raise OSError("read-only")

        monkeypatch.setattr(db, "kv_set", _refuse)

        cost._bootstrap_active_global_cost(db, "monthly")
        cost._seed_alltime_cost(db)

        assert db.kv_get("_system", cost._GLOBAL_COST_ALLTIME_KEY) is None


class TestCheck:
    def test_no_limit_never_blocks(self, db: WactorzDB) -> None:
        cost.accumulate_global_cost(100.0)

        cost.check_cost_limit()

    def test_near_the_limit_warns_and_at_it_blocks(
        self, db: WactorzDB, caplog: pytest.LogCaptureFixture
    ) -> None:
        cost.set_cost_limit(1.0, "monthly")
        cost.accumulate_global_cost(0.9)

        cost.check_cost_limit()
        assert "90.0% of $1.00 monthly budget used" in caplog.text

        cost.accumulate_global_cost(0.1)
        with pytest.raises(RuntimeError, match=re.escape("LLM cost limit of $1.00 reached")):
            cost.check_cost_limit()
