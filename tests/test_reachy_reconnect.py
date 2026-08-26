"""
Tests for the reachy-mini ``reconnect`` command.

``setup()`` opens the robot link exactly once. When the robot is powered on
*after* the agent spawned, the handle stays ``None`` for the agent's whole life
and every robot command refuses — previously the only fix was restarting the
agent. ``cmd=reconnect`` re-runs the same connection ladder in place.

What we guard:
- ``reconnect`` re-opens the link and brings the robot back up (wake) when the
  robot appears after spawn.
- A still-failing reconnect reports ``ok:false`` with the retry hint — it must
  never publish a success envelope for a robot that is still offline.
- ``reconnect`` is reachable while disconnected (the offline fail-fast that
  refuses robot commands must exempt it) and via plain "reconnect" chat text,
  so it never falls through to the main bridge, which has no robot state and
  would answer with an invented "connection re-established".
- An already-connected agent doesn't re-open unless forced, and a stale handle
  is released before a new one is opened.
"""

import asyncio
import sys
import threading
import types
import unittest
from unittest import mock

from wactorz.catalogue_agents.reachy_mini_agent import AGENT_CODE


def _load_recipe_namespace():
    ns: dict = {}
    exec(compile(AGENT_CODE, "reachy_mini_agent<AGENT_CODE>", "exec"), ns)
    return ns


NS = _load_recipe_namespace()


_FAKE_REACHY_SDK = None


def setUpModule():
    """Provide a minimal SDK module while testing reconnect without hardware."""
    global _FAKE_REACHY_SDK
    try:
        __import__("reachy_mini")
    except ImportError:
        # ImportError rather than ModuleNotFoundError: the SDK can be installed
        # and still fail to import, because it pulls in bindings of its own that
        # need not work on this interpreter. Only its metadata is used here, so
        # an SDK that will not load is the same situation as an absent one.
        sdk = types.ModuleType("reachy_mini")
        sdk.ReachyMini = mock.Mock()  # pyright: ignore[reportAttributeAccessIssue]
        _FAKE_REACHY_SDK = sdk
        sys.modules["reachy_mini"] = sdk


def tearDownModule():
    if _FAKE_REACHY_SDK is not None and sys.modules.get("reachy_mini") is _FAKE_REACHY_SDK:
        sys.modules.pop("reachy_mini", None)


class FakeAgent:
    """Just enough of the actor surface for the reconnect path."""

    def __init__(self, mini=None, robot_host="192.168.68.64", connection_mode="network"):
        self.state = {
            "mini": mini,
            "robot_host": robot_host,
            "media_backend": "",
            "connection_mode": connection_mode,
            "last_connect_error": None if mini else "connection refused",
            "volume_level": 100,
            "motors_enabled": False,
            "awake": False,
            "last_cmd": None,
        }
        self.published: list[tuple[str, object]] = []
        self.logs: list[str] = []
        self.alerts: list[str] = []
        self.persisted: dict = {}

    async def publish(self, topic, payload):
        self.published.append((topic, payload))

    async def log(self, msg, level="info"):
        self.logs.append(msg)

    async def alert(self, msg, severity="info"):
        self.alerts.append(msg)

    def recall(self, key, default=None):
        return self.persisted.get(key, default)

    def persist(self, key, value):
        self.persisted[key] = value

    def events(self):
        return [p for t, p in self.published if t == "custom/reachy/events"]


def _run(coro):
    return asyncio.run(coro)


def _fake_mini():
    """A live-looking SDK handle: connected, wakes, exits cleanly."""
    mini = types.SimpleNamespace()
    mini.connected = True
    mini.wake_up = mock.Mock()
    mini.__exit__ = mock.Mock(return_value=False)
    mini.media = types.SimpleNamespace(
        audio=types.SimpleNamespace(
            daemon_url="",
            apply_audio_config=mock.Mock(return_value=True),
        )
    )
    return mini


class ReconnectCommandTest(unittest.TestCase):
    def test_remote_conversation_audio_config_uses_daemon(self):
        agent = FakeAgent(mini=_fake_mini())
        agent.state["mini"].media.audio.daemon_url = "http://robot/"
        response = mock.Mock()
        response.json.return_value = {"applied": True}

        with mock.patch("requests.post", return_value=response) as post:
            configured = _run(NS["_configure_conversation_audio"](agent))

        self.assertTrue(configured)
        self.assertTrue(agent.state["conversation_echo_control"])
        response.raise_for_status.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://robot/api/audio/config/apply")
        self.assertTrue(kwargs["json"]["verify"])
        params = {item["name"]: item["values"] for item in kwargs["json"]["config"]}
        self.assertEqual(params["PP_AGCMAXGAIN"], [10.0])
        self.assertEqual(params["PP_MGSCALE"], [4.0, 1.0, 1.0])
        self.assertNotIn("PP_NLATTENONOFF", params)

    def test_failed_audio_config_disables_automatic_barge_in(self):
        agent = FakeAgent(mini=_fake_mini())
        agent.state["mini"].media.audio.daemon_url = "http://robot"

        with mock.patch("requests.post", side_effect=OSError("no endpoint")):
            configured = _run(NS["_configure_conversation_audio"](agent))

        self.assertFalse(configured)
        self.assertFalse(agent.state["conversation_echo_control"])
        self.assertTrue(
            any("automatic voice interruption is disabled" in line for line in agent.logs)
        )

    def test_reconnect_opens_link_and_brings_robot_up(self):
        agent = FakeAgent(mini=None)
        mini = _fake_mini()

        async def fake_open(_agent):
            return mini, None, "host=192.168.68.64+network"

        with mock.patch.dict(NS, {"_open_robot": fake_open}):
            res = _run(NS["_dispatch"](agent, "reconnect", {}, return_result=True))

        self.assertTrue(res["ok"])
        self.assertTrue(res["connected"])
        self.assertTrue(res["reconnected"])
        self.assertIs(agent.state["mini"], mini)
        # Brought up, not just connected: the robot is awake and ready.
        mini.wake_up.assert_called_once()
        self.assertTrue(agent.state["awake"])
        self.assertIn("Reconnected", res["result"])

    def test_failed_reconnect_reports_not_ok_with_retry_hint(self):
        agent = FakeAgent(mini=None)

        async def fake_open(_agent):
            return None, OSError("connection refused"), "host=192.168.68.64+network"

        with mock.patch.dict(NS, {"_open_robot": fake_open}):
            res = _run(NS["_dispatch"](agent, "reconnect", {}, return_result=True))

        self.assertFalse(res["ok"])
        self.assertIsNone(agent.state["mini"])
        self.assertIn("connection refused", res["error"])
        self.assertIn("reconnect", res["result"].lower())
        # The event envelope must not claim success for an offline robot.
        events = agent.events()
        self.assertTrue(events)
        self.assertFalse(events[-1]["ok"])
        self.assertEqual(events[-1]["type"], "reconnect")

    def test_reconnect_clears_reconnecting_flag_after_failure(self):
        agent = FakeAgent(mini=None)

        async def fake_open(_agent):
            return None, OSError("nope"), "autodetect"

        with mock.patch.dict(NS, {"_open_robot": fake_open}):
            _run(NS["_dispatch"](agent, "reconnect", {}, return_result=True))
        # A failed attempt must not wedge the agent into "already in progress".
        self.assertFalse(agent.state.get("_reconnecting"))

    def test_already_connected_does_not_reopen(self):
        mini = _fake_mini()
        agent = FakeAgent(mini=mini)
        opened = []

        async def fake_open(_agent):
            opened.append(True)
            return _fake_mini(), None, "x"

        with mock.patch.dict(NS, {"_open_robot": fake_open}):
            res = _run(NS["_dispatch"](agent, "reconnect", {}, return_result=True))

        self.assertEqual(opened, [])
        self.assertFalse(res["reconnected"])
        self.assertTrue(res["connected"])
        self.assertIs(agent.state["mini"], mini)

    def test_force_reopens_and_releases_stale_handle(self):
        stale = _fake_mini()
        agent = FakeAgent(mini=stale)
        fresh = _fake_mini()

        async def fake_open(_agent):
            return fresh, None, "x"

        with mock.patch.dict(NS, {"_open_robot": fake_open}):
            res = _run(NS["_dispatch"](agent, "reconnect", {"force": True}, return_result=True))

        self.assertTrue(res["reconnected"])
        self.assertIs(agent.state["mini"], fresh)
        # The old websocket/media objects are released, not leaked.
        stale.__exit__.assert_called_once()


class ReconnectReachabilityTest(unittest.TestCase):
    """reconnect must survive the guards that refuse robot commands offline."""

    def test_handle_task_reconnect_text_is_not_bridged_to_main(self):
        agent = FakeAgent(mini=None)
        mini = _fake_mini()
        bridged = []

        async def fake_open(_agent):
            return mini, None, "host=192.168.68.64+network"

        async def fake_bridge(_agent, text, _tid):
            bridged.append(text)
            return {"ok": True, "result": "I'm here! Connection re-established."}

        with mock.patch.dict(NS, {"_open_robot": fake_open, "_bridge_to_main": fake_bridge}):
            res = _run(NS["handle_task"](agent, {"text": "reconnect"}))

        # The bug this guards: "reconnect" reaching main, which has no robot
        # state and fabricates a reconnection that never happened.
        self.assertEqual(bridged, [])
        self.assertTrue(res["reconnected"])
        self.assertIs(agent.state["mini"], mini)

    def test_offline_fail_fast_exempts_reconnect(self):
        agent = FakeAgent(mini=None)
        mini = _fake_mini()

        async def fake_open(_agent):
            return mini, None, "x"

        with mock.patch.dict(NS, {"_open_robot": fake_open}):
            res = _run(NS["handle_task"](agent, {"cmd": "reconnect"}))

        self.assertTrue(res["ok"])
        self.assertTrue(res["reconnected"])

    def test_offline_fail_fast_still_refuses_other_robot_commands(self):
        agent = FakeAgent(mini=None)
        res = _run(NS["handle_task"](agent, {"cmd": "wake"}))
        self.assertFalse(res["ok"])
        # And it tells the user how to recover, rather than "restart the agent".
        self.assertIn("reconnect", res["result"].lower())


class DisconnectedMessageTest(unittest.TestCase):
    def test_no_handle_message_points_at_reconnect(self):
        agent = FakeAgent(mini=None)
        ok, reason = NS["_is_connected"](agent)
        self.assertFalse(ok)
        self.assertIn("reconnect", reason.lower())
        self.assertNotIn("restart the agent", reason.lower())

    def test_dropped_link_message_points_at_reconnect(self):
        mini = _fake_mini()
        mini.connected = False
        agent = FakeAgent(mini=mini)
        ok, reason = NS["_is_connected"](agent)
        self.assertFalse(ok)
        self.assertIn("reconnect", reason.lower())


if __name__ == "__main__":
    unittest.main()


class ConfigThenReconnectTest(unittest.TestCase):
    """The config topic is live; .env is read once at spawn. Reconnect must
    pick up a host published to custom/reachy/config without a restart —
    that's what the failure hint tells users to do."""

    def test_reconnect_uses_host_updated_via_config(self):
        agent = FakeAgent(mini=None, robot_host="", connection_mode="auto")
        seen = {}

        def fake_build(host, backend, mode):
            seen["host"], seen["mode"] = host, mode
            return [{"host": host, "connection_mode": mode}]

        # Simulate the config topic having landed a new host + mode.
        agent.state["robot_host"] = "192.168.68.64"
        agent.state["connection_mode"] = "network"

        mini = _fake_mini()
        fake_sdk = types.ModuleType("reachy_mini")
        fake_sdk.ReachyMini = mock.Mock(
            return_value=types.SimpleNamespace(__enter__=lambda *_: mini)
        )

        with (
            mock.patch.dict("sys.modules", {"reachy_mini": fake_sdk}),
            mock.patch.dict(NS, {"_build_connection_attempts": fake_build}),
        ):
            got, err, _tried = _run(NS["_open_robot"](agent))

        self.assertEqual(seen["host"], "192.168.68.64")
        self.assertEqual(seen["mode"], "network")
        self.assertIs(got, mini)
        self.assertIsNone(err)

    def test_auto_sentinel_maps_to_empty_mode_for_the_ladder(self):
        # state stores "auto" for "no explicit mode"; _build_connection_attempts
        # expects "" — a leaked "auto" would fall through to its network branch.
        agent = FakeAgent(mini=None, robot_host="", connection_mode="auto")
        seen = {}

        def fake_build(host, backend, mode):
            seen["mode"] = mode
            return []

        with mock.patch.dict(NS, {"_build_connection_attempts": fake_build}):
            _run(NS["_open_robot"](agent))
        self.assertEqual(seen["mode"], "")


class ConcurrentReconnectTest(unittest.TestCase):
    def test_second_reconnect_is_rejected_while_one_is_in_flight(self):
        """Two reconnects racing must not both open a link.

        The in-progress guard and the flag-set must not be separated by an
        await. This drives the exact window: the first reconnect is suspended
        inside the awaited stale-handle release (a real executor call) when the
        second one checks the guard. If the flag were set after that release,
        the second attempt would sail past the check and open a second link.
        """
        stale = _fake_mini()
        stale.connected = False  # dropped link -> reconnect proceeds
        release_started = threading.Event()
        allow_release = threading.Event()

        def blocking_exit(*_a):
            release_started.set()
            allow_release.wait(timeout=5)  # hold the first attempt mid-release
            return False

        stale.__exit__ = blocking_exit
        agent = FakeAgent(mini=stale)
        opens = []

        async def fake_open(_agent):
            opens.append(True)
            return _fake_mini(), None, "x"

        async def scenario():
            with mock.patch.dict(NS, {"_open_robot": fake_open}):
                first = asyncio.create_task(
                    NS["_dispatch"](agent, "reconnect", {}, return_result=True)
                )
                # Wait until the first attempt is genuinely parked in the release.
                while not release_started.is_set():
                    await asyncio.sleep(0.01)
                second = await NS["_dispatch"](agent, "reconnect", {}, return_result=True)
                allow_release.set()
                return await first, second

        first, second = _run(scenario())

        self.assertEqual(
            len(opens), 1, "a second link was opened — the in-progress guard let a racer through"
        )
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertIn("already in progress", second["error"])
