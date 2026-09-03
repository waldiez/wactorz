"""What Reachy can say about its own condition, and what it cannot.

The robot reports one thing about its health that is worth warning about: the
motors' hardware-error byte, which names overheating among other faults. The
daemon reads it several times a second and writes it to its log and nowhere
else — not into the status the SDK can read — so the log stream is the only way
to see it, and it is where the official control app gets its overheating
warning too.

There is no battery telemetry at all in the SDK, so `health` says so rather than
leaving an absent number to be read as a full charge.
"""

import asyncio
import types
import unittest

from wactorz.catalogue_agents.reachy_mini_agent import AGENT_CODE

NS = {}
exec(compile(AGENT_CODE, "reachy_mini_agent<AGENT_CODE>", "exec"), NS)

#: A fault line as journalctl actually delivers it, prefix and all.
JOURNAL_LINE = (
    "2026-08-18T21:40:02+0300 reachy reachy-mini-daemon[812]: ERROR "
    "Motor 'head_yaw' hardware errors: ['Overheating Error']"
)


class FakeAgent:
    name = "reachy-mini"

    def __init__(self, daemon_url=None):
        self.state = {}
        self.logs = []
        self.chat = []
        if daemon_url is not None:
            self.state["mini"] = types.SimpleNamespace(
                media=types.SimpleNamespace(audio=types.SimpleNamespace(daemon_url=daemon_url))
            )

    async def log(self, text, level="info"):
        self.logs.append((level, text))

    async def notify_user(self, text, **extra):
        self.chat.append(text)

    def run_in_background(self, coro):
        return asyncio.create_task(coro)


class ReadingFaultsFromTheLogTest(unittest.TestCase):
    def test_a_journalctl_line_is_understood_with_its_prefix(self):
        self.assertEqual(
            NS["_motor_faults_in_line"](JOURNAL_LINE), [("head_yaw", "overheating error")]
        )

    def test_several_faults_on_one_motor_are_all_read(self):
        line = "Motor 'antenna_left' hardware errors: ['Overload Error', 'Overheating Error']"

        self.assertEqual(
            NS["_motor_faults_in_line"](line),
            [("antenna_left", "overload error"), ("antenna_left", "overheating error")],
        )

    def test_an_ordinary_log_line_names_no_faults(self):
        for line in ("[Supervisor] Started.", "", None, "hardware errors: []"):
            with self.subTest(line=line):
                self.assertEqual(NS["_motor_faults_in_line"](line), [])

    def test_overheating_advice_says_what_to_do(self):
        described = NS["_describe_motor_fault"]("head_yaw", "overheating error")

        self.assertIn("head_yaw", described)
        self.assertIn("rest", described.lower())

    def test_a_fault_the_daemon_invents_is_still_reported(self):
        described = NS["_describe_motor_fault"]("head_yaw", "some new error")

        self.assertIn("head_yaw", described)
        self.assertIn("some new error", described)


class WarningWithoutNaggingTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_fault_is_warned_about_once_then_held(self):
        # The daemon re-reads the motors several times a second, so one hot
        # motor would otherwise be a warning per read.
        agent = FakeAgent()

        first = await NS["_report_motor_faults"](agent, JOURNAL_LINE, now=1000.0)
        immediately_again = await NS["_report_motor_faults"](agent, JOURNAL_LINE, now=1001.0)

        self.assertEqual(len(first), 1)
        self.assertEqual(immediately_again, [])
        self.assertEqual(len(agent.chat), 1)

    async def test_a_fault_that_is_still_there_later_is_raised_again(self):
        agent = FakeAgent()
        quiet = NS["_MOTOR_FAULT_QUIET_S"]

        await NS["_report_motor_faults"](agent, JOURNAL_LINE, now=1000.0)
        later = await NS["_report_motor_faults"](agent, JOURNAL_LINE, now=1000.0 + quiet + 1)

        self.assertEqual(len(later), 1)

    async def test_the_warning_reaches_chat_and_the_log(self):
        agent = FakeAgent()

        await NS["_report_motor_faults"](agent, JOURNAL_LINE, now=1000.0)

        self.assertTrue(any(level == "warning" for level, _ in agent.logs))
        self.assertIn("overheating", agent.chat[0].lower())

    async def test_a_different_motor_is_its_own_warning(self):
        agent = FakeAgent()
        other = "Motor 'antenna_right' hardware errors: ['Overheating Error']"

        await NS["_report_motor_faults"](agent, JOURNAL_LINE, now=1000.0)
        second = await NS["_report_motor_faults"](agent, other, now=1000.0)

        self.assertEqual(len(second), 1)


class TheExpectedVoltageFaultTest(unittest.IsolatedAsyncioTestCase):
    """Reachy Mini runs its motors above their own voltage threshold on purpose.

    Pollen document this in their FAQ: "We are using a higher voltage on Reachy
    Mini, it's on purpose". So the servos report an input-voltage fault on a
    perfectly healthy robot, and warning about it would mean crying wolf on
    every unit — which is how people learn to ignore the warnings that matter.
    """

    LINE = "Motor 'head_yaw' hardware errors: ['Input Voltage Error']"

    async def test_it_is_not_raised_at_the_user(self):
        agent = FakeAgent()

        reported = await NS["_report_motor_faults"](agent, self.LINE, now=1000.0)

        self.assertEqual(reported, [])
        self.assertEqual(agent.chat, [])

    async def test_it_is_still_written_to_the_log(self):
        # Quiet is not the same as hidden.
        agent = FakeAgent()

        await NS["_report_motor_faults"](agent, self.LINE, now=1000.0)

        self.assertTrue(any("motor note" in text for _level, text in agent.logs))
        self.assertFalse(any(level == "warning" for level, _ in agent.logs))

    async def test_a_real_fault_alongside_it_is_still_raised(self):
        agent = FakeAgent()
        both = "Motor 'head_yaw' hardware errors: ['Input Voltage Error', 'Overheating Error']"

        reported = await NS["_report_motor_faults"](agent, both, now=1000.0)

        self.assertEqual(len(reported), 1)
        self.assertIn("overheating", reported[0])


class WhereTheFaultStreamLivesTest(unittest.TestCase):
    def test_the_stream_url_is_derived_from_the_daemon(self):
        agent = FakeAgent(daemon_url="http://192.168.1.42:8000")

        self.assertEqual(NS["_daemon_log_ws_url"](agent), "ws://192.168.1.42:8000/logs/ws/daemon")

    def test_a_secure_daemon_gets_a_secure_stream(self):
        agent = FakeAgent(daemon_url="https://reachy.local:8000/")

        self.assertEqual(NS["_daemon_log_ws_url"](agent), "wss://reachy.local:8000/logs/ws/daemon")

    def test_no_robot_means_no_stream(self):
        # A Lite over USB serves no log stream; the daemon mounts that route
        # behind its wireless flag. Nothing to watch, and that is not an error.
        self.assertIsNone(NS["_daemon_log_ws_url"](FakeAgent()))


class HealthReportTest(unittest.IsolatedAsyncioTestCase):
    async def test_it_says_there_is_no_battery_rather_than_omitting_it(self):
        # An absent number would be read as a full charge.
        report = await NS["_health"](FakeAgent())

        self.assertIsNone(report["battery"])
        self.assertIn("battery", report["result"].lower())

    async def test_a_disconnected_robot_is_reported_as_such(self):
        report = await NS["_health"](FakeAgent())

        self.assertFalse(report["connected"])
        self.assertIn("not connected", report["result"].lower())

    async def test_faults_seen_this_session_are_listed(self):
        agent = FakeAgent(daemon_url="http://192.168.1.42:8000")
        await NS["_report_motor_faults"](agent, JOURNAL_LINE, now=1000.0)

        report = await NS["_health"](agent)

        self.assertIn("head_yaw/overheating error", report["motor_faults"])

    async def test_a_link_that_cannot_watch_says_so(self):
        # Silence from an unwatched robot must not read as "nothing wrong".
        agent = FakeAgent()
        agent.state["mini"] = object()

        report = await NS["_health"](agent)

        self.assertFalse(report["watching_faults"])
        self.assertIn("wireless", report["result"].lower())

    async def test_a_configured_but_disconnected_fault_watch_is_not_called_live(self):
        agent = FakeAgent(daemon_url="http://192.168.1.42:8000")

        report = await NS["_health"](agent)

        self.assertFalse(report["watching_faults"])
        self.assertTrue(report["fault_watch_configured"])
        self.assertIn("cannot currently read", report["result"].lower())

    async def test_a_live_fault_watch_can_truthfully_report_no_faults_seen(self):
        agent = FakeAgent(daemon_url="http://192.168.1.42:8000")
        agent.state["motor_fault_watch_connected"] = True

        report = await NS["_health"](agent)

        self.assertTrue(report["watching_faults"])
        self.assertIn("has not seen a fault", report["result"].lower())


class MotionLinkStatusTest(unittest.IsolatedAsyncioTestCase):
    def test_task_timeout_is_recognised_as_a_motor_link_problem(self):
        self.assertTrue(NS["_is_motion_link_error"]("Task did not complete in time"))

    def test_audio_errors_are_not_mistaken_for_motor_link_problems(self):
        self.assertFalse(NS["_is_motion_link_error"]("speaker volume is low"))

    async def test_the_imu_temperature_is_reported_when_the_robot_has_one(self):
        agent = FakeAgent()
        agent.state["mini"] = types.SimpleNamespace(get_imu_data=lambda: {"temperature": 41.27})

        self.assertEqual(await NS["_imu_temperature"](agent), 41.3)

    async def test_a_robot_without_an_imu_reports_no_temperature(self):
        agent = FakeAgent()
        agent.state["mini"] = types.SimpleNamespace(
            get_imu_data=lambda: (_ for _ in ()).throw(RuntimeError("no IMU"))
        )

        self.assertIsNone(await NS["_imu_temperature"](agent))


if __name__ == "__main__":
    unittest.main()
