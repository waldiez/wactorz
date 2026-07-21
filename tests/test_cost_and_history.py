"""
Tests for cost persistence, cost restoration on startup, and chat history API.

Covers three recent features:
  - LLM cost written to SQLite (_final_cost key) and restored on agent restart
  - Historical (deleted agent) cost included in snapshot() total
  - GET /api/actors/{id}/history endpoint filters and returns conversation history
"""

import json
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from wactorz.monitor import api_actors, cost, events, runtime

# ── Minimal stubs so heavy optional deps don't need to be installed ──────────
# aiohttp is a hard dependency and monitor_server imports it fully at module
# level (web, WSMsgType, …), so it must NOT be stubbed — handler responses are
# real aiohttp Response objects, read via _payload() below.
sys.modules.setdefault("openai", types.ModuleType("openai"))


# ─────────────────────────────────────────────────────────────────────────────
# 1. Persistence routing
# ─────────────────────────────────────────────────────────────────────────────


class FinalCostRoutingTest(unittest.TestCase):
    def test_final_cost_key_is_in_sqlite_keys(self):
        from wactorz.core.persistence import _SQLITE_KEYS

        self.assertIn(
            "_final_cost",
            _SQLITE_KEYS,
            "_final_cost must route to SQLite so it survives restarts "
            "and is queryable for deleted-agent cost accounting",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. LLMAgent cost restore on startup
# ─────────────────────────────────────────────────────────────────────────────


class LLMAgentCostRestoreTest(unittest.IsolatedAsyncioTestCase):
    """
    LLMAgent.on_start() should seed total_* from the persisted _final_cost so
    that heartbeats carry accurate lifetime totals after a restart.
    """

    def _make_agent(self, saved_cost: dict):
        from wactorz.agents.llm_agent import LLMAgent

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            agent = LLMAgent(name="test-llm", persistence_dir=tmp)

        def _recall(key, default=None):
            if key == "_final_cost":
                return saved_cost
            if key == "conversation_history":
                return []
            if key == "history_summary":
                return ""
            return default

        agent.recall = _recall
        agent.persist = MagicMock()
        agent.publish_manifest = AsyncMock()
        return agent

    async def test_cost_seeded_from_persisted_final_cost(self):
        saved = {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.0042}
        agent = self._make_agent(saved)

        await agent.on_start()

        self.assertEqual(agent.total_input_tokens, 100)
        self.assertEqual(agent.total_output_tokens, 50)
        self.assertAlmostEqual(agent.total_cost_usd, 0.0042, places=6)

    async def test_zero_cost_when_no_saved_cost(self):
        agent = self._make_agent({})

        await agent.on_start()

        self.assertEqual(agent.total_input_tokens, 0)
        self.assertEqual(agent.total_cost_usd, 0.0)

    async def test_cost_accumulates_on_top_of_restored_baseline(self):
        """After restoring from persistence, new exchanges add to the running total."""
        saved = {"input_tokens": 200, "output_tokens": 80, "cost_usd": 0.01}
        agent = self._make_agent(saved)
        await agent.on_start()

        agent.total_input_tokens += 10
        agent.total_output_tokens += 5
        agent.total_cost_usd += 0.001

        self.assertEqual(agent.total_input_tokens, 210)
        self.assertAlmostEqual(agent.total_cost_usd, 0.011, places=6)


# ─────────────────────────────────────────────────────────────────────────────
# 3. _persist_cost() writes correct structure
# ─────────────────────────────────────────────────────────────────────────────


class PersistCostTest(unittest.TestCase):
    def _make_agent(self):
        from wactorz.agents.llm_agent import LLMAgent

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            agent = LLMAgent(name="cost-agent", persistence_dir=tmp)

        agent.persist = MagicMock()
        agent.total_input_tokens = 300
        agent.total_output_tokens = 120
        agent.total_cost_usd = 0.0315
        return agent

    def test_persist_cost_writes_all_fields(self):
        agent = self._make_agent()
        agent._persist_cost()

        agent.persist.assert_called_once()
        key, payload = agent.persist.call_args[0]
        self.assertEqual(key, "_final_cost")
        self.assertEqual(payload["input_tokens"], 300)
        self.assertEqual(payload["output_tokens"], 120)
        self.assertAlmostEqual(payload["cost_usd"], 0.0315, places=6)
        self.assertEqual(payload["name"], "cost-agent")

    def test_persist_cost_rounds_to_six_decimals(self):
        agent = self._make_agent()
        agent.total_cost_usd = 1 / 3
        agent._persist_cost()

        _, payload = agent.persist.call_args[0]
        # round() to 6 places: 0.333333
        self.assertEqual(payload["cost_usd"], round(1 / 3, 6))


# ─────────────────────────────────────────────────────────────────────────────
# 4. Historical cost accounting in monitor_server
# ─────────────────────────────────────────────────────────────────────────────


def _make_kv_db(entries: list[dict]) -> object:
    """Return a minimal db stub backed by in-memory SQLite with kv_store rows."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE kv_store (agent TEXT, key TEXT, value TEXT)")
    for e in entries:
        conn.execute(
            "INSERT INTO kv_store (agent, key, value) VALUES (?, ?, ?)",
            (e["agent"], e["key"], json.dumps(e["value"])),
        )
    conn.commit()
    return types.SimpleNamespace(conn=conn)


class HistoricalCostTest(unittest.TestCase):
    def setUp(self):
        # Reset module state between tests
        self._orig_db = runtime.db
        self._orig_state = dict(runtime.state["agents"])

    def tearDown(self):
        runtime.db = self._orig_db
        runtime.state["agents"] = self._orig_state

    def _live_names(self):
        """Derive live_names from state["agents"] as snapshot() does when no registry."""
        return {a.get("name", "") for a in runtime.state["agents"].values()}

    def test_returns_zero_when_db_is_none(self):
        runtime.db = None
        self.assertEqual(cost.historical_cost_usd(self._live_names()), 0.0)

    def test_returns_zero_when_no_final_cost_rows(self):
        runtime.db = _make_kv_db([])
        runtime.state["agents"] = {}
        self.assertEqual(cost.historical_cost_usd(self._live_names()), 0.0)

    def test_sums_costs_for_deleted_agents(self):
        runtime.db = _make_kv_db(
            [
                {
                    "agent": "old-agent",
                    "key": "_final_cost",
                    "value": {"name": "old-agent", "cost_usd": 0.05},
                },
                {
                    "agent": "gone-agent",
                    "key": "_final_cost",
                    "value": {"name": "gone-agent", "cost_usd": 0.03},
                },
            ]
        )
        runtime.state["agents"] = {}  # no live agents

        total = cost.historical_cost_usd(self._live_names())
        self.assertAlmostEqual(total, 0.08, places=6)

    def test_excludes_live_agent_costs(self):
        """Live agents report cost via MQTT heartbeats — don't double-count."""
        runtime.db = _make_kv_db(
            [
                {
                    "agent": "live-agent",
                    "key": "_final_cost",
                    "value": {"name": "live-agent", "cost_usd": 0.10},
                },
                {
                    "agent": "dead-agent",
                    "key": "_final_cost",
                    "value": {"name": "dead-agent", "cost_usd": 0.04},
                },
            ]
        )
        runtime.state["agents"] = {
            "live-agent": {"name": "live-agent", "cost_usd": 0.10},
        }

        total = cost.historical_cost_usd(self._live_names())
        self.assertAlmostEqual(total, 0.04, places=6)

    def test_ignores_malformed_rows(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE kv_store (agent TEXT, key TEXT, value TEXT)")
        conn.execute(
            "INSERT INTO kv_store VALUES (?, ?, ?)", ("broken", "_final_cost", "not-valid-json{{{")
        )
        conn.execute(
            "INSERT INTO kv_store VALUES (?, ?, ?)",
            ("ok", "_final_cost", json.dumps({"name": "ok", "cost_usd": 0.02})),
        )
        conn.commit()
        runtime.db = types.SimpleNamespace(conn=conn)
        runtime.state["agents"] = {}

        total = cost.historical_cost_usd(self._live_names())
        self.assertAlmostEqual(total, 0.02, places=6)


# ─────────────────────────────────────────────────────────────────────────────
# 4b. Durable lifetime cost ledger in monitor_server
# ─────────────────────────────────────────────────────────────────────────────


class _KVStub:
    """Minimal kv_get/kv_set db stub with JSON round-trip, like WactorzDB."""

    def __init__(self, store=None):
        self._kv = dict(store or {})

    def kv_get(self, agent, key, default=None):
        return self._kv.get((agent, key), default)

    def kv_set(self, agent, key, value):
        # Mimic the real store's JSON serialization so callers can't mutate
        # what's "persisted" by holding a reference.
        self._kv[(agent, key)] = json.loads(json.dumps(value))


class LifetimeCostLedgerTest(unittest.TestCase):
    def setUp(self):
        self._orig_db = runtime.db
        self._orig_ledger = dict(cost.lifetime_cost)
        self._orig_loaded = cost.lifetime_loaded
        cost.lifetime_cost.clear()
        cost.lifetime_loaded = False
        runtime.db = _KVStub()

    def tearDown(self):
        runtime.db = self._orig_db
        cost.lifetime_cost.clear()
        cost.lifetime_cost.update(self._orig_ledger)
        cost.lifetime_loaded = self._orig_loaded

    def test_records_and_totals_cost(self):
        cost.record_lifetime_cost("a1", 0.05)
        cost.record_lifetime_cost("a2", 0.03)
        self.assertAlmostEqual(cost.lifetime_cost_total(), 0.08, places=6)

    def test_is_monotonic_high_water(self):
        cost.record_lifetime_cost("a1", 0.05)
        cost.record_lifetime_cost("a1", 0.02)  # lower report — ignored
        self.assertAlmostEqual(cost.lifetime_cost_total(), 0.05, places=6)
        cost.record_lifetime_cost("a1", 0.09)  # higher — raises
        self.assertAlmostEqual(cost.lifetime_cost_total(), 0.09, places=6)

    def test_survives_agent_disappearing(self):
        """The core bug: a deleted / hard-killed agent must not drop the total."""
        cost.record_lifetime_cost("planner-1123da", 0.0365)
        # Agent vanishes (no more reports, never written to _final_cost).
        self.assertAlmostEqual(cost.lifetime_cost_total(), 0.0365, places=6)

    def test_ignores_non_positive_and_invalid(self):
        for bad in (0, -1.0, None, "x"):
            cost.record_lifetime_cost("a1", bad)
        self.assertEqual(cost.lifetime_cost_total(), 0.0)

    def test_persists_to_db_and_reloads(self):
        cost.record_lifetime_cost("a1", 0.07)
        # Simulate a monitor restart: drop in-memory state, keep the db.
        cost.lifetime_cost.clear()
        cost.lifetime_loaded = False
        self.assertAlmostEqual(cost.lifetime_cost_total(), 0.07, places=6)

    def test_no_db_is_safe(self):
        runtime.db = None
        cost.lifetime_loaded = False
        cost.record_lifetime_cost("a1", 0.05)  # must not raise
        self.assertEqual(cost.lifetime_cost_total(), 0.05)


class ResetActorCostTest(unittest.TestCase):
    """A wipe / metrics-reset must realign the per-call accrual baseline, or the
    global period and all-time counters stop advancing afterward because
    delta = total_cost_usd - baseline goes negative (the "limit count stopped
    counting after a wipe" bug)."""

    def test_zeroes_counters_and_baselines(self):
        class _Actor:
            total_cost_usd = 0.42
            total_input_tokens = 1000
            total_output_tokens = 500
            _last_persisted_usd = 0.42
            _last_period_cost_usd = 0.42

        a = _Actor()
        cost.reset_actor_cost(a)
        self.assertEqual(a.total_cost_usd, 0.0)
        self.assertEqual(a.total_input_tokens, 0)
        self.assertEqual(a.total_output_tokens, 0)
        self.assertEqual(a._last_persisted_usd, 0.0)
        self.assertEqual(a._last_period_cost_usd, 0.0)

    def test_next_delta_is_positive_after_reset(self):
        """The first new spend after a wipe must yield a positive accrual delta
        (was negative because the baseline kept the pre-wipe total)."""

        class _Actor:
            total_cost_usd = 0.42
            total_input_tokens = 0
            total_output_tokens = 0
            _last_persisted_usd = 0.42

        a = _Actor()
        cost.reset_actor_cost(a)
        a.total_cost_usd += 0.01  # one new LLM call after the wipe
        self.assertGreater(a.total_cost_usd - a._last_persisted_usd, 0)

    def test_actor_without_cost_attrs_is_noop(self):
        cost.reset_actor_cost(object())  # must not raise


class SnapshotTotalsTest(unittest.TestCase):
    """snapshot() headline totals must match the cards the dashboard renders,
    including remote / spawned agents that live in state but not in this
    process's registry."""

    def setUp(self):
        self._orig_db = runtime.db
        self._orig_reg = runtime.registry
        self._orig_agents = dict(runtime.state["agents"])
        self._orig_ledger = dict(cost.lifetime_cost)
        self._orig_loaded = cost.lifetime_loaded
        runtime.db = None  # no historical / no ledger persistence
        cost.lifetime_cost.clear()
        cost.lifetime_loaded = True  # skip db hydrate
        runtime.state["agents"] = {}

    def tearDown(self):
        runtime.db = self._orig_db
        runtime.registry = self._orig_reg
        runtime.state["agents"] = self._orig_agents
        cost.lifetime_cost.clear()
        cost.lifetime_cost.update(self._orig_ledger)
        cost.lifetime_loaded = self._orig_loaded

    def test_sums_all_visible_agents_when_no_registry(self):
        runtime.registry = None
        runtime.state["agents"] = {
            "main": {"name": "main", "cost_usd": 0.0381},
            "reachy-mini": {"name": "reachy-mini", "cost_usd": 0.0213},
        }
        snap = events.snapshot()
        self.assertAlmostEqual(snap["total_cost_usd"], 0.0594, places=6)

    def test_includes_state_only_agent_missing_from_registry(self):
        """A spawned/remote agent shows on a card (state) but isn't in the local
        registry — it must still count toward the headline total."""
        local_main = types.SimpleNamespace(
            actor_id="main",
            name="main",
            total_cost_usd=0.0381,
            metrics=types.SimpleNamespace(messages_processed=1),
        )
        runtime.registry = types.SimpleNamespace(all_actors=lambda: [local_main])
        runtime.state["agents"] = {
            "main": {"name": "main", "cost_usd": 0.0381, "messages_processed": 1},
            "reachy-mini": {"name": "reachy-mini", "cost_usd": 0.0213, "messages_processed": 3},
        }
        snap = events.snapshot()
        # Old behaviour summed only registry actors -> 0.0381. Must be the full sum.
        self.assertAlmostEqual(snap["total_cost_usd"], 0.0594, places=6)
        self.assertEqual(snap["total_messages"], 4)

    def test_includes_cost_living_on_actor_object_only(self):
        """An agent's card shows cost from its live actor object (total_cost_usd)
        but state["cost_usd"] was never set by an MQTT metrics frame. The header
        must still count it."""
        main_actor = types.SimpleNamespace(
            actor_id="main-id",
            name="main",
            total_cost_usd=0.038124,
            metrics=types.SimpleNamespace(messages_processed=1),
        )
        reachy_actor = types.SimpleNamespace(
            actor_id="reachy-id",
            name="reachy-mini",
            total_cost_usd=0.021285,
            metrics=types.SimpleNamespace(messages_processed=3),
        )
        runtime.registry = types.SimpleNamespace(all_actors=lambda: [main_actor, reachy_actor])
        runtime.state["agents"] = {
            "main-id": {"name": "main", "cost_usd": 0.038124, "messages_processed": 1},
            "reachy-id": {"name": "reachy-mini", "messages_processed": 3},  # no cost_usd
        }
        snap = events.snapshot()
        self.assertAlmostEqual(snap["total_cost_usd"], 0.059409, places=6)
        self.assertEqual(snap["total_messages"], 4)

    def test_includes_cost_from_sqlite_final_cost(self):
        """Cost lives only in the persisted _final_cost row (no MQTT state, zero on
        the object) — still counts, matching the card's _actor_cost fallback."""
        runtime.db = _make_kv_db(
            [
                {
                    "agent": "main",
                    "key": "_final_cost",
                    "value": {"name": "main", "cost_usd": 0.038124},
                },
                {
                    "agent": "reachy-mini",
                    "key": "_final_cost",
                    "value": {"name": "reachy-mini", "cost_usd": 0.021285},
                },
            ]
        )
        main_a = types.SimpleNamespace(
            actor_id="m",
            name="main",
            total_cost_usd=0.0,
            metrics=types.SimpleNamespace(messages_processed=0),
        )
        reachy_a = types.SimpleNamespace(
            actor_id="r",
            name="reachy-mini",
            total_cost_usd=0.0,
            metrics=types.SimpleNamespace(messages_processed=0),
        )
        runtime.registry = types.SimpleNamespace(all_actors=lambda: [main_a, reachy_a])
        runtime.state["agents"] = {"m": {"name": "main"}, "r": {"name": "reachy-mini"}}
        snap = events.snapshot()
        # Must NOT double-count: _final_cost is used for the live sum, and
        # historical (also _final_cost) must skip names already counted.
        self.assertAlmostEqual(snap["total_cost_usd"], 0.059409, places=6)

    def test_folds_in_live_actor_not_yet_in_state(self):
        """Fresh-boot window: an actor exists but hasn't published a heartbeat
        into state yet — its cost still counts, with no double-count for those
        already in state."""
        in_state = types.SimpleNamespace(
            actor_id="main",
            name="main",
            total_cost_usd=0.05,
            metrics=types.SimpleNamespace(messages_processed=0),
        )
        not_yet = types.SimpleNamespace(
            actor_id="fresh",
            name="fresh",
            total_cost_usd=0.02,
            metrics=types.SimpleNamespace(messages_processed=0),
        )
        runtime.registry = types.SimpleNamespace(all_actors=lambda: [in_state, not_yet])
        runtime.state["agents"] = {"main": {"name": "main", "cost_usd": 0.05}}
        snap = events.snapshot()
        self.assertAlmostEqual(snap["total_cost_usd"], 0.07, places=6)


# ─────────────────────────────────────────────────────────────────────────────
# 5. actor_history_handler
# ─────────────────────────────────────────────────────────────────────────────


def _payload(resp):
    """Decode the JSON body of a real aiohttp ``web.json_response``."""
    return json.loads(resp.body)


class ActorHistoryHandlerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_registry = runtime.registry

    def tearDown(self):
        runtime.registry = self._orig_registry

    def _make_request(self, actor_id: str):
        return types.SimpleNamespace(match_info={"actor_id": actor_id})

    async def test_returns_empty_list_when_registry_none(self):
        runtime.registry = None

        resp = await api_actors.actor_history_handler(self._make_request("any"))

        self.assertEqual(_payload(resp), [])
        self.assertEqual(resp.status, 200)

    async def test_returns_empty_list_when_actor_not_found(self):
        registry = MagicMock()
        registry.get.return_value = None
        runtime.registry = registry

        resp = await api_actors.actor_history_handler(self._make_request("ghost"))

        self.assertEqual(_payload(resp), [])

    async def test_returns_only_user_and_assistant_turns(self):
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "tool", "content": "tool output"},  # should be filtered
            {"role": "system", "content": "system prompt"},  # should be filtered
        ]
        actor = MagicMock()
        actor.recall.return_value = history
        registry = MagicMock()
        registry.get.return_value = actor
        runtime.registry = registry

        resp = await api_actors.actor_history_handler(self._make_request("test-agent"))

        payload = _payload(resp)
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["role"], "user")
        self.assertEqual(payload[1]["role"], "assistant")

    async def test_returns_empty_list_when_history_empty(self):
        actor = MagicMock()
        actor.recall.return_value = []
        registry = MagicMock()
        registry.get.return_value = actor
        runtime.registry = registry

        resp = await api_actors.actor_history_handler(self._make_request("quiet-agent"))

        self.assertEqual(_payload(resp), [])

    async def test_handles_actor_without_recall(self):
        """Actors without a recall() method (non-LLM) return empty history."""
        actor = object()  # plain object, no recall method
        registry = MagicMock()
        registry.get.return_value = actor
        runtime.registry = registry

        resp = await api_actors.actor_history_handler(self._make_request("dumb-agent"))

        self.assertEqual(_payload(resp), [])


# ─────────────────────────────────────────────────────────────────────────────
# 6. Global period-spend accumulation, rollover, and reset
# ─────────────────────────────────────────────────────────────────────────────


class _KVStub:
    """Minimal kv_get/kv_set store standing in for WactorzDB."""

    def __init__(self):
        self.store = {}

    def kv_set(self, agent, key, value):
        self.store[(agent, key)] = json.dumps(value, default=str)

    def kv_get(self, agent, key, default=None):
        raw = self.store.get((agent, key))
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return raw


def _fixed_datetime(y, mo, d):
    from datetime import datetime as _dt

    class _DT(_dt):
        @classmethod
        def now(cls, tz=None):
            return _dt(y, mo, d, 12, 0, 0)

    return _DT


class GlobalCostAccumulationTest(unittest.TestCase):
    def setUp(self):
        import wactorz.agents.llm_agent as L

        self.L = L
        self.db = _KVStub()
        self._p_db = patch.object(L, "get_db", lambda: self.db)
        self._p_db.start()

    def tearDown(self):
        self._p_db.stop()

    def test_accumulates_without_a_limit(self):
        """Regression: period spend must accrue even when no cap is configured."""
        with patch.object(self.L, "datetime", _fixed_datetime(2026, 6, 3)):
            self.L._accumulate_global_cost(0.50)
            info = self.L.get_global_cost_info()
        self.assertAlmostEqual(info["spend_usd"], 0.50, places=6)

    def test_cap_set_mid_period_sees_prior_spend(self):
        """Spend before a cap exists is still counted, so the cap can't be silently overshot."""
        with patch.object(self.L, "datetime", _fixed_datetime(2026, 7, 1)):
            self.L._accumulate_global_cost(20.0)  # no cap yet
            self.L.set_cost_limit(5.0, "monthly")  # now add a $5 cap
            info = self.L.get_global_cost_info()
        self.assertAlmostEqual(info["spend_usd"], 20.0, places=6)
        self.assertTrue(info["limit_reached"])

    def test_day_rollover_starts_fresh(self):
        with patch.object(self.L, "datetime", _fixed_datetime(2026, 6, 3)):
            self.L.set_cost_limit(10.0, "daily")
            self.L._accumulate_global_cost(3.0)
            self.assertAlmostEqual(self.L.get_global_cost_info()["spend_usd"], 3.0, places=6)
        with patch.object(self.L, "datetime", _fixed_datetime(2026, 6, 4)):
            self.assertAlmostEqual(self.L.get_global_cost_info()["spend_usd"], 0.0, places=6)
        # going back preserves the old bucket
        with patch.object(self.L, "datetime", _fixed_datetime(2026, 6, 3)):
            self.assertAlmostEqual(self.L.get_global_cost_info()["spend_usd"], 3.0, places=6)

    def test_reset_zeroes_current_period(self):
        with patch.object(self.L, "datetime", _fixed_datetime(2026, 6, 3)):
            self.L.set_cost_limit(10.0, "daily")
            self.L._accumulate_global_cost(3.0)
            self.L.reset_global_cost()
            self.assertAlmostEqual(self.L.get_global_cost_info()["spend_usd"], 0.0, places=6)

    def test_alltime_counter_survives_period_rollover(self):
        """All-time spend keeps accruing across months while the monthly bucket resets."""
        with patch.object(self.L, "datetime", _fixed_datetime(2026, 6, 3)):
            self.L._accumulate_global_cost(2.0)
            self.assertAlmostEqual(self.L.get_global_alltime_cost(), 2.0, places=6)
        with patch.object(self.L, "datetime", _fixed_datetime(2026, 7, 1)):
            self.L._accumulate_global_cost(1.5)
            # New month: "this period" resets, but all-time keeps both months.
            self.assertAlmostEqual(self.L.get_global_cost_info()["spend_usd"], 1.5, places=6)
            self.assertAlmostEqual(self.L.get_global_alltime_cost(), 3.5, places=6)

    def test_alltime_counter_never_below_period_spend(self):
        """Invariant the dashboard relies on: all-time floor >= this-period spend."""
        with patch.object(self.L, "datetime", _fixed_datetime(2026, 6, 3)):
            self.L._accumulate_global_cost(0.2157)
            info = self.L.get_global_cost_info()
        self.assertGreaterEqual(self.L.get_global_alltime_cost(), info["spend_usd"])

    def test_reset_zeroes_alltime_too(self):
        with patch.object(self.L, "datetime", _fixed_datetime(2026, 6, 3)):
            self.L._accumulate_global_cost(3.0)
            self.L.reset_global_cost()
            self.assertAlmostEqual(self.L.get_global_alltime_cost(), 0.0, places=6)

    def test_weekly_key_is_iso_week(self):
        # 2026-01-01 is a Thursday → ISO week 2026-W01 (not the %W "W00" partial)
        with patch.object(self.L, "datetime", _fixed_datetime(2026, 1, 1)):
            self.assertEqual(self.L._period_key("weekly"), "2026-W01")
        # late-December days that belong to next year's ISO week 1
        with patch.object(self.L, "datetime", _fixed_datetime(2025, 12, 29)):
            self.assertEqual(self.L._period_key("weekly"), "2026-W01")

    def test_planner_usage_feeds_period_spend(self):
        from wactorz.agents.planner_agent import PlannerAgent

        agent = PlannerAgent(llm_provider=None)
        with patch("wactorz.agents.planner_agent._accumulate_global_cost") as accrue:
            agent._accrue_usage({"input_tokens": 2, "output_tokens": 3, "cost_usd": 0.0123})
            agent._accrue_usage({"input_tokens": 4, "output_tokens": 5, "cost_usd": 0.004})

        self.assertAlmostEqual(agent.total_cost_usd, 0.0163, places=6)
        deltas = [c.args[0] for c in accrue.call_args_list]
        self.assertAlmostEqual(deltas[0], 0.0123, places=6)
        self.assertAlmostEqual(deltas[1], 0.004, places=6)

    def test_one_off_actuator_usage_feeds_period_spend(self):
        from wactorz.agents.one_off_actuator_agent import OneOffActuatorAgent

        agent = OneOffActuatorAgent(
            request="turn on the lamp",
            llm_provider=None,
            task_id="task-12345678",
            reply_to_id="main",
        )
        with patch("wactorz.agents.one_off_actuator_agent._accumulate_global_cost") as accrue:
            agent._accumulate_usage({"input_tokens": 7, "output_tokens": 8, "cost_usd": 0.0395})

        self.assertAlmostEqual(agent.total_cost_usd, 0.0395, places=6)
        accrue.assert_called_once_with(0.0395)


if __name__ == "__main__":
    unittest.main()
