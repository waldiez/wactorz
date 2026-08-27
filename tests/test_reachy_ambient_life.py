"""Ambient life, and the promise that it never takes the robot off you.

The layer exists so Reachy does not read as switched off while nobody is
talking to him. The risk it introduces is the opposite failure: motion that
fights a command, or drags him off a pose that was set deliberately. Most of
what follows is about that second failure, because a robot that ignores you is
worse than one that merely stands still.

The offset generators are pure functions, so the cases are driven directly
rather than through a robot.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import random
import types

import pytest

from wactorz.catalogue_agents.reachy_mini_agent import AGENT_CODE

NS: dict = {}
exec(compile(AGENT_CODE, "reachy_mini_agent<AGENT_CODE>", "exec"), NS)

HEAD_FIELDS = ("z", "pitch", "yaw", "roll", "body_yaw")


class FakeNumpy:
    """Just the two calls the motion path makes."""

    @staticmethod
    def deg2rad(values):
        if isinstance(values, (list, tuple)):
            return [math.radians(float(v)) for v in values]
        return math.radians(float(values))

    @staticmethod
    def rad2deg(value):
        return math.degrees(float(value))


class FakeAgent:
    """Records what reached the robot instead of moving one."""

    def __init__(self, **state):
        self.targets: list[dict] = []
        self.logs: list[tuple[str, str]] = []
        mini = types.SimpleNamespace(set_target=self._set_target)
        self.state = {
            "mini": mini,
            "np": FakeNumpy(),
            "create_head_pose": lambda **kw: dict(kw),
            "life_enabled": True,
            "life_amplitude": 1.0,
            "awake": True,
            "busy": False,
            "_speech_motion": False,
            "_life_base": NS["_life_neutral_base"](),
            "_life_base_known": True,
            "_life_antenna_base": (0.0, 0.0),
            "_facing_body_yaw_deg": 0.0,
        }
        self.state.update(state)

    def _set_target(self, **kw):
        self.targets.append(kw)

    async def log(self, text, level="info"):
        self.logs.append((level, text))


def offsets_at(elapsed, gaze=(0.0, 0.0), amplitude=1.0):
    return NS["_life_offsets"](elapsed, gaze, amplitude)


class TestNothingEverLeavesTheSafeEnvelope:
    """The layer runs unattended for hours beside the public."""

    @pytest.mark.parametrize("elapsed", [t * 0.37 for t in range(400)])
    def test_every_offset_stays_inside_its_ceiling(self, elapsed: float) -> None:
        # Gaze pushed to its own limit at the same time, so this is the
        # extreme the generator can produce rather than a typical frame.
        result = offsets_at(elapsed, gaze=(99.0, 99.0), amplitude=1.0)
        for field in HEAD_FIELDS:
            assert abs(result[field]) <= NS["_LIFE_MAX"][field] + 1e-9, (
                f"{field} left the envelope at t={elapsed}"
            )

    def test_an_absurd_amplitude_is_still_clamped(self) -> None:
        result = offsets_at(1.0, gaze=(0.0, 0.0), amplitude=1000.0)
        for field in HEAD_FIELDS:
            assert abs(result[field]) <= NS["_LIFE_MAX"][field] + 1e-9

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "x", None])
    def test_a_broken_number_becomes_zero_rather_than_a_command(self, bad) -> None:
        """A NaN reaching a servo is worse than a dropped frame."""
        assert NS["_life_clamp"]("yaw", bad) == 0.0

    def test_zero_amplitude_means_no_motion_at_all(self) -> None:
        result = offsets_at(3.3, gaze=(4.0, 1.0), amplitude=0.0)
        assert all(result[field] == 0.0 for field in HEAD_FIELDS)


class TestItDoesNotLookLikeALoop:
    def test_the_three_periods_share_no_short_common_multiple(self) -> None:
        """Harmonic periods make the sum visibly repeat, which reads as a machine."""
        periods = (
            NS["_LIFE_BREATH_PERIOD"],
            NS["_LIFE_BOB_PERIOD"],
            NS["_LIFE_SWAY_PERIOD"],
        )
        for i, first in enumerate(periods):
            for second in periods[i + 1 :]:
                ratio = max(first, second) / min(first, second)
                assert abs(ratio - round(ratio)) > 0.1, (
                    f"{first} and {second} are close to harmonic; the composite will repeat"
                )

    def test_the_pose_actually_changes_over_time(self) -> None:
        """A layer that emits a constant would pass every clamp test above."""
        samples = {tuple(round(offsets_at(t)[f], 3) for f in HEAD_FIELDS) for t in range(60)}
        assert len(samples) > 40, "ambient motion is barely moving"


class TestGazeHoldsAndThenMoves:
    def test_it_settles_rather_than_sweeping_continuously(self) -> None:
        """Continuous drift reads as scanning, and scanning reads as a task."""
        rng = random.Random(7)
        gaze = {"from": (0.0, 0.0), "to": (0.0, 0.0), "started": 0.0, "travel": 1.0, "until": 0.0}
        positions = [NS["_life_gaze_step"](gaze, t / 10.0, rng) for t in range(600)]
        still = sum(
            1
            for a, b in itertools.pairwise(positions)
            if abs(a[0] - b[0]) < 0.01 and abs(a[1] - b[1]) < 0.01
        )
        assert still > len(positions) * 0.4, "gaze never holds still; it is scanning, not idling"

    def test_gaze_targets_stay_within_the_yaw_ceiling(self) -> None:
        rng = random.Random(3)
        gaze = {"from": (0.0, 0.0), "to": (0.0, 0.0), "started": 0.0, "travel": 1.0, "until": 0.0}
        for tick in range(2000):
            yaw, pitch = NS["_life_gaze_step"](gaze, tick / 10.0, rng)
            assert abs(yaw) <= NS["_LIFE_MAX"]["yaw"] + 1e-9
            assert abs(pitch) <= NS["_LIFE_MAX"]["pitch"] + 1e-9


class TestAntennasDoNotTwin:
    def test_the_two_antennas_do_not_move_as_one(self) -> None:
        """Twinning reads as a mechanism rather than as a creature."""
        rng = random.Random(11)
        ant = {
            "from": (0.0, 0.0),
            "to": (0.0, 0.0),
            "started": 0.0,
            "travel": 1.0,
            "until": 0.0,
            "resting": True,
        }
        pairs = [NS["_life_antenna_step"](ant, t / 20.0, rng) for t in range(2000)]
        moving = [(left, right) for left, right in pairs if abs(left) > 0.5]
        assert moving, "antennas never moved"
        identical = sum(1 for left, right in moving if abs(left - right) < 0.2)
        assert identical < len(moving) * 0.1, "antennas are moving as a matched pair"


class TestItYieldsToEveryDeliberateCommand:
    """The requirement: ambient motion must never take the robot off you."""

    def test_a_command_in_flight_stops_it(self) -> None:
        agent = FakeAgent(busy=True)
        assert NS["_life_should_run"](agent) is False

    def test_speech_owns_the_head_while_he_talks(self) -> None:
        agent = FakeAgent(_speech_motion=True)
        assert NS["_life_should_run"](agent) is False

    def test_it_stays_off_when_not_enabled(self) -> None:
        agent = FakeAgent(life_enabled=False)
        assert NS["_life_should_run"](agent) is False

    def test_it_stays_still_while_asleep(self) -> None:
        agent = FakeAgent(awake=False)
        assert NS["_life_should_run"](agent) is False

    def test_it_stays_still_with_no_robot(self) -> None:
        agent = FakeAgent(mini=None)
        assert NS["_life_should_run"](agent) is False

    def test_an_idle_awake_robot_is_the_one_case_that_runs(self) -> None:
        assert NS["_life_should_run"](FakeAgent()) is True


class TestItBreathesAroundYourPoseRatherThanReplacingIt:
    def test_offsets_are_added_to_the_commanded_base(self) -> None:
        """The whole safety argument: this layer never names an absolute target."""
        agent = FakeAgent()
        agent.state["_life_base"] = {
            "x": 0.0,
            "y": 0.0,
            "z": 0.0,
            "roll": 0.0,
            "pitch": 0.0,
            "yaw": 30.0,  # you told him to look right
        }

        asyncio.run(
            NS["_life_apply"](
                agent,
                {"z": 2.0, "pitch": 1.0, "yaw": 4.0, "roll": 0.5, "body_yaw": 2.0},
                (3.0, -2.0),
            )
        )

        head = agent.targets[-1]["head"]
        assert head["yaw"] == 34.0, "ambient motion replaced the commanded yaw instead of adding"
        assert head["pitch"] == 1.0

    def test_a_new_pose_command_moves_the_base(self) -> None:
        agent = FakeAgent()
        NS["_note_base_pose"](agent, yaw=25.0, pitch=-4.0)

        assert agent.state["_life_base"]["yaw"] == 25.0
        assert agent.state["_life_base_known"] is True

    def test_body_sway_is_added_to_the_direction_he_is_facing(self) -> None:
        """He can be left facing backwards; sway must not walk him back to front."""
        agent = FakeAgent(_facing_body_yaw_deg=180.0)

        asyncio.run(
            NS["_life_apply"](
                agent,
                {"z": 0.0, "pitch": 0.0, "yaw": 0.0, "roll": 0.0, "body_yaw": 3.0},
                (0.0, 0.0),
            )
        )

        assert agent.targets[-1]["body_yaw"] == pytest.approx(math.radians(183.0))

    def test_antenna_offsets_are_added_to_their_own_base(self) -> None:
        agent = FakeAgent(_life_antenna_base=(20.0, 20.0))

        asyncio.run(
            NS["_life_apply"](
                agent,
                dict.fromkeys(HEAD_FIELDS, 0.0),
                (5.0, -5.0),
            )
        )

        # The SDK order is [right, left].
        right, left = agent.targets[-1]["antennas"]
        assert left == pytest.approx(math.radians(25.0))
        assert right == pytest.approx(math.radians(15.0))


class TestAnUnknowableBaseLeavesTheHeadAlone:
    """look_at and recorded clips end somewhere with no name in pose space."""

    def test_the_head_is_not_touched_once_the_base_is_unknown(self) -> None:
        agent = FakeAgent()
        NS["_forget_base_pose"](agent)

        asyncio.run(NS["_life_apply"](agent, dict.fromkeys(HEAD_FIELDS, 1.0), (4.0, -4.0)))

        sent = agent.targets[-1]
        assert "head" not in sent, (
            "breathing around a stale base drags him off the target look_at aimed at"
        )
        assert "body_yaw" not in sent

    def test_the_antennas_keep_their_pulse(self) -> None:
        """He holds the gaze he was told to hold, but is still visibly alive."""
        agent = FakeAgent()
        NS["_forget_base_pose"](agent)

        asyncio.run(NS["_life_apply"](agent, dict.fromkeys(HEAD_FIELDS, 1.0), (4.0, -4.0)))

        assert "antennas" in agent.targets[-1]


class TestSpeechMovesOnTheWords:
    def test_an_accent_peaks_at_a_word_onset(self) -> None:
        beats = [(0.0, 0.3), (1.0, 0.3), (2.0, 0.3)]

        at_onset, _ = NS["_speech_accent"](beats, 1.0)
        later, _ = NS["_speech_accent"](beats, 1.0 + NS["_SPEECH_ACCENT_DECAY"] * 3)

        assert at_onset > later * 5, "the accent is not landing on the word"

    def test_silence_before_the_first_word_is_still(self) -> None:
        envelope, _ = NS["_speech_accent"]([(0.5, 0.3)], 0.1)
        assert envelope == 0.0

    def test_no_word_timings_means_no_accent(self) -> None:
        """Some edge-tts versions emit no boundary metadata; that must not crash."""
        envelope, index = NS["_speech_accent"]([], 1.0)
        assert (envelope, index) == (0.0, 0)

    def test_consecutive_words_push_opposite_ways(self) -> None:
        """All accents in one direction reads as a tic rather than as speech."""
        beats = [(float(i) * 0.4, 0.3) for i in range(6)]
        directions = []
        for i in range(6):
            offsets, _ = NS["_speech_offsets"](beats, i * 0.4 + 0.01, 2.4)
            directions.append(offsets["yaw"] > 0)
        assert directions == [True, False, True, False, True, False]

    def test_speech_offsets_respect_the_same_ceilings(self) -> None:
        beats = [(float(i) * 0.12, 0.9) for i in range(80)]
        for tick in range(400):
            offsets, antenna = NS["_speech_offsets"](beats, tick * 0.025, 10.0, amplitude=1.0)
            for field in HEAD_FIELDS:
                assert abs(offsets[field]) <= NS["_LIFE_MAX"][field] + 1e-9
            assert max(abs(antenna[0]), abs(antenna[1])) <= NS["_LIFE_MAX"]["antenna"] + 1e-9

    def test_the_utterance_starts_and_ends_settled(self) -> None:
        """The arc is what gives a sentence a beginning and an end."""
        beats = [(0.0, 0.3)]
        start, _ = NS["_speech_offsets"](beats, 0.0, 4.0)
        middle, _ = NS["_speech_offsets"](beats, 2.0, 4.0)
        end, _ = NS["_speech_offsets"](beats, 4.0, 4.0)

        assert abs(start["z"]) < 0.01
        assert abs(end["z"]) < 0.01
        assert middle["z"] > 1.0


class TestSpeechMotionHandsBackCleanly:
    def test_the_flag_is_cleared_even_when_cancelled(self) -> None:
        """A stuck flag would leave ambient life switched off for the rest of the day."""
        agent = FakeAgent()

        async def scenario():
            task = asyncio.ensure_future(NS["_speech_motion"](agent, [(0.0, 0.3)], 30.0))
            await asyncio.sleep(0.05)
            assert agent.state["_speech_motion"] is True
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        assert agent.state["_speech_motion"] is False

    def test_it_stops_on_its_own_when_the_words_run_out(self) -> None:
        agent = FakeAgent()

        asyncio.run(NS["_speech_motion"](agent, [(0.0, 0.05)], 0.05))

        assert agent.state["_speech_motion"] is False

    def test_a_deliberate_command_mid_sentence_still_wins(self) -> None:
        agent = FakeAgent(busy=True)

        async def scenario():
            task = asyncio.ensure_future(NS["_speech_motion"](agent, [(0.0, 0.3)], 30.0))
            await asyncio.sleep(0.15)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        assert agent.targets == [], "speech motion moved the robot during a deliberate command"


class TestTheSettingsReadTheWayPeopleWriteThem:
    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "FALSE", "", None])
    def test_the_off_spellings_are_all_off(self, raw) -> None:
        """Every one of these is truthy to bool(); an env var is always a string."""
        assert NS["_truthy"](raw) is False

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE"])
    def test_the_on_spellings_are_all_on(self, raw) -> None:
        assert NS["_truthy"](raw) is True

    def test_a_persisted_bool_wins_over_the_environment(self) -> None:
        assert NS["_truthy"](False, "1") is False
        assert NS["_truthy"](None, "1") is True

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0.5", 0.5),
            # Presets reach past 1.0 on purpose - _LIFE_MAX is the real limit --
            # so the cap is _LIFE_MAX_AMPLITUDE rather than 1.0.
            ("2", 1.5),
            ("1.4", 1.4),
            ("-1", 0.0),
            ("nonsense", 1.0),
            ("", 1.0),
        ],
    )
    def test_amplitude_is_clamped_and_falls_back_to_full(self, raw, expected) -> None:
        assert NS["_life_amplitude_setting"](raw) == expected


class TestAPoseIsHeldAndThenReleased:
    """A commanded pose is transient intent, not a new resting posture.

    `describe` aims the head down through `_orient_head` -> `_pose`. If that aim
    became the base, ambient motion would breathe around a head-down robot for
    as long as it was left running, and only an explicit command would bring him
    back up.
    """

    def test_the_pose_is_held_at_first(self) -> None:
        agent = FakeAgent()
        agent.state["_life_base"] = {**NS["_life_neutral_base"](), "pitch": 25.0}
        agent.state["_life_base_at"] = 1000.0

        base, _ = NS["_life_relaxed_base"](agent, 1000.0 + NS["_LIFE_HOLD_SECONDS"] - 0.1)

        assert base["pitch"] == pytest.approx(25.0), "the aim was abandoned before it was held"

    def test_it_eases_back_to_neutral_after_the_hold(self) -> None:
        agent = FakeAgent()
        agent.state["_life_base"] = {**NS["_life_neutral_base"](), "pitch": 25.0}
        agent.state["_life_base_at"] = 1000.0

        settled = 1000.0 + NS["_LIFE_HOLD_SECONDS"] + NS["_LIFE_RELAX_SECONDS"] + 0.5
        base, _ = NS["_life_relaxed_base"](agent, settled)

        assert base["pitch"] == pytest.approx(0.0, abs=1e-6), (
            "the head never returns to neutral, so an aim taken for one "
            "command becomes the posture he keeps"
        )

    def test_the_return_is_gradual_rather_than_a_snap(self) -> None:
        agent = FakeAgent()
        agent.state["_life_base"] = {**NS["_life_neutral_base"](), "pitch": 25.0}
        agent.state["_life_base_at"] = 0.0

        span = NS["_LIFE_HOLD_SECONDS"] + NS["_LIFE_RELAX_SECONDS"]
        track = [
            NS["_life_relaxed_base"](agent, tick / 40.0)[0]["pitch"]
            for tick in range(int(span * 40) + 40)
        ]
        biggest_step = max(abs(a - b) for a, b in itertools.pairwise(track))

        assert biggest_step < 1.0, f"the return jumps {biggest_step:.2f} deg in one frame"

    def test_antennas_relax_with_the_head(self) -> None:
        agent = FakeAgent()
        agent.state["_life_antenna_base"] = (40.0, 40.0)
        agent.state["_life_base_at"] = 1000.0

        settled = 1000.0 + NS["_LIFE_HOLD_SECONDS"] + NS["_LIFE_RELAX_SECONDS"] + 0.5
        _, antennas = NS["_life_relaxed_base"](agent, settled)

        assert antennas == pytest.approx((0.0, 0.0), abs=1e-6)

    def test_the_direction_he_faces_is_never_relaxed_away(self) -> None:
        """Turning him to face the room is a decision, not a passing aim."""
        agent = FakeAgent(_facing_body_yaw_deg=180.0)
        agent.state["_life_base_at"] = 0.0

        asyncio.run(NS["_life_apply"](agent, dict.fromkeys(HEAD_FIELDS, 0.0), (0.0, 0.0)))

        assert agent.targets[-1]["body_yaw"] == pytest.approx(math.radians(180.0))

    def test_relaxation_can_be_switched_off_to_pin_a_pose(self) -> None:
        agent = FakeAgent(life_relax=False)
        agent.state["_life_base"] = {**NS["_life_neutral_base"](), "pitch": 25.0}
        agent.state["_life_base_at"] = 0.0

        base, _ = NS["_life_relaxed_base"](agent, 10_000.0)

        assert base["pitch"] == pytest.approx(25.0)


class TestAnUnknowableBaseIsAPauseNotADeadEnd:
    """A pose with no name in pose space suspends the head, and only for a while."""

    def test_recovery_brings_him_home_and_restores_the_base(self) -> None:
        moved = []
        agent = FakeAgent()
        agent.state["motion_lock"] = asyncio.Lock()
        agent.state["mini"].goto_target = lambda **kw: moved.append(kw)
        NS["_forget_base_pose"](agent)

        asyncio.run(NS["_life_recover_base"](agent))

        assert moved, "the head stays frozen wherever look_at pointed it"
        assert agent.state["_life_base_known"] is True, "ambient motion never resumed"

    def test_recovery_uses_a_trajectory_rather_than_the_raw_stream(self) -> None:
        """The distance home can be the whole range; only a goto crosses it smoothly."""
        moved = []
        agent = FakeAgent()
        agent.state["motion_lock"] = asyncio.Lock()
        agent.state["mini"].goto_target = lambda **kw: moved.append(kw)
        NS["_forget_base_pose"](agent)

        asyncio.run(NS["_life_recover_base"](agent))

        assert moved[0].get("duration", 0) > 0.5, "recovery snapped instead of moving"
        assert agent.targets == [], "recovery used set_target, which would jump"

    def test_recovery_stands_down_while_a_command_holds_the_lock(self) -> None:
        moved = []
        agent = FakeAgent()
        lock = asyncio.Lock()
        agent.state["motion_lock"] = lock
        agent.state["mini"].goto_target = lambda **kw: moved.append(kw)

        async def scenario():
            await lock.acquire()
            try:
                await NS["_life_recover_base"](agent)
            finally:
                lock.release()

        asyncio.run(scenario())

        assert moved == [], "recovery moved the robot during another command"


class TestAttractBeatsAreLegibleAndPolite:
    def test_every_beat_returns_to_neutral(self) -> None:
        """A beat ending off-centre leaves the base somewhere it did not intend."""
        for name, steps in NS["_ATTRACT_BEATS"].items():
            yaw, pitch, roll, left, right, _ = steps[-1]
            assert (yaw, pitch, roll, left, right) == (0, 0, 0, 0, 0), (
                f"{name} does not end at neutral"
            )

    def test_beats_are_big_enough_to_read_across_a_room(self) -> None:
        """Their whole purpose; ambient breathing already covers subtlety."""
        for name, steps in NS["_ATTRACT_BEATS"].items():
            reach = max(max(abs(s[0]), abs(s[1]), abs(s[3])) for s in steps)
            assert reach >= 15, f"{name} peaks at {reach} deg, invisible at distance"

    def test_a_beat_stands_down_mid_conversation(self) -> None:
        agent = FakeAgent()
        agent.state["attract_enabled"] = True
        agent.state["conversation_session"] = {"task": types.SimpleNamespace(done=lambda: False)}
        agent.state["conversation_state"] = "speaking"

        assert NS["_attract_is_welcome"](agent) is False

    def test_a_beat_is_welcome_while_merely_waiting_to_be_spoken_to(self) -> None:
        agent = FakeAgent()
        agent.state["attract_enabled"] = True
        agent.state["conversation_session"] = {"task": types.SimpleNamespace(done=lambda: False)}
        agent.state["conversation_state"] = "listening"

        assert NS["_attract_is_welcome"](agent) is True

    def test_beats_stay_off_when_attract_is_disabled(self) -> None:
        agent = FakeAgent()
        agent.state["attract_enabled"] = False

        assert NS["_attract_is_welcome"](agent) is False

    def test_a_beat_never_runs_on_an_unknown_base(self) -> None:
        """A beat ends by claiming neutral, which would undo a gaze set by look_at."""
        agent = FakeAgent()
        agent.state["attract_enabled"] = True
        NS["_forget_base_pose"](agent)

        assert NS["_attract_is_welcome"](agent) is False

    def test_playing_a_beat_leaves_a_base_ambient_motion_can_use(self) -> None:
        agent = FakeAgent()
        agent.state["motion_lock"] = asyncio.Lock()
        agent.state["mini"].goto_target = lambda **kw: None

        assert asyncio.run(NS["_play_attract_beat"](agent, "perk")) is True
        assert agent.state["_life_base_known"] is True
        assert agent.state["_life_base"]["pitch"] == 0.0

    def test_an_unknown_beat_name_is_refused_rather_than_guessed(self) -> None:
        agent = FakeAgent()
        agent.state["motion_lock"] = asyncio.Lock()

        assert asyncio.run(NS["_play_attract_beat"](agent, "moonwalk")) is False


class TestThePresetsAreOrderedAndComplete:
    """Five dials collapsed to one word, because five is too many with an
    audience already standing in front of the robot."""

    def test_every_preset_sets_every_dial(self) -> None:
        """A half-applied preset leaves the previous mood behind and confuses."""
        for name in NS["_LIFE_PRESETS"]:
            agent = FakeAgent()
            NS["_apply_life_preset"](agent, name)
            for key in (
                "life_preset",
                "life_enabled",
                "life_amplitude",
                "life_tempo",
                "life_channels",
                "attract_enabled",
                "attract_min_gap",
                "attract_max_gap",
            ):
                assert key in agent.state, f"{name} left {key} unset"

    def test_they_are_monotonic_in_liveliness(self) -> None:
        """The names are a scale; amplitude has to agree with the names."""
        ladder = ["off", "calm", "antennas", "alive", "showtime"]
        amplitudes = [NS["_LIFE_PRESETS"][name]["amplitude"] for name in ladder]
        assert amplitudes == sorted(amplitudes), f"the ladder is out of order: {amplitudes}"

    def test_alive_is_the_default(self) -> None:
        assert NS["_LIFE_DEFAULT_PRESET"] == "alive"

    def test_alive_is_the_reference_tuning(self) -> None:
        """Every other preset is defined around it, so it must not drift."""
        alive = NS["_LIFE_PRESETS"]["alive"]

        assert alive["amplitude"] == 1.0
        assert alive["tempo"] == 1.0
        assert alive["attract"] is True
        assert alive["gaps"] == (18.0, 45.0)
        assert alive["channels"] == ("head", "body", "antennas")

    def test_every_preset_explains_itself(self) -> None:
        """cmd=life returns these, so the list is self-documenting in the field."""
        for name, spec in NS["_LIFE_PRESETS"].items():
            assert spec["blurb"].strip(), f"{name} has no description"

    def test_showtime_is_allowed_past_an_amplitude_of_one(self) -> None:
        """The hard ceilings are the safety limit; the tuned motion sits below."""
        assert NS["_LIFE_PRESETS"]["showtime"]["amplitude"] > 1.0
        assert NS["_LIFE_PRESETS"]["showtime"]["amplitude"] <= NS["_LIFE_MAX_AMPLITUDE"]

    def test_showtime_stays_inside_the_hard_ceilings(self) -> None:
        """Raising the gain must not be a way around the envelope."""
        gain = NS["_LIFE_PRESETS"]["showtime"]["amplitude"]
        for tick in range(600):
            result = offsets_at(tick * 0.31, gaze=(99.0, 99.0), amplitude=gain)
            for field in HEAD_FIELDS:
                assert abs(result[field]) <= NS["_LIFE_MAX"][field] + 1e-9


class TestAPresetDecidesWhichJointsMoveAtAll:
    def test_antennas_only_keeps_the_head_completely_still(self) -> None:
        """Different from "very small": this holds the head, it does not shrink it."""
        agent = FakeAgent()
        NS["_apply_life_preset"](agent, "antennas")

        asyncio.run(NS["_life_apply"](agent, dict.fromkeys(HEAD_FIELDS, 3.0), (5.0, -5.0)))

        sent = agent.targets[-1]
        assert "head" not in sent
        assert "body_yaw" not in sent
        assert "antennas" in sent

    def test_off_sends_no_target_at_all(self) -> None:
        """An empty target is a wasted round trip 20 times a second."""
        agent = FakeAgent()
        NS["_apply_life_preset"](agent, "off")

        asyncio.run(NS["_life_apply"](agent, dict.fromkeys(HEAD_FIELDS, 3.0), (5.0, -5.0)))

        assert agent.targets == []

    def test_alive_moves_everything(self) -> None:
        agent = FakeAgent()
        NS["_apply_life_preset"](agent, "alive")

        asyncio.run(NS["_life_apply"](agent, dict.fromkeys(HEAD_FIELDS, 3.0), (5.0, -5.0)))

        assert set(agent.targets[-1]) == {"head", "body_yaw", "antennas"}

    def test_attract_beats_never_play_on_a_head_still_preset(self) -> None:
        """Every beat is a head move, so "antennas only" has to mean it."""
        agent = FakeAgent()
        NS["_apply_life_preset"](agent, "antennas")
        agent.state["attract_enabled"] = True  # even if forced back on

        assert NS["_attract_is_welcome"](agent) is False


class TestPresetNamesSurviveTheRealWorld:
    def test_an_unknown_name_falls_back_rather_than_failing_to_boot(self) -> None:
        """This is read from .env; a typo must not cost a working robot."""
        assert NS["_life_preset_name"]("livley") == "alive"

    def test_the_first_name_that_is_real_wins(self) -> None:
        assert NS["_life_preset_name"](None, "", "calm") == "calm"

    @pytest.mark.parametrize("raw", ["CALM", " calm ", "Calm"])
    def test_case_and_padding_do_not_matter(self, raw: str) -> None:
        assert NS["_life_preset_name"](raw) == "calm"

    def test_applying_an_unknown_name_leaves_a_working_robot(self) -> None:
        agent = FakeAgent()

        assert NS["_apply_life_preset"](agent, "banana") == "alive"
        assert agent.state["life_enabled"] is True


class TestPresetsCanBeSaidOutLoud:
    """The control that gets used with an audience watching, so it has to work
    from across the room and in either language."""

    @pytest.mark.parametrize(
        ("phrase", "expected"),
        [
            ("calm down", "calm"),
            ("settle down", "calm"),
            ("be still", "calm"),
            ("antennas only", "antennas"),
            ("just your antennas", "antennas"),
            ("showtime", "showtime"),
            ("show off", "showtime"),
            ("full energy", "showtime"),
            ("stop moving", "off"),
            ("hold still", "off"),
        ],
    )
    def test_english(self, phrase: str, expected: str) -> None:
        assert NS["_embodied_command_for_text"](phrase) == {"cmd": "life", "preset": expected}

    @pytest.mark.parametrize(
        ("phrase", "expected"),
        [
            ("ηρέμησε", "calm"),
            ("χαλάρωσε", "calm"),
            ("μόνο τις κεραίες", "antennas"),
            ("μη κουνιέσαι", "off"),
            ("πιο ζωηρά", "showtime"),
        ],
    )
    def test_greek(self, phrase: str, expected: str) -> None:
        assert NS["_embodied_command_for_text"](phrase) == {"cmd": "life", "preset": expected}

    def test_a_volume_request_is_still_about_the_voice(self) -> None:
        """The two vocabularies overlap; ordering decides, so pin the ordering."""
        assert NS["_embodied_command_for_text"]("lower your voice") == {
            "cmd": "volume",
            "delta": -25,
        }

    def test_an_ordinary_request_is_not_swallowed(self) -> None:
        assert NS["_embodied_command_for_text"]("turn the light on") is None


class TestAStuckHoldCannotFreezeHimForGood:
    """A suppression flag that outlives its owner must not be permanent.

    Ambient motion stands down for `busy` and for speech. Both are cleared in a
    `finally`, but a cancellation at the wrong moment can leave one set with
    nothing left to clear it — and the loop checks suppression *before* anything
    else, so a stuck flag halts the head, the antennas and the base recovery
    together, while every command still reports success.
    """

    def test_a_speech_flag_left_behind_is_released(self) -> None:
        agent = FakeAgent(_speech_motion=True, _speaking=False)

        cleared = NS["_life_clear_stale_holds"](agent)

        assert "speech-motion" in cleared
        assert NS["_life_should_run"](agent) is True

    def test_a_busy_flag_left_behind_is_released(self) -> None:
        agent = FakeAgent(busy=True)
        agent.state["motion_lock"] = asyncio.Lock()

        cleared = NS["_life_clear_stale_holds"](agent)

        assert "busy" in cleared
        assert NS["_life_should_run"](agent) is True

    def test_speech_actually_in_progress_is_never_interrupted(self) -> None:
        """`_speaking` is set by playback itself, so it cannot be faked."""
        agent = FakeAgent(_speech_motion=True, _speaking=True)

        assert NS["_life_clear_stale_holds"](agent) == []
        assert agent.state["_speech_motion"] is True

    def test_a_command_actually_running_is_never_interrupted(self) -> None:
        """A real command holds the motion lock for its whole duration."""
        agent = FakeAgent(busy=True)
        lock = asyncio.Lock()
        agent.state["motion_lock"] = lock

        async def scenario():
            await lock.acquire()
            try:
                return NS["_life_clear_stale_holds"](agent)
            finally:
                lock.release()

        assert asyncio.run(scenario()) == []
        assert agent.state["busy"] is True

    def test_nothing_stuck_means_nothing_cleared(self) -> None:
        agent = FakeAgent()
        agent.state["motion_lock"] = asyncio.Lock()

        assert NS["_life_clear_stale_holds"](agent) == []

    def test_the_timeout_outlasts_any_real_utterance_or_move(self) -> None:
        """It must never fire on a long sentence or a slow trajectory."""
        assert NS["_LIFE_STUCK_SECONDS"] > 20.0

    def test_both_holds_are_released_together(self) -> None:
        agent = FakeAgent(busy=True, _speech_motion=True, _speaking=False)
        agent.state["motion_lock"] = asyncio.Lock()

        cleared = NS["_life_clear_stale_holds"](agent)

        assert sorted(cleared) == ["busy", "speech-motion"]


class TestThePhrasingsPeopleActuallyUse:
    """Each of these should reach the preset without going through the planner."""

    @pytest.mark.parametrize(
        ("phrase", "expected"),
        [
            ("alive", "alive"),
            ("set preset animation alive", "alive"),
            ("i mean set preset animation alive", "alive"),
            ("idle showtime", "showtime"),
            ("motion calm", "calm"),
            ("change the animation to alive", "alive"),
            ("preset off", "off"),
            ("κανονικά", "alive"),
        ],
    )
    def test_it_reaches_the_preset(self, phrase: str, expected: str) -> None:
        assert NS["_embodied_command_for_text"](phrase) == {"cmd": "life", "preset": expected}

    @pytest.mark.parametrize(
        "phrase",
        [
            "are you alive?",
            "what is normal for you",
            "turn off the light",
            "perform a visual inspection",
        ],
    )
    def test_a_bare_preset_word_does_not_hijack_a_real_request(self, phrase: str) -> None:
        """ "alive" is a preset name; "are you alive?" is a question about him."""
        result = NS["_embodied_command_for_text"](phrase)
        assert result is None or result.get("cmd") != "life"


def recipe_source() -> str:
    """The recipe text itself, for the few invariants that live in ordering."""
    from wactorz.catalogue_agents.reachy_mini_agent import AGENT_CODE

    return AGENT_CODE


class TestItIsAliveFromSpawnWithNoCommands:
    """Reachy can be driven as a pure output channel, with the microphone in the
    browser (Web Speech), so `conversation_start` is never called and neither is
    `wake`. Everything the animation layer needs must already be true when setup
    ends.
    """

    def test_setup_wakes_the_robot_before_starting_the_loop(self) -> None:
        """Ordering, not vibes: `awake` gates every frame, and bring-up sets it."""
        source = recipe_source()
        bring_up = source.index("await _bring_up_robot(agent)")
        start = source.index("_start_life_loop(agent)\n        preset =")

        assert bring_up < start, (
            "ambient life starts before the robot is woken, so the first frames "
            "are dropped and nothing moves until something else wakes him"
        )

    def test_bring_up_enables_motors_before_waking(self) -> None:
        """Torque off means the daemon accepts targets and moves nothing."""
        source = recipe_source()
        start = source.index("async def _bring_up_robot")
        block = source[start : source.index("\ndef ", start)]
        motors = block.index("_ensure_motors_enabled")
        wake = block.index("mini.wake_up")

        assert motors < wake

    def test_a_failed_wake_is_reported_rather_than_silent(self) -> None:
        """Otherwise: ready in the log, every command succeeds, and nothing moves."""
        assert "ambient life is on but Reachy is not awake" in recipe_source()

    def test_the_startup_line_names_the_preset(self) -> None:
        """Before he moves, the log is where the mood is visible."""
        assert 'f"ambient life on (preset: {preset})"' in recipe_source()


class TestNothingRequiresAConversationSession:
    """Every gate has to cope with a session that is never created."""

    def test_ambient_motion_runs_with_no_session(self) -> None:
        agent = FakeAgent()
        NS["_apply_life_preset"](agent, "alive")
        agent.state.pop("conversation_session", None)

        assert NS["_life_should_run"](agent) is True

    def test_attract_beats_run_with_no_session(self) -> None:
        agent = FakeAgent()
        NS["_apply_life_preset"](agent, "alive")
        agent.state.pop("conversation_session", None)

        assert NS["_attract_is_welcome"](agent) is True

    def test_a_session_key_left_as_none_is_not_mistaken_for_a_live_one(self) -> None:
        agent = FakeAgent(conversation_session=None)
        NS["_apply_life_preset"](agent, "alive")

        assert NS["_attract_is_welcome"](agent) is True

    def test_beats_still_stand_down_while_he_is_speaking(self) -> None:
        """The chat mic still produces spoken replies; those still own the head."""
        agent = FakeAgent(_speaking=True)
        NS["_apply_life_preset"](agent, "alive")

        assert NS["_attract_is_welcome"](agent) is False


class TestTheFirstBeatArrivesPromptly:
    def test_the_loop_does_not_wait_a_full_gap_before_the_first_beat(self) -> None:
        """Nothing tells you the layer is alive until something moves, and a
        fresh spawn is when that most needs confirming."""
        source = recipe_source()
        block = source[source.index("async def _attract_loop") :][:1400]

        assert "first = True" in block
        assert "rng.uniform(2.5, 5.0)" in block

    def test_the_normal_cadence_still_applies_afterwards(self) -> None:
        source = recipe_source()
        block = source[source.index("async def _attract_loop") :][:1400]

        assert 'agent.state.get("attract_min_gap"' in block
        assert 'agent.state.get("attract_max_gap"' in block
