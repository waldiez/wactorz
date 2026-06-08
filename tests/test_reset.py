"""
Tests for wactorz.reset and the /api/reset HTTP handler.

Covers:
  1. reset_logs() — truncates files offline
  2. reset_handler scope validation
  3. reset_handler response shape for every valid scope
  4. reset_handler per-agent filter is forwarded
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ── Minimal stubs so aiohttp / mqtt deps are not required ────────────────────

def _install_stubs() -> None:
    for mod in ("aiomqtt", "openai", "websockets"):
        sys.modules.setdefault(mod, types.ModuleType(mod))


_install_stubs()


def _make_aiohttp_mod() -> types.ModuleType:
    """Build a fresh aiohttp ModuleType stub each time.

    Using a proper ModuleType (not SimpleNamespace) is required so that
    ``from aiohttp import web`` inside handler functions succeeds — Python
    needs the module object to have a ``__name__`` attribute.  Other test
    files may replace ``sys.modules["aiohttp"]`` with a SimpleNamespace
    between test runs, so handler tests install their own stub via
    ``patch.dict`` in setUp/tearDown instead of relying on module-level state.
    """

    class _Response:
        def __init__(self, *, body=b"", headers=None, content_type=None, status=200):
            self.body = body
            self.status = status
            self.headers = dict(headers or {})

    mod = types.ModuleType("aiohttp")
    mod.web = types.SimpleNamespace(  # type: ignore[attr-defined]
        json_response=lambda data, **kw: types.SimpleNamespace(
            data=data, status=kw.get("status", 200)
        ),
        Response=_Response,
        middleware=lambda fn: fn,
        HTTPException=type("HTTPException", (Exception,), {"status": 500}),
    )
    return mod


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
        import logging
        from wactorz.reset import reset_logs
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "wactorz.log"
            handler = logging.FileHandler(str(log_path))
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


# ─────────────────────────────────────────────────────────────────────────────
# 2 & 3. reset_handler — scope validation and response shape
# ─────────────────────────────────────────────────────────────────────────────

def _make_request(body: dict):
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    return req


class ResetHandlerScopeValidationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._aiohttp_patcher = patch.dict(sys.modules, {"aiohttp": _make_aiohttp_mod()})
        self._aiohttp_patcher.start()

    def tearDown(self):
        self._aiohttp_patcher.stop()

    async def test_invalid_scope_returns_400(self):
        import wactorz.monitor_server as ms
        req = _make_request({"scope": "invalid"})
        resp = await ms.reset_handler(req)
        self.assertEqual(resp.status, 400)
        self.assertIn("scope", str(resp.data))

    async def test_empty_scope_returns_400(self):
        import wactorz.monitor_server as ms
        req = _make_request({"scope": ""})
        resp = await ms.reset_handler(req)
        self.assertEqual(resp.status, 400)

    async def test_missing_scope_returns_400(self):
        import wactorz.monitor_server as ms
        req = _make_request({})
        resp = await ms.reset_handler(req)
        self.assertEqual(resp.status, 400)

    async def test_invalid_json_returns_400(self):
        import wactorz.monitor_server as ms
        req = MagicMock()
        req.json = AsyncMock(side_effect=Exception("bad json"))
        resp = await ms.reset_handler(req)
        self.assertEqual(resp.status, 400)


VALID_SCOPES = ("chat", "state", "metrics", "spawns", "logs", "all")


class ResetHandlerValidScopesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._aiohttp_patcher = patch.dict(sys.modules, {"aiohttp": _make_aiohttp_mod()})
        self._aiohttp_patcher.start()
        import wactorz.monitor_server as ms
        self._ms = ms
        self._orig_state = {
            "alerts": list(ms.state.get("alerts", [])),
            "log_feed": list(ms.state.get("log_feed", [])),
        }

    def tearDown(self):
        self._ms.state["alerts"] = self._orig_state["alerts"]
        self._ms.state["log_feed"] = self._orig_state["log_feed"]
        self._aiohttp_patcher.stop()

    async def _call(self, scope: str, agent: str | None = None) -> object:
        import wactorz.monitor_server as ms
        body: dict = {"scope": scope}
        if agent is not None:
            body["agent"] = agent
        req = _make_request(body)

        # Patch the individual reset functions so no FS/DB access is needed
        with patch("wactorz.reset.reset_chat"), \
             patch("wactorz.reset.reset_agent_state"), \
             patch("wactorz.reset.reset_metrics"), \
             patch("wactorz.reset.reset_spawns"), \
             patch("wactorz.reset.reset_logs"), \
             patch("wactorz.reset.reset_all"), \
             patch("wactorz.reset._reset_all_pickles"), \
             patch.object(ms, "broadcast", new=AsyncMock()):
            return await ms.reset_handler(req)

    async def test_chat_scope_returns_200(self):
        resp = await self._call("chat")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.data["status"], "ok")
        self.assertEqual(resp.data["scope"], "chat")

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
        self.assertEqual(resp.data["scope"], "logs")

    async def test_all_scope_returns_200(self):
        resp = await self._call("all")
        self.assertEqual(resp.status, 200)

    async def test_response_includes_agent_when_provided(self):
        resp = await self._call("chat", agent="my-agent")
        self.assertEqual(resp.data["agent"], "my-agent")

    async def test_response_agent_is_none_when_not_provided(self):
        resp = await self._call("chat")
        self.assertIsNone(resp.data["agent"])


# ─────────────────────────────────────────────────────────────────────────────
# 4. reset_handler calls the right reset function per scope
# ─────────────────────────────────────────────────────────────────────────────

class ResetHandlerDispatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Pre-import wactorz.reset before patch.dict snapshots sys.modules.
        # On Python 3.10, patch.dict.stop() restores sys.modules from the snapshot;
        # if wactorz.reset is absent from the snapshot, tearDown removes it, leaving
        # a stale package attribute.  Subsequent patch() calls then target the stale
        # module while the handler imports a fresh one, so mocks are never seen.
        import wactorz.reset  # noqa: F401
        self._aiohttp_patcher = patch.dict(sys.modules, {"aiohttp": _make_aiohttp_mod()})
        self._aiohttp_patcher.start()

    def tearDown(self):
        self._aiohttp_patcher.stop()

    async def _call_with_mocks(self, scope: str, agent: str | None = None):
        import wactorz.monitor_server as ms
        req = _make_request({"scope": scope, "agent": agent})
        mocks = {}
        targets = {
            "reset_chat":        "wactorz.reset.reset_chat",
            "reset_agent_state": "wactorz.reset.reset_agent_state",
            "reset_metrics":     "wactorz.reset.reset_metrics",
            "reset_spawns":      "wactorz.reset.reset_spawns",
            "reset_logs":        "wactorz.reset.reset_logs",
            "reset_all":         "wactorz.reset.reset_all",
            "_reset_all_pickles":"wactorz.reset._reset_all_pickles",
        }
        patches = [patch(t) for t in targets.values()]
        with patch.object(ms, "broadcast", new=AsyncMock()):
            started = [p.start() for p in patches]
            mocks = dict(zip(targets.keys(), started))
            try:
                await ms.reset_handler(req)
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


if __name__ == "__main__":
    unittest.main()
