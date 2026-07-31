"""
Tests for wactorz.reset and the /api/reset HTTP handler.

Covers:
  1. reset_logs() — truncates files offline
  2. reset_handler scope validation
  3. reset_handler response shape for every valid scope
  4. reset_handler per-agent filter is forwarded
"""

# pylint: disable=too-few-public-methods,too-many-instance-attributes
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=protected-access,import-outside-toplevel

# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import json
import logging
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from wactorz.web import api_reset, cost, runtime


def _payload(resp):
    """Decode the JSON body of a real aiohttp ``web.json_response``.

    the web app binds ``web`` at import (real aiohttp), so its handlers always
    return a real Response no matter what other tests do to ``sys.modules`` — read
    the body directly instead of faking ``json_response`` (which cannot reach the
    handler's already-bound ``web`` reference anyway).
    """
    return json.loads(resp.body)


# ─────────────────────────────────────────────────────────────────────────────
# 1. reset_logs — offline file truncation
# ─────────────────────────────────────────────────────────────────────────────


class ResetLogsTest(unittest.TestCase):
    def test_truncates_wactorz_log(self):
        from wactorz.reset import reset_logs

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "wactorz.log"
            log.write_text("some log content\nmore content\n")
            reset_logs(log_dir=tmp)
            self.assertEqual(log.read_text(), "")

    def test_truncates_monitor_log(self):
        from wactorz.reset import reset_logs

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "monitor.log"
            log.write_text("monitor output\n")
            reset_logs(log_dir=tmp)
            self.assertEqual(log.read_text(), "")

    def test_skips_missing_files_silently(self):
        from wactorz.reset import reset_logs

        with tempfile.TemporaryDirectory() as tmp:
            # Neither file exists — should not raise
            reset_logs(log_dir=tmp)

    def test_truncates_both_logs_when_both_present(self):
        from wactorz.reset import reset_logs

        with tempfile.TemporaryDirectory() as tmp:
            for name in ("wactorz.log", "monitor.log"):
                (Path(tmp) / name).write_text("data")
            reset_logs(log_dir=tmp)
            for name in ("wactorz.log", "monitor.log"):
                self.assertEqual((Path(tmp) / name).read_text(), "")

    def test_truncates_open_file_handler(self):
        from wactorz.reset import reset_logs

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "wactorz.log"
            handler = logging.FileHandler(str(log_path))
            assert handler.stream
            handler.stream.write("previous content\n")
            handler.stream.flush()
            logging.root.addHandler(handler)
            try:
                reset_logs(log_dir=tmp)
                handler.stream.flush()
                # The handler writes "logs cleared" after truncation, so the
                # file is non-empty — but the old content must be gone.
                content = log_path.read_text()
                self.assertNotIn("previous content", content)
            finally:
                logging.root.removeHandler(handler)
                handler.close()

    def test_releases_the_handler_lock_when_truncation_fails(self):
        """A failed truncate must not strand the logger's lock.

        The failure this guards is silent and total: the exception is caught and
        logged, so the reset reports success, but every later log call blocks
        forever on a lock nobody holds a reference to — and nothing explains why,
        because logging is what deadlocked.

        The lock has to be probed from another thread: handlers use an RLock, so
        the thread that leaked it could re-acquire it and see nothing wrong.
        """
        import threading

        from wactorz.reset import reset_logs

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "wactorz.log"
            handler = logging.FileHandler(str(log_path))
            assert handler.stream
            handler.stream.write("content\n")
            handler.stream.flush()

            def _explode(*_args, **_kwargs):
                raise OSError("disk gone")

            handler.stream.truncate = _explode  # type: ignore[method-assign]
            logging.root.addHandler(handler)
            try:
                reset_logs(log_dir=tmp)  # swallows the error by design

                acquired: list[bool] = []

                def _probe() -> None:
                    # Acquire and release on the same thread: an RLock may only
                    # be released by its owner, and releasing from elsewhere
                    # raises while leaving the lock held for good.
                    got = handler.lock.acquire(timeout=2)
                    acquired.append(got)
                    if got:
                        handler.lock.release()

                probe = threading.Thread(target=_probe)
                probe.start()
                probe.join(timeout=5)

                self.assertTrue(acquired and acquired[0], "handler lock was never released")
            finally:
                logging.root.removeHandler(handler)
                handler.close()


# ─────────────────────────────────────────────────────────────────────────────
# 2 & 3. reset_handler — scope validation and response shape
# ─────────────────────────────────────────────────────────────────────────────


def _make_request(body: dict):
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    return req


class ResetHandlerScopeValidationTest(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_scope_returns_400(self):

        req = _make_request({"scope": "invalid"})
        resp = await api_reset.reset_handler(req)
        self.assertEqual(resp.status, 400)
        self.assertIn("scope", str(_payload(resp)))

    async def test_empty_scope_returns_400(self):

        req = _make_request({"scope": ""})
        resp = await api_reset.reset_handler(req)
        self.assertEqual(resp.status, 400)

    async def test_missing_scope_returns_400(self):

        req = _make_request({})
        resp = await api_reset.reset_handler(req)
        self.assertEqual(resp.status, 400)

    async def test_invalid_json_returns_400(self):

        req = MagicMock()
        req.json = AsyncMock(side_effect=Exception("bad json"))
        resp = await api_reset.reset_handler(req)
        self.assertEqual(resp.status, 400)


VALID_SCOPES = ("chat", "state", "metrics", "spawns", "logs", "all")


class ResetHandlerValidScopesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_state = {
            "alerts": list(runtime.state.get("alerts", [])),
            "log_feed": list(runtime.state.get("log_feed", [])),
        }

    def tearDown(self):
        runtime.state["alerts"] = self._orig_state["alerts"]
        runtime.state["log_feed"] = self._orig_state["log_feed"]

    async def _call(self, scope: str, agent: str | None = None) -> object:

        body: dict = {"scope": scope}
        if agent is not None:
            body["agent"] = agent
        req = _make_request(body)

        # Patch the individual reset functions so no FS/DB access is needed
        with (
            patch("wactorz.reset.reset_chat"),
            patch("wactorz.reset.reset_agent_state"),
            patch("wactorz.reset.reset_metrics"),
            patch("wactorz.reset.reset_spawns"),
            patch("wactorz.reset.reset_logs"),
            patch("wactorz.reset.reset_all"),
            patch("wactorz.reset._reset_all_pickles"),
            patch("wactorz.web.ws.broadcast", new=AsyncMock()),
        ):
            return await api_reset.reset_handler(req)

    async def test_chat_scope_returns_200(self):
        resp = await self._call("chat")
        payload = _payload(resp)
        self.assertEqual(resp.status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["scope"], "chat")

    async def test_state_scope_returns_200(self):
        resp = await self._call("state")
        self.assertEqual(resp.status, 200)

    async def test_metrics_scope_returns_200(self):
        resp = await self._call("metrics")
        self.assertEqual(resp.status, 200)

    async def test_spawns_scope_returns_200(self):
        resp = await self._call("spawns")
        self.assertEqual(resp.status, 200)

    async def test_logs_scope_returns_200(self):
        resp = await self._call("logs")
        self.assertEqual(resp.status, 200)
        self.assertEqual(_payload(resp)["scope"], "logs")

    async def test_chat_scope_preserves_alerts_and_feed(self):
        # Narrowed: clearing chat history must not wipe alerts or the feed.
        runtime.state["alerts"] = [{"id": "a1"}]
        runtime.state["log_feed"] = [{"type": "log", "message": "keep"}]
        resp = await self._call("chat")
        self.assertEqual(resp.status, 200)
        self.assertEqual(runtime.state["alerts"], [{"id": "a1"}])
        self.assertEqual(runtime.state["log_feed"], [{"type": "log", "message": "keep"}])

    async def test_metrics_scope_clears_alerts_and_feed(self):
        runtime.state["alerts"] = [{"id": "a1"}]
        runtime.state["log_feed"] = [{"type": "log", "message": "old"}]
        resp = await self._call("metrics")
        self.assertEqual(resp.status, 200)
        self.assertEqual(runtime.state["alerts"], [])
        self.assertEqual(runtime.state["log_feed"], [])

    async def test_logs_scope_clears_log_feed(self):
        # Truncating the log files should also drop the in-memory activity-feed
        # buffer so a refreshed client doesn't replay stale lines.
        runtime.state["log_feed"] = [{"type": "log", "message": "old"}]
        resp = await self._call("logs")
        self.assertEqual(resp.status, 200)
        self.assertEqual(runtime.state["log_feed"], [])

    async def test_metrics_scope_zeroes_live_actor_counters(self):
        # reset_metrics only clears the persisted kv; the handler must also zero
        # the running actors' in-memory counters so the dashboard updates live
        # instead of waiting for a restart.

        actor = types.SimpleNamespace(
            name="alpha",
            metrics=types.SimpleNamespace(messages_processed=42),
            total_cost_usd=1.23,
            total_input_tokens=100,
            total_output_tokens=200,
        )
        fake_registry = MagicMock()
        fake_registry.all_actors.return_value = [actor]
        req = _make_request({"scope": "metrics"})
        orig_registry = runtime.registry
        runtime.registry = fake_registry
        try:
            with (
                patch("wactorz.reset.reset_metrics"),
                patch("wactorz.web.ws.broadcast", new=AsyncMock()),
                patch("wactorz.web.events.snapshot", return_value={}),
            ):
                resp = await api_reset.reset_handler(req)
            self.assertEqual(resp.status, 200)
            self.assertEqual(actor.metrics.messages_processed, 0)
            self.assertEqual(actor.total_cost_usd, 0.0)
            self.assertEqual(actor.total_input_tokens, 0)
            self.assertEqual(actor.total_output_tokens, 0)
        finally:
            runtime.registry = orig_registry

    async def test_metrics_scope_clears_in_memory_lifetime_ledger(self):
        # The headline total is max(live+historical, lifetime ledger). The kv
        # ledger is cleared by reset_metrics, but the in-memory _lifetime_cost
        # high-water survives in-process and pins the headline — the handler must
        # clear it too, or the total only drops by the live component.

        orig_registry = runtime.registry
        orig_ledger = dict(cost.lifetime_cost)
        runtime.registry = None
        cost.lifetime_cost.clear()
        cost.lifetime_cost.update({"id1": 5.0, "id2": 2.0})
        try:
            req = _make_request({"scope": "metrics"})
            with (
                patch("wactorz.reset.reset_metrics"),
                patch("wactorz.agents.llm_agent.reset_global_cost"),
                patch("wactorz.web.ws.broadcast", new=AsyncMock()),
                patch("wactorz.web.events.snapshot", return_value={}),
            ):
                resp = await api_reset.reset_handler(req)
            self.assertEqual(resp.status, 200)
            self.assertEqual(cost.lifetime_cost, {})
        finally:
            runtime.registry = orig_registry
            cost.lifetime_cost.clear()
            cost.lifetime_cost.update(orig_ledger)

    async def test_chat_scope_clears_live_conversation(self):
        # reset_chat only clears persisted chat; the handler must also wipe the
        # running actor's in-memory conversation so the agent stops "remembering"
        # without a restart.

        actor = types.SimpleNamespace(
            name="alpha",
            _conversation_history=[{"role": "user", "content": "hi"}],
            _history_summary="prior summary",
        )
        fake_registry = MagicMock()
        fake_registry.all_actors.return_value = [actor]
        req = _make_request({"scope": "chat"})
        orig_registry = runtime.registry
        runtime.registry = fake_registry
        try:
            with (
                patch("wactorz.reset.reset_chat"),
                patch("wactorz.web.ws.broadcast", new=AsyncMock()),
                patch("wactorz.web.events.snapshot", return_value={}),
            ):
                resp = await api_reset.reset_handler(req)
            self.assertEqual(resp.status, 200)
            self.assertEqual(actor._conversation_history, [])
            self.assertEqual(actor._history_summary, "")
        finally:
            runtime.registry = orig_registry

    async def test_chat_scope_respects_agent_filter(self):

        alpha = types.SimpleNamespace(name="alpha", _conversation_history=[1], _history_summary="a")
        beta = types.SimpleNamespace(name="beta", _conversation_history=[2], _history_summary="b")
        fake_registry = MagicMock()
        fake_registry.all_actors.return_value = [alpha, beta]
        req = _make_request({"scope": "chat", "agent": "alpha"})
        orig_registry = runtime.registry
        runtime.registry = fake_registry
        try:
            with (
                patch("wactorz.reset.reset_chat"),
                patch("wactorz.web.ws.broadcast", new=AsyncMock()),
                patch("wactorz.web.events.snapshot", return_value={}),
            ):
                resp = await api_reset.reset_handler(req)
            self.assertEqual(resp.status, 200)
            self.assertEqual(alpha._conversation_history, [])
            self.assertEqual(beta._conversation_history, [2])
        finally:
            runtime.registry = orig_registry

    async def test_metrics_scope_respects_agent_filter(self):
        # With an agent filter, only the matching actor's counters reset.

        alpha = types.SimpleNamespace(
            name="alpha", metrics=types.SimpleNamespace(messages_processed=10)
        )
        beta = types.SimpleNamespace(
            name="beta", metrics=types.SimpleNamespace(messages_processed=20)
        )
        fake_registry = MagicMock()
        fake_registry.all_actors.return_value = [alpha, beta]
        req = _make_request({"scope": "metrics", "agent": "alpha"})
        orig_registry = runtime.registry
        runtime.registry = fake_registry
        try:
            with (
                patch("wactorz.reset.reset_metrics"),
                patch("wactorz.web.ws.broadcast", new=AsyncMock()),
                patch("wactorz.web.events.snapshot", return_value={}),
            ):
                resp = await api_reset.reset_handler(req)
            self.assertEqual(resp.status, 200)
            self.assertEqual(alpha.metrics.messages_processed, 0)
            self.assertEqual(beta.metrics.messages_processed, 20)
        finally:
            runtime.registry = orig_registry

    async def test_all_scope_returns_200(self):
        resp = await self._call("all")
        self.assertEqual(resp.status, 200)

    async def test_response_includes_agent_when_provided(self):
        resp = await self._call("chat", agent="my-agent")
        self.assertEqual(_payload(resp)["agent"], "my-agent")

    async def test_response_agent_is_none_when_not_provided(self):
        resp = await self._call("chat")
        self.assertIsNone(_payload(resp)["agent"])


# ─────────────────────────────────────────────────────────────────────────────
# 4. reset_handler calls the right reset function per scope
# ─────────────────────────────────────────────────────────────────────────────


class ResetHandlerDispatchTest(unittest.IsolatedAsyncioTestCase):
    async def _call_with_mocks(self, scope: str, agent: str | None = None):

        req = _make_request({"scope": scope, "agent": agent})
        mocks = {}
        targets = {
            "reset_chat": "wactorz.reset.reset_chat",
            "reset_agent_state": "wactorz.reset.reset_agent_state",
            "reset_metrics": "wactorz.reset.reset_metrics",
            "reset_spawns": "wactorz.reset.reset_spawns",
            "reset_logs": "wactorz.reset.reset_logs",
            "reset_all": "wactorz.reset.reset_all",
            "_reset_all_pickles": "wactorz.reset._reset_all_pickles",
        }
        patches = [patch(t) for t in targets.values()]
        with patch("wactorz.web.ws.broadcast", new=AsyncMock()):
            started = [p.start() for p in patches]
            mocks = dict(zip(targets.keys(), started))
            try:
                await api_reset.reset_handler(req)
            finally:
                for p in patches:
                    p.stop()
        return mocks

    async def test_chat_scope_calls_reset_chat(self):
        mocks = await self._call_with_mocks("chat")
        mocks["reset_chat"].assert_called_once()

    async def test_metrics_scope_calls_reset_metrics(self):
        mocks = await self._call_with_mocks("metrics")
        mocks["reset_metrics"].assert_called_once()

    async def test_spawns_scope_calls_reset_spawns(self):
        mocks = await self._call_with_mocks("spawns")
        mocks["reset_spawns"].assert_called_once()

    async def test_logs_scope_calls_reset_logs(self):
        mocks = await self._call_with_mocks("logs")
        mocks["reset_logs"].assert_called_once()

    async def test_all_scope_calls_reset_all(self):
        mocks = await self._call_with_mocks("all")
        mocks["reset_all"].assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# 5. reset_spawns clears the AUTHORITATIVE kv-backed registry
#    (regression: "deleted agents keep coming back" after a wipe)
# ─────────────────────────────────────────────────────────────────────────────


class ResetSpawnsKvRegistryTest(unittest.TestCase):
    """main reads its spawn registry from kv_store("main", "_spawned_agents").

    reset_spawns must clear THAT, not only the vestigial spawn_registry table,
    or _restore_spawned_agents() re-spawns every deleted agent on restart.
    """

    def _db(self, tmp: str):
        from wactorz.core.persistence import WactorzDB

        return WactorzDB(str(Path(tmp) / "wactorz.db"))

    def test_all_clears_kv_spawn_registry(self):
        from wactorz.reset import reset_spawns

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "wactorz.db")
            db = self._db(tmp)
            db.kv_set("main", "_spawned_agents", {"a": {"node": ""}, "b": {"node": "n1"}})
            reset_spawns(db_path=db_path)
            self.assertIsNone(db.kv_get("main", "_spawned_agents", None))

    def test_single_agent_pops_only_that_entry(self):
        from wactorz.reset import reset_spawns

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "wactorz.db")
            db = self._db(tmp)
            db.kv_set("main", "_spawned_agents", {"a": {"node": ""}, "b": {"node": "n1"}})
            reset_spawns(agent_name="a", db_path=db_path)
            reg = db.kv_get("main", "_spawned_agents", None)
            self.assertEqual(reg, {"b": {"node": "n1"}})

    def test_restart_read_sees_empty_registry_after_wipe(self):
        """End-to-end of the actual symptom: after a wipe, the registry read
        that main._restore_spawned_agents() performs on the NEXT startup must
        return empty — i.e. recall("_spawned_agents") via a fresh PersistenceAPI
        bound to the same db file sees nothing, so nothing is re-spawned.
        """
        from wactorz.core.persistence import (
            PersistenceAPI,
            PickleStore,
            RedisStore,
        )
        from wactorz.reset import reset_all

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "wactorz.db")
            db = self._db(tmp)
            api = PersistenceAPI(db, RedisStore(), PickleStore(tmp), "main")
            api.set("_spawned_agents", {"ghost1": {"node": ""}, "ghost2": {"node": "n1"}})
            self.assertTrue(api.get("_spawned_agents"))  # sanity

            reset_all(db_path=db_path, state_dir=tmp)

            fresh = PersistenceAPI(self._db(tmp), RedisStore(), PickleStore(tmp), "main")
            self.assertFalse(fresh.get("_spawned_agents") or {})


# ─────────────────────────────────────────────────────────────────────────────
# 6. reset_handler "all" purges retained nodes/{node}/desired_state
#    (regression: a reconnecting runner reconciles deleted agents back)
# ─────────────────────────────────────────────────────────────────────────────


class ResetHandlerDesiredStatePurgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_scope_clears_desired_state_for_all_nodes(self):

        main = MagicMock()
        main.protected = True
        main.name = "main"
        main._get_spawn_registry.return_value = {"agentX": {"node": "n1"}}
        main.recall.return_value = None  # no in-memory registry attr to clear

        fake_registry = MagicMock()
        fake_registry.all_actors.return_value = [main]
        fake_registry.find_by_name.return_value = main
        fake_registry._supervisor_ref = None

        mqtt = AsyncMock()

        orig_registry, orig_mqtt = runtime.registry, runtime.mqtt_client_ref
        orig_nodes = dict(runtime.state["nodes"])
        runtime.registry = fake_registry
        runtime.mqtt_client_ref = mqtt
        runtime.state["nodes"] = {"n2": {"node": "n2"}}
        try:
            req = _make_request({"scope": "all"})
            with (
                patch("wactorz.reset.reset_all"),
                patch("wactorz.web.ws.broadcast", new=AsyncMock()),
                patch("wactorz.web.events.snapshot", return_value={}),
            ):
                resp = await api_reset.reset_handler(req)
            self.assertEqual(resp.status, 200)

            desired_topics = {
                c.args[0]
                for c in mqtt.publish.call_args_list
                if c.args and str(c.args[0]).endswith("/desired_state")
            }
            self.assertIn("nodes/n1/desired_state", desired_topics)
            self.assertIn("nodes/n2/desired_state", desired_topics)
            for c in mqtt.publish.call_args_list:
                if c.args and str(c.args[0]).endswith("/desired_state"):
                    self.assertEqual(c.args[1], b"")
                    self.assertTrue(c.kwargs.get("retain"))
        finally:
            runtime.registry = orig_registry
            runtime.mqtt_client_ref = orig_mqtt
            runtime.state["nodes"] = orig_nodes

    async def test_all_scope_never_touches_protected_agents(self):
        # Regression: the dashboard entry's "protected" flag is only set when a
        # heartbeat carried it, so trusting it tore down system agents (main /
        # monitor / installer / catalog). The registry's protected flag is
        # authoritative and must shield them — no retained purge, no tombstone.

        main = MagicMock()
        main.protected = True
        main.name = "main"
        main.actor_id = "main-id"
        main.recall.return_value = None
        main._get_spawn_registry.return_value = {}
        io = MagicMock()
        io.protected = False
        io.name = "io-agent"
        io.actor_id = "io-id"
        io.stop = AsyncMock()

        fake_registry = MagicMock()
        fake_registry.all_actors.return_value = [main, io]
        fake_registry.find_by_name.return_value = main
        fake_registry.unregister = AsyncMock()
        fake_registry._supervisor_ref = None

        orig_registry, orig_mqtt = runtime.registry, runtime.mqtt_client_ref
        orig_nodes, orig_agents = dict(runtime.state["nodes"]), dict(runtime.state["agents"])
        orig_deleted = list(runtime.deleted_agent_ids)
        runtime.registry = fake_registry
        runtime.mqtt_client_ref = AsyncMock()
        runtime.state["nodes"] = {}
        # Dashboard entries WITHOUT the protected flag — the exact bug condition.
        runtime.state["agents"] = {"main-id": {"name": "main"}, "io-id": {"name": "io-agent"}}
        runtime.deleted_agent_ids = []
        try:
            with (
                patch("wactorz.reset.reset_all"),
                patch("wactorz.web.lifecycle.purge_agent_retained", new=AsyncMock()) as purge,
                patch("wactorz.web.ws.broadcast", new=AsyncMock()),
                patch("wactorz.web.events.snapshot", return_value={}),
            ):
                resp = await api_reset.reset_handler(_make_request({"scope": "all"}))
            self.assertEqual(resp.status, 200)
            purged = {c.args[0] for c in purge.call_args_list}
            self.assertIn("io-id", purged)
            self.assertNotIn("main-id", purged)  # protected: never purged
            tombstoned = {aid for aid, _ in runtime.deleted_agent_ids}
            self.assertIn("io-id", tombstoned)
            self.assertNotIn("main-id", tombstoned)  # protected: never tombstoned
        finally:
            runtime.registry = orig_registry
            runtime.mqtt_client_ref = orig_mqtt
            runtime.state["nodes"] = orig_nodes
            runtime.state["agents"] = orig_agents
            runtime.deleted_agent_ids = orig_deleted


class FactoryResetKeepSetTest(unittest.TestCase):
    """`reset all` (factory reset) keeps the fresh-boot set — protected system
    actors plus the HA system agents — and wipes user/catalog-spawned agents."""

    def test_protected_system_actors_are_kept(self):

        for name in ("main", "monitor", "io-agent", "installer", "catalog"):
            self.assertTrue(api_reset.survives_factory_reset(name, True), name)

    def test_ha_system_agents_kept_despite_being_unprotected(self):

        # Unprotected → still individually deletable, but a factory reset keeps them.
        for name in api_reset.HA_SYSTEM_AGENTS:
            self.assertTrue(api_reset.survives_factory_reset(name, False), name)

    def test_user_and_catalog_spawned_agents_are_wiped(self):

        self.assertFalse(api_reset.survives_factory_reset("weather-agent", False))
        self.assertFalse(api_reset.survives_factory_reset("my-catalog-spawn", False))

    def test_keep_set_covers_every_app_py_supervised_agent(self):
        """Drift guard: every fresh-boot agent (app.py .supervise chain) must be
        kept. If a new .supervise("x") is added, either mark it protected or add
        it to HA_SYSTEM_AGENTS — otherwise a factory reset would wipe it."""
        import re

        app_src = Path(__file__).resolve().parents[1] / "wactorz" / "app.py"
        supervised = re.findall(r'supervise\(\s*"([^"]+)"', app_src.read_text())
        self.assertGreaterEqual(len(supervised), 8, "app.py supervise chain not found")
        # Protection lives on the actor class; treat the known protected names as
        # protected here so the guard checks name-coverage, not the flag itself.
        protected = {"main", "monitor", "io-agent", "installer", "catalog"}
        missing = [n for n in supervised if not api_reset.survives_factory_reset(n, n in protected)]
        self.assertEqual(missing, [], f"fresh-boot agents not kept by reset: {missing}")


if __name__ == "__main__":
    unittest.main()
