"""CATALOG RECIPE — reachy-mini-agent
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Drives a Reachy Mini robot (Wireless over WiFi, or Lite over USB) as an
embodied output channel for the wactorz fleet. Subscribes to:

    custom/reachy/cmd            — structured commands (one verb per payload)
    custom/reachy/cmd/<verb>     — per-verb shortcut topics
                                      (includes conversation_start/stop)
    custom/reachy/config         — runtime config (currently: robot_host)
    <bound topics>               — dynamically bound via cmd=bind

Publishes:

    custom/reachy/state          — retained: awake, busy, robot_host, bindings
    custom/reachy/events         — per-command success/failure
    custom/reachy/cmd_result/<id>— per-correlated-command ack
    custom/reachy/camera         — one-shot camera frame (only when cmd=camera publish=true)
    custom/reachy/audio          — one-shot mic clip   (only when cmd=listen publish=true)

Push-to-talk voice input is explicit: cmd=ask_voice captures one bounded WAV,
transcribes it, and sends the transcript through the existing Wactorz interface
bridge. It never enables always-on listening or wake-word detection.

Reactive bindings are persisted so they survive /agents restart.

DEPENDENCIES
────────────
Python:
    reachy-mini    → Pollen Robotics SDK (and its transitive deps)
    numpy
    edge-tts       → speech synthesis for the `say` command (required for `say`;
                     other commands work without it)

Outside wactorz:
    Wireless: robot powered on, same WiFi as the wactorz host, no client
              isolation on the network (test with `ping reachy-mini.local`).
    Lite:     daemon running locally — `reachy-mini-daemon -p <serial_port>`.

    NO HF App may be running on the robot — Apps take exclusive control.

    ffmpeg (OPTIONAL system binary) — only used to boost the TTS loudness
           (~3-4x). If it is missing or fails, `say` still works: it just plays
           the raw, quieter edge-tts audio and says so once. Install it on the
           host if room/audience-level speech is too quiet; the Home Assistant
           add-on image does not carry it.

SPAWN
─────
    @catalog spawn reachy-mini
    /agents                    → confirms reachy-mini is running
    /agents restart reachy-mini → re-runs setup() if connection dropped

PINNING THE HOST (Wireless only — optional)
───────────────────────────────────────────
The SDK autodetects Lite vs Wireless and chooses the right transport. If you
have multiple robots on the LAN, or mDNS is flaky, pin a host:

    env:         REACHY_ROBOT_HOST=192.168.1.42   (or reachy-mini.local)
    or publish:  custom/reachy/config  {"robot_host": "192.168.1.42"}

The env var is the reliable choice when running WITHOUT the control app and it
survives a wiped state folder; the config-topic value persists across restarts.
Current host is published to custom/reachy/state under `robot_host`.

RUN WITHOUT THE REACHY MINI CONTROL APP (direct to the robot over WiFi)
──────────────────────────────────────────────────────────────────────
The control app is only needed for `connection_mode=local` (or the simulator).
To drive the powered-on robot directly, with no app running on your machine:

    REACHY_CONNECTION_MODE=network
    REACHY_ROBOT_HOST=<robot ip or reachy-mini.local>

Set both in .env and restart the agent. `network` skips the localhost probe and
connects straight to the robot's on-board daemon; pinning the host avoids
depending on mDNS. If it still can't connect, the failure reason is now in the
agent log AND in the "reachy not connected: …" reply to any robot command.

CONNECTION MODE (wireless robot vs. local control app / simulator)
──────────────────────────────────────────────────────────────────
Default is auto-detect (localhost first, then the robot). To pin one:

    publish to:  custom/reachy/config
    payload:     {"connection_mode": "network"}   # wireless: straight to robot
                 {"connection_mode": "local"}      # Reachy Mini control app / sim

Or set REACHY_CONNECTION_MODE=network|local in the environment. Restart the
agent to apply. "network" talks directly to the robot over WiFi and skips the
localhost probe (no control app needed); "local" targets the Reachy Mini
control app or simulator on localhost. The active mode is published to
custom/reachy/state under `connection_mode`. Audio routing is set separately by
media_backend, not by this mode.

TASK PAYLOAD EXAMPLES
─────────────────────
Wake / sleep:
    {"cmd": "wake"}      {"cmd": "sleep"}

Reconnect after the robot was off at spawn (no agent restart needed):
    {"cmd": "reconnect"}   # or say "reconnect" / "connect" / "try again"
    # Re-runs the same connection ladder setup() uses, reading the CURRENT
    # robot_host / connection_mode / media_backend (so a custom/reachy/config
    # publish then a reconnect applies without a restart), then brings the robot
    # back up: audio routing, volume sync, motor torque, wake.
    # {"cmd": "reconnect", "force": true} re-opens even if the link looks alive.

Motor torque (the control app's enable/disable — needed to move at all):
    {"cmd": "motors", "on": true}    # torque ON so the robot can move
    {"cmd": "motors", "on": false}   # torque OFF (compliant; hand-pose it)
    # Motors are enabled automatically on connect and on every wake. If the
    # robot TALKS but won't MOVE, its torque is off — this is the toggle. Plain
    # English "enable motors" / "go limp" work too.

Stop talking right now (cut the current utterance):
    {"cmd": "shutup"}    # or say "shut up" / "stop talking" / "be quiet"
    # Different from volume mute (persistent): shutup cuts what's playing NOW.
    # {"cmd":"stop"} also cuts speech and motion.

Diagnose "won't move" (daemon version vs SDK + a real move self-test):
    {"cmd": "diag"}      # or say "diag" / "why won't you move"

Head pose (yaw 30°, smooth):
    {"cmd": "pose", "yaw": 30, "duration": 0.6, "method": "minjerk"}

Antennas (degrees, default):
    {"cmd": "antennas", "left": 45, "right": -45, "duration": 0.3}

Look at a world point:
    {"cmd": "look_at", "x": 0.5, "y": 0.0, "z": 0.2, "duration": 1.0}

Play recorded emotion:
    {"cmd": "emotion", "name": "curious1"}

Capture a camera frame (base64 JPEG in the result; add "publish": true to also
emit it on custom/reachy/camera, or "path": "/tmp/shot.jpg" to save it). NOTE:
the frame is NOT saved anywhere unless you pass "path" or "publish":
    {"cmd": "camera"}

Look and SPEAK what the camera sees (sends the frame to the vision LLM, then
says the real description — not a made-up line). The head is oriented to the look
aim first (default level/forward; aim with "look down/up/left" or pitch/yaw).
Optional "question" to ask something specific; "say": false to get text only:
    {"cmd": "describe"}
    {"cmd": "describe", "question": "how many people are here?"}
    {"cmd": "describe", "pitch": 22}          # look down at the desk, then describe

Look AROUND the room — pan across several head angles and describe the whole
scene in one go (say "look around" / "what's in the room"):
    {"cmd": "look_around"}

Record a short mic clip (base64 WAV + direction of arrival in the result):
    {"cmd": "listen", "duration": 3}

Direction of arrival only (no recording):
    {"cmd": "doa"}

Reactive binding — robot looks curious when living-room lamp turns on:
    {"cmd": "bind",
     "topic": "home/state/light.living_room",
     "when":  {"new_state.state": "on"},
     "do":    {"cmd": "emotion", "name": "curious1"}}

Unbind:
    {"cmd": "unbind", "topic": "home/state/light.living_room"}

For full payload shapes, see reachy-mini.README.md alongside this file.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

# ──────────────────────────────────────────────────────────────────────────────
# AGENT_CODE — loaded by CatalogAgent._load_recipe()
# ──────────────────────────────────────────────────────────────────────────────

AGENT_CODE = r'''
import asyncio
import base64
import difflib
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from collections import deque
from typing import Any


async def _do(fn, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking SDK call in the default executor so the actor loop stays free."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


# Pollen's conversation app applies these XVF3800 values before opening its
# realtime audio loop. In particular, they keep the board's acoustic echo
# cancellation usable for close-field speech while Reachy's speaker is active.
_CONVERSATION_AUDIO_CONFIG = (
    ("PP_AGCMAXGAIN", (10.0,)),
    ("PP_MIN_NS", (0.8,)),
    ("PP_MIN_NN", (0.8,)),
    ("PP_GAMMA_E", (0.5,)),
    ("PP_GAMMA_ETAIL", (0.5,)),
    # The current robot firmware owns/forces PP_NLATTENONOFF. Verifying a
    # requested value falsely marks the otherwise-applied profile as failed.
    ("PP_MGSCALE", (4.0, 1.0, 1.0)),
)


async def _configure_conversation_audio(agent) -> bool:
    """Apply Pollen's conversation tuning locally or through the robot daemon.

    Barge-in is unsafe unless this succeeds: without echo cancellation Reachy's
    microphone hears its own first syllable and immediately stops playback.
    """
    mini = agent.state.get("mini")
    media = getattr(mini, "media", None) or getattr(mini, "media_manager", None)
    audio = getattr(media, "audio", None) if media is not None else None
    configured = False
    route = "unavailable"
    try:
        daemon_url = str(getattr(audio, "daemon_url", None) or "").rstrip("/")
        if daemon_url:
            route = "robot daemon"

            def _apply_remote():
                import requests

                response = requests.post(
                    f"{daemon_url}/api/audio/config/apply",
                    json={
                        "config": [
                            {"name": name, "values": list(values)}
                            for name, values in _CONVERSATION_AUDIO_CONFIG
                        ],
                        "verify": True,
                    },
                    timeout=5,
                )
                response.raise_for_status()
                return bool(response.json().get("applied"))

            configured = bool(await _do(_apply_remote))
        else:
            apply_config = getattr(audio, "apply_audio_config", None)
            if callable(apply_config):
                route = "local audio board"
                configured = bool(
                    await _do(
                        apply_config,
                        _CONVERSATION_AUDIO_CONFIG,
                        verify=True,
                        write_settle_seconds=0.1,
                    )
                )
    except Exception as exc:
        await agent.log(
            f"Conversation echo-control setup via {route} failed: {exc}",
            level="warning",
        )

    agent.state["conversation_echo_control"] = configured
    if configured:
        await agent.log(
            f"Conversation echo control configured via {route}; automatic voice "
            "interruption remains off unless barge_in=true is requested."
        )
    else:
        await agent.log(
            "Conversation echo control is unavailable; automatic voice interruption "
            "is disabled so Reachy cannot cut off its own speech. Set barge_in=true "
            "explicitly only if this audio setup has working echo cancellation.",
            level="warning",
        )
    return configured


def _normalize_connection_mode(raw: str) -> str:
    """Map user-facing connection words to 'network' | 'local' | '' (auto).

    'network'/'wireless' → talk straight to the robot over WiFi (no control app
    needed). 'local'/'sim' → the Reachy Mini control app or simulator on
    localhost. Anything unrecognised (including '') means auto-detect.
    """
    m = (raw or "").strip().lower()
    if m in ("network", "wireless", "wifi", "remote", "robot", "direct"):
        return "network"
    if m in (
        "local",
        "localhost",
        "app",
        "control",
        "control-app",
        "control_app",
        "sim",
        "simu",
        "simulation",
        "simulator",
        "desktop",
    ):
        return "local"
    return ""


def _build_connection_attempts(robot_host, media_backend, conn_mode):
    """Ordered ReachyMini(**kwargs) attempts for the requested connection mode.

    conn_mode:
      'network' — wireless: connect straight to the robot, never probe localhost.
      'local'   — the Reachy Mini control app / simulator on localhost.
      ''        — auto: pinned host (if any), then SDK autodetect, then network.

    media_backend (when set) is threaded into every attempt — it, not the
    connection mode, is what decides whether audio plays on the robot.
    """
    base = {"media_backend": media_backend} if media_backend else {}
    host = (robot_host or "").strip()
    attempts = []
    if conn_mode == "network":
        # Direct to the robot over WiFi; skip the localhost probe entirely.
        if host:
            attempts.append({**base, "host": host, "connection_mode": "network"})
        attempts.append({**base, "connection_mode": "network"})
    elif conn_mode == "local":
        # The control app / simulator listens on localhost; a robot_host pin is
        # the robot's own address, so it is intentionally ignored here.
        attempts.append({**base, "host": "localhost"})
        attempts.append({**base})  # autodetect (localhost-first) fallback
    else:
        # Auto: pinned host first, then SDK autodetect, then forced network.
        if host:
            attempts.append({**base, "host": host})
            attempts.append({**base, "host": host, "connection_mode": "network"})
        attempts.append({**base})
        attempts.append({**base, "connection_mode": "network"})
    return attempts


def _describe_attempt(kw):
    """Human-readable label for one ReachyMini(**kwargs) attempt."""
    parts = []
    if kw.get("host"):
        parts.append(f"host={kw['host']}")
    if kw.get("connection_mode"):
        parts.append(kw["connection_mode"])
    return "+".join(parts) or "autodetect(localhost-first)"


async def _open_robot(agent):
    """Run the connection ladder once. Returns (mini, last_err, tried_label).

    Reads the CURRENT robot_host / media_backend / connection_mode off
    agent.state, so a custom/reachy/config update followed by cmd=reconnect
    applies without a restart. Shared by setup() and _reconnect() so both take
    exactly the same path — there is no second, drifting connect implementation.
    """
    from reachy_mini import ReachyMini  # pyright: ignore[reportMissingImports]

    conn_mode = agent.state.get("connection_mode") or ""
    if conn_mode == "auto":  # state's sentinel for "no explicit mode"
        conn_mode = ""
    attempts = _build_connection_attempts(
        agent.state.get("robot_host") or "",
        agent.state.get("media_backend") or "",
        conn_mode,
    )

    # IMPORTANT: ReachyMini().__enter__() does blocking websocket / media handshakes
    # that take 5–10 seconds. Running it directly here freezes the asyncio event
    # loop long enough for MQTT keepalives to time out and the monitor server
    # (port 8887) to fail to bind. Always run it in an executor.
    loop = asyncio.get_event_loop()

    def _open_sync(kw):
        return ReachyMini(**kw).__enter__()

    mini = None
    last_err = None
    for kwargs in attempts:
        try:
            mini = await loop.run_in_executor(None, _open_sync, kwargs)
            await agent.log(f"Connected to Reachy via {kwargs or 'autodetect'}")
            break
        except TypeError as e:
            # Older SDK without connection_mode/host kwargs — keep trying simpler forms.
            last_err = e
            continue
        except Exception as e:
            last_err = e
            continue

    tried = ", ".join(_describe_attempt(kw) for kw in attempts) or "autodetect"
    agent.state["last_connect_error"] = str(last_err) if mini is None else None
    return mini, last_err, tried


async def _bring_up_robot(agent):
    """Post-connect bring-up. Requires agent.state['mini'] to be a live handle.

    Runs on both the setup() path and cmd=reconnect, so a reconnected robot is
    in the same state a freshly-spawned one is: audio routing reported, volume
    synced from the daemon, motor torque on, awake.
    """
    mini = agent.state.get("mini")

    # Report where speech will come out: only the WebRTC backend reaches the
    # robot speaker (play_sound -> daemon). LOCAL/gstreamer plays on this host.
    try:
        _audio = getattr(getattr(mini, "media", None), "audio", None)
        _on_robot = bool(getattr(_audio, "daemon_url", None))
        agent.state["audio_on_robot"] = _on_robot
        if _on_robot:
            await agent.log("Audio routes to the ROBOT speaker (WebRTC backend).")
        else:
            await agent.log(
                "Audio will play on THIS HOST, not the robot. For robot "
                'speech publish {"media_backend": "webrtc"} to '
                "custom/reachy/config and reconnect.",
                level="warning",
            )
    except Exception:
        pass

    # Tune the XVF3800 before conversation playback begins. Wireless clients use
    # the daemon REST endpoint because the USB audio board lives on the robot;
    # Lite/local clients can configure the board through the SDK directly.
    await _configure_conversation_audio(agent)

    # The daemon persists its own volume, so sync FROM it (a GET, no test sound)
    # rather than re-applying ours. Caller has already seeded the persisted value.
    live = await _get_daemon_volume(agent)
    if live is not None:
        agent.state["volume_level"] = live
        await agent.log(f"Robot speaker volume is {live}/100 (from daemon).")

    # Enable motor torque so the robot actually MOVES: wake_up()/goto_target()
    # only stream target positions; with torque OFF the daemon accepts them and
    # plays sounds but nothing moves ("talks but won't move"). The control app
    # enables motors for you; running direct over `network` the daemon can come
    # up with motors disabled, so do it here.
    await _ensure_motors_enabled(agent)

    try:
        await _do(mini.wake_up)
        agent.state["awake"] = True
        await agent.publish("custom/reachy/events", {"type": "wake", "ts": time.time()})
    except Exception as e:
        await agent.alert(f"wake_up failed: {e}", severity="warning")


#: Motor faults the daemon reads from the servos, with what to do about each.
#: The daemon decodes these from the Dynamixel hardware-error byte and writes
#: them to its log and nowhere else — not into the status the SDK can read — so
#: the log stream is the only way to see them. That is also where the official
#: control app gets its overheating warning from.
_MOTOR_FAULT_ADVICE = {
    "overheating error": (
        "A motor is too hot. Let Reachy rest — it will cut its own torque to "
        "protect itself if it gets hotter."
    ),
    "overload error": (
        "A motor is straining. Check nothing is holding the head or antennas, "
        "and that no pose is pushing against a limit."
    ),
    "electrical shock error": (
        "A motor reported an electrical fault. Power-cycle the robot; if it "
        "comes back, stop using it and check the wiring."
    ),
    # Deliberately not a warning. Pollen run Reachy Mini above the servos' own
    # protection threshold on purpose, and say so in their FAQ, so this fires on
    # a healthy robot. The daemon already drops it below 7.8V; above that it
    # still logs, and treating it as a fault would mean crying wolf on every
    # robot — which teaches people to ignore the ones that matter.
    "input voltage error": (
        "Expected on Reachy Mini: it runs the motors above their default "
        "voltage threshold on purpose. Nothing to do."
    ),
}

#: Faults reported quietly rather than as warnings, because they are normal here.
_MOTOR_FAULT_EXPECTED = frozenset({"input voltage error"})

#: How long the same fault on the same motor stays quiet after being reported.
#: The daemon re-reads the motors several times a second, so without this one
#: hot motor would be a warning per read.
_MOTOR_FAULT_QUIET_S = 300.0


def _motor_faults_in_line(line: str) -> list[tuple[str, str]]:
    """Faults named by one daemon log line, as (motor, fault) pairs.

    The daemon logs `Motor 'head_yaw' hardware errors: ['Overheating Error']`,
    wrapped in whatever prefix journalctl adds. Parsing rather than reading a
    status field is not a shortcut: the status field does not carry them.
    """
    match = re.search(r"Motor '([^']+)' hardware errors: \[([^\]]*)\]", str(line or ""))
    if not match:
        return []
    motor = match.group(1).strip()
    faults = [
        fault.strip().strip("'\"").lower()
        for fault in match.group(2).split(",")
        if fault.strip().strip("'\"")
    ]
    return [(motor, fault) for fault in faults if fault]


def _describe_motor_fault(motor, fault):
    """One line a person can act on, for one fault on one motor."""
    advice = _MOTOR_FAULT_ADVICE.get(fault, "The motor reported a fault the daemon did not name.")
    return f"{motor}: {fault} — {advice}"


def _fault_should_be_reported(agent, motor, fault, now=None) -> bool:
    """Whether this fault is new enough to be worth saying again."""
    seen = agent.state.setdefault("_motor_faults_seen", {})
    stamp = time.time() if now is None else now
    last = seen.get(f"{motor}/{fault}")
    if last is not None and stamp - last < _MOTOR_FAULT_QUIET_S:
        return False
    seen[f"{motor}/{fault}"] = stamp
    return True


async def _report_motor_faults(agent, line, now=None) -> list[str]:
    """Warn about any fault named by a log line. Returns what was reported."""
    reported = []
    for motor, fault in _motor_faults_in_line(line):
        if not _fault_should_be_reported(agent, motor, fault, now=now):
            continue
        message = _describe_motor_fault(motor, fault)
        if fault in _MOTOR_FAULT_EXPECTED:
            # Logged so it is not invisible, but never raised at the user.
            await agent.log(f"motor note — {message}")
            continue
        reported.append(message)
        await agent.log(f"motor fault — {message}", level="warning")
        try:
            await agent.notify_user(
                f"Reachy hardware warning. {message}",
                **{"from": getattr(agent, "name", "reachy-mini"), "to": "user"},
            )
        except Exception:
            pass
    return reported


def _daemon_log_ws_url(agent) -> str | None:
    """WebSocket URL of the daemon's log stream, or None when unreachable.

    Only the wireless robot serves it — the daemon mounts the route behind its
    `wireless_version` flag — so a Lite over USB has no fault stream and simply
    gets no watcher.
    """
    url = _daemon_url(agent)
    if not url:
        return None
    base = str(url).rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/logs/ws/daemon"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/logs/ws/daemon"
    return None


async def _watch_motor_faults(agent, reconnect_s=20.0) -> None:
    """Follow the daemon's log and warn about motor faults it only logs.

    Reachy has no battery telemetry and no temperature the SDK will hand over —
    the one thing the robot does report about its own health is the motors'
    hardware-error byte, and the daemon writes that to its log and nowhere the
    SDK can read. So this reads the log, which is the same source the official
    control app shows its overheating warning from.

    Reconnects rather than giving up: the stream ends whenever the daemon
    restarts, which is exactly when a fault is most likely to appear next.
    """
    import aiohttp

    while True:
        url = _daemon_log_ws_url(agent)
        if not url:
            await asyncio.sleep(reconnect_s)
            continue
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(url, heartbeat=30) as ws:
                    await agent.log("watching the robot's log for motor faults")
                    async for message in ws:
                        if message.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        await _report_motor_faults(agent, message.data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never fatal and never noisy: no fault stream is the normal state
            # for a Lite, and a wireless robot that is simply off should not
            # fill the log with connection failures.
            await agent.log(f"motor fault watch unavailable: {exc}", level="debug")
        await asyncio.sleep(reconnect_s)


def _start_motor_fault_watch(agent):
    """Start the fault watch once, if it is not already running."""
    task = agent.state.get("_motor_fault_task")
    if task is not None and not task.done():
        return task
    task = agent.run_in_background(_watch_motor_faults(agent))
    agent.state["_motor_fault_task"] = task
    return task


async def _imu_temperature(agent) -> float | None:
    """The IMU's own temperature in °C, or None when the robot has no IMU.

    This is the inertial chip, not a motor, so it reads the robot's internal
    ambient rather than the thing that actually overheats. Reported because it
    is the only temperature the SDK exposes at all; the motors' own thermal
    fault arrives through the log watch instead.
    """
    mini = agent.state.get("mini")
    if mini is None:
        return None
    try:
        data = await _do(mini.get_imu_data)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("temperature", "")
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


async def _health(agent, payload=None) -> dict[str, Any]:
    """Report what the robot can actually say about its own condition."""
    del payload
    ok_link, link_reason = _is_connected(agent)
    temperature = await _imu_temperature(agent) if ok_link else None
    faults = sorted(agent.state.get("_motor_faults_seen") or {})
    watching = bool(_daemon_log_ws_url(agent))

    parts = []
    if not ok_link:
        parts.append(link_reason or "not connected")
    if temperature is not None:
        parts.append(f"internal temperature {temperature}°C")
    if faults:
        parts.append("motor faults seen this session: " + ", ".join(faults))
    elif watching:
        parts.append("no motor faults reported")
    if not watching and ok_link:
        # The Lite has no log stream, so silence there means "not watched",
        # which must not be read as "nothing wrong".
        parts.append(
            "motor fault warnings need the wireless robot's daemon; this link does not serve them"
        )
    # Said plainly rather than left to be inferred from an absent number.
    parts.append("Reachy reports no battery level — the SDK exposes none")

    return {
        "ok": True,
        "cmd": "health",
        "connected": ok_link,
        "imu_temperature_c": temperature,
        "motor_faults": faults,
        "watching_faults": watching,
        "battery": None,
        "result": ". ".join(parts) + ".",
    }


async def setup(agent):
    # ---- Heavy imports inside setup (never at module level) ----
    import numpy as np
    from reachy_mini.utils import create_head_pose  # pyright: ignore[reportMissingImports]

    # ---- Resolve robot host (Wireless on LAN OR Lite via USB) ----
    # Priority: persisted robot_host  >  REACHY_ROBOT_HOST env  >  autodetect.
    # The user can pin it any of these ways:
    #   1. publish once to custom/reachy/config: {"robot_host": "192.168.1.42"}
    #      (persists across restarts)
    #   2. set REACHY_ROBOT_HOST=reachy-mini.local (or an IP) in the environment
    #      /.env — the reliable way to run WITHOUT the control app and without
    #      relying on mDNS, and it survives a wiped state folder.
    #   3. leave blank — SDK autodetect chooses Lite (localhost) vs Wireless (LAN).
    robot_host = (agent.recall("robot_host") or os.environ.get("REACHY_ROBOT_HOST") or "").strip()
    agent.state["robot_host"] = robot_host

    # Live update channel for the host (no restart needed)
    async def on_config(payload):
        if not isinstance(payload, dict):
            return
        if "robot_host" in payload:
            h = payload["robot_host"]
            agent.persist("robot_host", h)
            agent.state["robot_host"] = h
            await agent.log(f'robot_host updated to {h} — say "reconnect" to apply')
        if "media_backend" in payload:
            m = payload["media_backend"]
            agent.persist("media_backend", m)
            agent.state["media_backend"] = m
            await agent.log(f'media_backend updated to {m} — say "reconnect" to apply')
        if "connection_mode" in payload:
            c = payload["connection_mode"]
            agent.persist("connection_mode", c)
            agent.state["connection_mode"] = _normalize_connection_mode(c) or "auto"
            await agent.log(f'connection_mode updated to {c} — say "reconnect" to apply')

    agent.subscribe("custom/reachy/config", on_config)

    # ---- Open robot with a small fallback chain ----
    # Wireless: daemon runs on the robot, accessible over WiFi.
    # Lite:     daemon runs on localhost (you started it manually).
    # NO HF App may be active on the robot — Apps own it exclusively.
    # ---- Build connection attempt ladder ----
    # media_backend resolution (first non-empty wins):
    #   1. runtime config via custom/reachy/config (agent.recall)
    #   2. REACHY_MEDIA_BACKEND in the environment/.env  (deterministic, no MQTT race)
    #   3. "" → SDK auto-detect.
    # Set "webrtc" to route speech to the ROBOT speaker: with the desktop app's
    # local daemon bridging to a Wireless robot, the WebRTC backend plays via the
    # daemon (play_sound -> /api/media/play_sound) instead of the host speakers.
    # The LOCAL/gstreamer backend always plays on this host.
    media_backend = (
        agent.recall("media_backend") or os.environ.get("REACHY_MEDIA_BACKEND") or ""
    ).strip()
    agent.state["media_backend"] = media_backend

    # ---- Connection mode (first non-empty wins) ----------------------------
    #   1. runtime config via custom/reachy/config: {"connection_mode": "..."}
    #   2. REACHY_CONNECTION_MODE in the environment/.env
    #   3. "" → auto (SDK autodetect: localhost first, then the robot).
    # Modes:
    #   network / wireless — talk straight to the robot over WiFi; never probe
    #                        localhost. Use this when NO control app / simulator
    #                        is running on this machine (the common wireless case).
    #   local / sim        — the Reachy Mini control app or simulator on
    #                        localhost (handy for a shared demo or headless sim).
    # NOTE: audio routing is decided by media_backend above, NOT by this mode.
    conn_mode = _normalize_connection_mode(
        agent.recall("connection_mode") or os.environ.get("REACHY_CONNECTION_MODE") or ""
    )
    agent.state["connection_mode"] = conn_mode or "auto"

    mini, last_err, tried = await _open_robot(agent)

    # Note: numpy + create_head_pose are stored regardless — they're pure helpers
    # that the LLM-planning path doesn't actually need a live robot to use.
    agent.state["np"] = np
    agent.state["create_head_pose"] = create_head_pose
    agent.state["mini"] = mini
    # Motor faults are only ever written to the daemon's log, so nothing sees
    # them unless something is reading it. Started here rather than on demand:
    # a warning is worth having the moment it happens, not when next asked.
    if mini is not None:
        _start_motor_fault_watch(agent)

    if mini is None:
        # Stay alive in a "disconnected" mode so HA-only commands keep working
        # and so users get a friendly "reachy not connected" instead of a crash
        # loop. Setup() must NOT raise — the supervisor would auto-restart us.
        await agent.log(
            f"Reachy daemon unreachable (tried: {tried}). Last error: {last_err}. "
            f"Agent stays up and refuses robot commands with 'reachy not connected'. "
            f'Power the robot on, then say "reconnect" (or publish {{"cmd":"reconnect"}} '
            f"to custom/reachy/cmd) to retry — no agent restart needed. "
            f"To run WITHOUT the Reachy Mini control app, publish "
            f'{{"connection_mode":"network","robot_host":"<ip>"}} to custom/reachy/config '
            f'and say "reconnect" — or set REACHY_CONNECTION_MODE=network and '
            f"REACHY_ROBOT_HOST=<robot ip/hostname> in .env, which is only re-read on "
            f"a restart.",
            level="warning",
        )

    # ---- HA is delegated to the home-assistant-agent ----
    # reachy never calls HA's REST API directly. The entity inventory (only used to
    # fill entity_ids in reactive robot-binds) is fetched lazily from the HA agent on
    # first plan — not here — to avoid a startup race if the HA agent isn't up yet.
    agent.state.setdefault("ha_entities", {})
    agent.state.setdefault("ha_entities_ts", 0.0)

    # ---- Optional: recorded emotion library (HF dataset) ----
    agent.state["moves"] = None
    agent.state["emotion_names"] = []
    try:
        from reachy_mini.motion.recorded_move import (  # pyright: ignore[reportMissingImports]
            RecordedMoves,
        )

        moves = RecordedMoves("pollen-robotics/reachy-mini-emotions-library")
        agent.state["moves"] = moves
        # Best-effort list, because the accessor is named differently across
        # SDK versions; the `moves` mapping is the fallback when none answer. An
        # empty list is not cosmetic - `list_emotions` returns it and the
        # planner concludes Reachy has no emotions to play.
        names = []
        for attr in ("list_moves", "available", "list", "keys"):
            f = getattr(moves, attr, None)
            if callable(f):
                try:
                    names = list(f())  # pyright: ignore[reportArgumentType]
                    break
                except Exception:
                    continue
        if not names:
            try:
                names = list(getattr(moves, "moves", {}) or {})
            except Exception:
                names = []
        agent.state["emotion_names"] = names
        await agent.log(f"Emotion library loaded ({len(names)} clips)")
    except Exception as e:
        await agent.log(f"Emotion library unavailable (continuing without): {e}", level="warning")

    # ---- Motion serialization ----
    # Only one interpolated motion in flight at a time — trajectories cannot overlap.
    # Fast set_target stream uses a separate non-locking path.
    agent.state["motion_lock"] = asyncio.Lock()
    agent.state["busy"] = False
    agent.state["awake"] = False
    agent.state["last_cmd"] = None
    # Execution receipts are internal by default. Users can opt in from chat
    # with "enable debug"; restarts always return to the quiet UX.
    agent.state["debug"] = False
    agent.state["_facing_body_yaw_deg"] = 0.0
    # Speech interrupt state — a 'shutup'/'stop' sets stop_speaking to cut a say.
    agent.state["stop_speaking"] = False
    agent.state["_speaking"] = False
    # Opt-in multi-turn voice session. The task and cancellation event are
    # process-local only; a restart always returns to idle.
    agent.state["conversation_session"] = None
    agent.state["conversation_state"] = "idle"

    # ---- Reactive bindings ----
    # bindings :: { topic_pattern: { 'when': {field: value}, 'do': {cmd, payload} } }
    # Loaded from persistent state so they survive restarts.
    agent.state["bindings"] = agent.recall("bindings") or {}

    # ---- Robot speaker volume (0-100, daemon-native; set via cmd=volume) ----
    # The daemon persists its own volume, so sync FROM it at startup (a GET, no
    # test sound) rather than re-applying our value. Fall back to persisted.
    agent.state["muted"] = bool(agent.recall("muted"))
    agent.state["premute_level"] = int(agent.recall("premute_level") or 100)
    agent.state["volume_level"] = int(agent.recall("volume_level") or 100)
    agent.state["motors_enabled"] = False

    # ---- Bring the robot up (audio report, volume sync, torque, wake) ----
    # Skipped while disconnected; cmd=reconnect runs the same helper on success.
    if mini is not None:
        await _bring_up_robot(agent)

    # ---- Direct command bus ----
    async def on_cmd(payload):
        cmd = (payload or {}).get("cmd") or (payload or {}).get("action")
        await _dispatch(agent, cmd, payload or {})

    # Single wildcard subscription: cmd is in the payload OR derivable from the topic suffix.
    # We also support per-verb topics (custom/reachy/cmd/<verb>) for convenience.
    async def on_verb_topic(payload):
        # Wired by the per-verb shim below.
        await _dispatch(agent, payload.get("_verb"), payload)

    # One topic, structured payload (preferred):
    agent.subscribe("custom/reachy/cmd", on_cmd)

    # Convenience: per-verb topics rewrite payload through the same dispatcher.
    for verb in (
        "wake",
        "sleep",
        "reconnect",
        "pose",
        "turn",
        "antennas",
        "gesture",
        "look_at",
        "look_pixel",
        "emotion",
        "motors",
        "diag",
        "set_pose",
        "bind",
        "unbind",
        "list_emotions",
        "stop",
        "shutup",
        "say",
        "volume",
        "camera",
        "describe",
        "look_behind",
        "look_around",
        "listen",
        "doa",
        "ask_voice",
        "conversation_start",
        "conversation_stop",
        "debug",
        "face_forward",
        "health",
    ):

        def _make_cb(v):
            async def cb(payload):
                p = dict(payload or {})
                p["_verb"] = v
                await _dispatch(agent, v, p)

            return cb

        agent.subscribe(f"custom/reachy/cmd/{verb}", _make_cb(verb))

    # ---- Reactive: listen to bound source topics ----
    # When the user (or planner) calls cmd=bind, we add a subscription on the fly.
    # On boot we re-establish all persisted bindings.
    for topic_pattern in list(agent.state["bindings"].keys()):
        await _wire_binding(agent, topic_pattern)

    # ---- Publish initial retained state ----
    await agent.publish(
        "custom/reachy/state",
        {
            "awake": agent.state["awake"],
            "busy": False,
            "emotions": agent.state["emotion_names"],
            "bindings": list(agent.state["bindings"].keys()),
            "robot_host": agent.state.get("robot_host") or "(autodetect)",
            "media_backend": agent.state.get("media_backend") or "default",
            "connection_mode": agent.state.get("connection_mode", "auto"),
            "volume_level": agent.state.get("volume_level", 100),
            "muted": bool(agent.state.get("muted")),
            "motors_enabled": bool(agent.state.get("motors_enabled")),
            "conversation_echo_control": bool(agent.state.get("conversation_echo_control")),
            "conversation_state": agent.state.get("conversation_state", "idle"),
            "ts": time.time(),
        },
    )

    await agent.log("reachy-mini ready")


async def process(agent):
    # Periodic heartbeat: republish the current state (retained) so dashboards
    # always show a fresh value. All actual work is callback-driven via subscribe().
    connected, reason = _is_connected(agent)
    await agent.publish(
        "custom/reachy/state",
        {
            "connected": connected,
            "reason": reason,
            "awake": agent.state.get("awake", False),
            "busy": agent.state.get("busy", False),
            "last_cmd": agent.state.get("last_cmd"),
            "emotions": agent.state.get("emotion_names", []),
            "bindings": list(agent.state.get("bindings", {}).keys()),
            "robot_host": agent.state.get("robot_host") or "(autodetect)",
            "media_backend": agent.state.get("media_backend") or "default",
            "connection_mode": agent.state.get("connection_mode", "auto"),
            "volume_level": agent.state.get("volume_level", 100),
            "muted": bool(agent.state.get("muted")),
            "motors_enabled": bool(agent.state.get("motors_enabled")),
            "conversation_echo_control": bool(agent.state.get("conversation_echo_control")),
            "conversation_state": agent.state.get("conversation_state", "idle"),
            "ts": time.time(),
        },
    )


_NL_SYSTEM = """You drive a Reachy Mini robot AND a Home Assistant smart home.
Convert the user's instruction to a JSON array of commands, executed in order.

Robot commands:
  {"cmd":"wake"}
  {"cmd":"sleep"}
  {"cmd":"stop"}
  {"cmd":"pose","yaw":<deg>,"pitch":<deg>,"roll":<deg>,"duration":<sec>}
  {"cmd":"turn","angle":<signed-deg>}       - relative body turn; left positive, right negative
  {"cmd":"gesture","name":"dance|nod|shake|wiggle|curious|turn_around"}
  {"cmd":"face_forward"}
  {"cmd":"look_behind"}                    - turn toward the rear, capture there, and stay facing rearward
  {"cmd":"antennas","left":<deg>,"right":<deg>,"duration":<sec>}
  {"cmd":"look_at","x":<m>,"y":<m>,"z":<m>,"duration":<sec>}
  {"cmd":"camera"}                         ← capture one still frame from the robot camera (returns an image)
  {"cmd":"describe"}                        ← LOOK through the camera and SPEAK a description of what is seen
  {"cmd":"describe","question":"<q>"}       ← answer a specific question about the current view
  {"cmd":"look_around"}                      ← pan the head across the room and describe the WHOLE scene
  {"cmd":"listen","duration":<sec>}        ← record a short mic clip (returns audio + direction of arrival)
  {"cmd":"ask_voice","duration":<sec>}     ← push-to-talk: transcribe one clip, ask Wactorz, speak answer
  {"cmd":"conversation_start"}              ← opt-in VAD multi-turn voice session
  {"cmd":"conversation_stop"}               ← safely stop the active voice session
  {"cmd":"say","text":"<what to say>"}
  {"cmd":"say","text":"<what to say>","voice":"<edge-tts voice name>"}
  {"cmd":"volume","preset":"whisper|normal|louder|presenter"}  ← human speaking modes (whisper=70, normal=85, louder=93, presenter=100)
  {"cmd":"volume","level":<0-100>}    ← absolute robot speaker volume; 100=loudest, 50=mid, 0=quietest
  {"cmd":"volume","delta":<+/-pts>}   ← relative change in level points (add to the CURRENT level shown below)
  {"cmd":"volume","mute":true|false}  ← mute (true) or restore the pre-mute volume (false)

Speech & volume rules:
- "say X", "tell them X", "speak X", "announce X" -> {"cmd":"say","text":"X"}.
- PRESETS (prefer these for how-to-speak phrasing, NOT raw numbers):
  "whisper" / "speak softly/quietly" -> preset whisper; "speak normally" / "normal/conversational" -> preset normal;
  "speak up" / "speak loudly" -> preset louder; "presenter/presentation mode" / "for the audience" / "fill the room" -> preset presenter.
- ABSOLUTE: "max/full/loudest volume" -> level 100; "minimum/quietest" -> level 0;
  "half volume" -> level 50; "set volume to N" / "N percent" -> level N.
  (The robot speaker is near-inaudible below ~65 — for any normal speech prefer a
   preset over a raw number, and never go under ~65 unless the user explicitly wants near-silent.)
- RELATIVE (use delta, NOT level — the current level is given below):
  "a bit/slightly/a little louder" -> delta +15; "louder" / "turn it up" -> delta +25; "much louder" -> level 100;
  "a bit quieter" -> delta -15; "quieter" / "turn it down" / "too loud" -> delta -25.
- MUTE: "mute" / "silence" / "be quiet" / "stop talking" -> mute true;
  "unmute" / "speak up again" / "sound back on" -> mute false.

Home Assistant - DELEGATED through Wactorz (you NEVER call HA directly):
  {"cmd":"ha","request":"<the smart-home request in plain natural language>"}
Use this for ANY smart-home thing - lights, switches, plugs, climate, scenes, sensors -
AND for creating / editing / listing / deleting Home Assistant AUTOMATIONS. Put the user's
full intent in 'request' (keep the room/device names they said). One-shot device control
is routed through main's actuator path; automations/listing/info are routed to the
home-assistant-agent. Do NOT pass entity_ids or HA service names. Examples of 'request'
values: "turn on the kitchen light", "turn the Tapo light pink",
"create an automation that turns the porch light off at sunrise", "list my automations".
Reactive bindings — "WHEN X happens, do Y". SPLIT by what Y is:
- Y is a ROBOT action (wake / sleep / say / emotion / pose / antennas) → reachy-side bind:
  {"cmd":"bind",
   "topic":"homeassistant/state_changes",
   "when":{"entity_id":"<entity_id from the inventory>","new_state.state":"on"|"off"},
   "do":{"cmd":"<wake|sleep|say|emotion|pose|...>"}}
  {"cmd":"unbind","topic":"homeassistant/state_changes"}   ← removes ALL rules on a topic
  A bind is a STANDING reachy rule — survives restarts, fires every time the HA event matches.
- Y is an HA action (turn a light/switch/device on or off, set climate, etc.) → do NOT bind;
  make it a real Home Assistant automation via the HA agent:
  {"cmd":"ha","request":"create an automation: when <trigger>, <ha action>"}

Expressive gestures (combine with say for rich expression — use say for speech, motion for feeling):
- "happy noise" / "happy" -> antennas wiggle up (left:60,right:60 then left:30,right:30 then left:60,right:60), head tilt up (pitch:-10)
- "sleepy noise" / "tired" -> head droop down slowly (pitch:25 duration:1.5), antennas droop (left:-30,right:-30 duration:1.2)
- "curious" -> head tilt (roll:15), antennas up
- "yes/nod" -> pitch up then down
- "no/shake" -> yaw left then right

Conventions:
- yaw: left=+, right=-. pitch: down=+. All degrees.
- Typical durations: 0.4 fast, 0.8 normal, 1.5 slow.
- Pause/hold pose is implicit between commands.
- "sleep" is a sleepy droop animation only — it does NOT power down.

Decision rules — CRITICAL:
- ANY smart-home / Home Assistant request — control OR automation — becomes a single
  {"cmd":"ha","request":"..."} that forwards the user's words to the right Wactorz HA route.
  Never emit entity_ids or HA service names yourself; never call HA directly.
- "turn on/off the light", "open/close the lamp", "lights on", "switch on", "set the
  thermostat", "list/create/delete an automation" → all are {"cmd":"ha","request":"..."}.
- "WHEN X" / "EVERY TIME X" / "if X happens" / "whenever" / "react to" / "when ... goes on/off"
  → the user wants a STANDING RULE, not a one-shot. SPLIT by the reaction:
    • reaction is a ROBOT action → {"cmd":"bind",...} (reachy-side).
    • reaction is an HA action  → {"cmd":"ha","request":"create an automation: when X, <action>"}.
  Do not also emit the one-shot action afterwards.
- A request without "when/whenever/every/if" is a one-shot — emit the action directly.
- ONLY add wake/sleep when the user asks for robot motion, an expression,
  or explicitly says wake/sleep. Pure HA requests do NOT need wake/sleep.
- SEEING / VISION: a question about what's in FRONT of the robot — "what do you
  see", "what is this", "who is here", "read this label", "what colour is X" —
  becomes {"cmd":"describe"} (or {"cmd":"describe","question":"<their question>"}).
  To look in a direction, add words like "look down/up/left" (describe aims the head).
  A request to survey the whole ROOM — "look around", "what's in the room",
  "describe the room", "scan the room" — becomes {"cmd":"look_around"} instead,
  which pans across several angles. NEVER emit camera+say for vision: camera only
  captures a raw frame, and say would invent a description; describe/look_around
  actually look and speak.
- REAR VIEW: "behind you", "look behind you", or "turn around and describe it"
  becomes one {"cmd":"look_behind"}. Never build pose(yaw=180) + gesture + describe:
  later steps recenter the view and photograph the user instead of the rear scene.
- If the request is not a robot, camera, microphone, explicit speech, or Home
  Assistant action, return an empty JSON array: []. General questions about the
  time, weather, calendar, news, knowledge, or other Wactorz capabilities are
  handled by the main orchestrator. NEVER invent their answer in a say command.
  Only emit say when the user explicitly asks Reachy to say/announce/repeat words.
Examples:
User: "turn on the light"
  -> [{"cmd":"ha","request":"turn on the light"}]

User: "turn off the lamp"
  -> [{"cmd":"ha","request":"turn off the lamp"}]

User: "wake up and turn on the light"
  -> [{"cmd":"wake"},
     {"cmd":"ha","request":"turn on the light"}]

User: "turn the light pink and act sleepy"
  -> [{"cmd":"ha","request":"turn the light pink"},
     {"cmd":"wake"},
     {"cmd":"pose","pitch":25,"duration":1.5},
     {"cmd":"antennas","left":-30,"right":-30,"duration":1.2},
     {"cmd":"sleep"}]
User: "when the light turns on, wake up"
  → [{"cmd":"bind","topic":"homeassistant/state_changes",
      "when":{"entity_id":"<LIGHT>","new_state.state":"on"},
      "do":{"cmd":"wake"}}]

User: "when the light turns off, go to sleep"
  → [{"cmd":"bind","topic":"homeassistant/state_changes",
      "when":{"entity_id":"<LIGHT>","new_state.state":"off"},
      "do":{"cmd":"sleep"}}]

User: "react to the light: wake when on, sleep when off"
  → [{"cmd":"bind","topic":"homeassistant/state_changes",
      "when":{"entity_id":"<LIGHT>","new_state.state":"on"},
      "do":{"cmd":"wake"}},
     {"cmd":"bind","topic":"homeassistant/state_changes",
      "when":{"entity_id":"<LIGHT>","new_state.state":"off"},
      "do":{"cmd":"sleep"}}]

User: "stop reacting to the light"
  → [{"cmd":"unbind","topic":"homeassistant/state_changes"}]

User: "what do you see?"   (and "what's in front of you", "describe the scene", "look around")
  → [{"cmd":"describe"}]

User: "how many people are in the room?"   (any question ABOUT the view)
  → [{"cmd":"describe","question":"how many people are in the room?"}]

User: "what time is it in Athens?"
  -> []

User: "summarize my calendar today"
  -> []

User: "wiggle your antennas"
  → [{"cmd":"wake"},
     {"cmd":"antennas","left":60,"right":60,"duration":0.4},
     {"cmd":"antennas","left":-30,"right":-30,"duration":0.4},
     {"cmd":"antennas","left":60,"right":60,"duration":0.4},
     {"cmd":"antennas","left":0,"right":0,"duration":0.4},
     {"cmd":"sleep"}]

User: "whisper hello"   (and "say X softly/quietly", "whisper that you love me")
  → [{"cmd":"volume","preset":"whisper"},
     {"cmd":"say","text":"hello"}]
  NOTE: "whisper X" means SET the whisper volume then SAY X out loud on the robot —
  NEVER answer it as chat/roleplay. Speak exactly what the user asked, do not invent
  extra words. Same shape for "say X loudly"/"announce X to the audience"
  → [{"cmd":"volume","preset":"presenter"},{"cmd":"say","text":"X"}].

Reply with ONLY the JSON array, no markdown, no prose."""


def _parse_volume_speak_compound(text):
    """Deterministically handle 'whisper X' / 'say X softly|loudly' so they NEVER
    get answered as chat. Returns a [volume-preset, say] command list, or None.

    'whisper hello' means: set the whisper speaker level, then SPEAK 'hello' aloud
    on the robot — not roleplay a whisper in text. Bare 'whisper'/'whisper mode'
    are already caught by the exact-match shortcuts before we get here.
    """
    t = (text or "").strip()
    # Skip preset-keyword-only tails like "whisper softly" → would say "softly".
    _STOP = {"mode", "softly", "quietly", "loudly", "back", "again", "now"}
    patterns = (
        (r"^whisper(?:\s+that)?\s+(.+)$", "whisper"),
        (
            r"^(?:say|announce|tell\s+them)\s+(.+?)\s+(?:softly|quietly|in\s+a\s+whisper)$",
            "whisper",
        ),
        (
            r"^(?:say|announce|tell\s+them)\s+(.+?)\s+(?:loudly|to\s+the\s+audience|for\s+everyone|for\s+the\s+room)$",
            "presenter",
        ),
    )
    for rx, preset in patterns:
        m = re.match(rx, t, re.IGNORECASE)
        if not m:
            continue
        said = m.group(1).strip()
        # Strip a single pair of surrounding quotes (straight or curly).
        if len(said) >= 2 and said[0] in "\"'“”‘’" and said[-1] in "\"'“”‘’":
            said = said[1:-1].strip()
        if said and said.lower() not in _STOP:
            return [{"cmd": "volume", "preset": preset}, {"cmd": "say", "text": said}]
    return None


def _parse_speak_compound(text):
    """Resolve literal speech requests locally instead of asking Main to role-play."""
    t = (text or "").strip()
    # STT commonly misspells Reachy in an address. The transcript normalizer fixes
    # voice turns; this prefix also protects typed/direct agent requests.
    aliases = (
        r"(?:reachy|richie|ritchie|richy|reachie|rechi|riti|rity|ritty|"
        r"ritsi|ritsie|ritsy|ritzi|ritzie|ritzy|rizzi|rizzy|lizzy|lizzie|lizi|lissy)"
    )
    t = re.sub(
        rf"^(?:(?:hey|hi|hello)\s+)?{aliases}\s*[,;:]?\s*",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"^please\s+", "", t, flags=re.IGNORECASE)

    volume_commands = _parse_volume_speak_compound(t)
    if volume_commands:
        return volume_commands

    # "Tell my brother/sister/friend ..." is a literal robot speech request.
    direct = re.match(
        r"^tell\s+(?:(?:my|the)\s+)?"
        r"(?:brother|bro|sister|friend|mother|mom|mum|father|dad|"
        r"him|her|them|someone|everybody|everyone)\s+"
        r"(?:that\s+)?(.+)$",
        t,
        re.IGNORECASE,
    )
    if not direct:
        return None

    said = direct.group(1).strip()
    dance = re.search(
        r"\s+(?:and|while)\s+(?:do(?:ing)?\s+)?(?:a\s+)?"
        r"(?:little\s+)?dance(?:\s+for\s+me)?[.!?]*$",
        said,
        re.IGNORECASE,
    )
    commands = []
    if dance:
        said = said[: dance.start()].rstrip(" ,.;")
    if said:
        commands.append({"cmd": "say", "text": said})
    if dance:
        commands.append({"cmd": "gesture", "name": "dance"})
    return commands or None


def _is_explicit_speech_request(text):
    """True only when the user actually asked Reachy to speak supplied words."""
    t = (text or "").strip()
    patterns = (
        r"^(?:please\s+)?(?:say|announce|repeat|speak)\b",
        r"^(?:can|could|would|will)\s+you\s+(?:please\s+)?"
        r"(?:say|announce|repeat|speak)\b",
        r"^(?:please\s+)?tell\s+(?:them|everyone|the\s+room)\b",
        r"\btell\s+(?:(?:my|the)\s+)?"
        r"(?:brother|bro|sister|friend|mother|mom|mum|father|dad|him|her|them)\b",
    )
    return any(re.search(pattern, t, re.IGNORECASE) for pattern in patterns)


def _is_invented_say_plan(cmds, original_text):
    """Reject a classifier-generated answer disguised as a robot say command.

    The local planner decides robot actions; Wactorz main answers general
    requests. Some LLMs nevertheless answer a question by emitting a lone say
    command. Treat that as unhandled unless the user explicitly requested speech.
    """
    if not isinstance(cmds, list) or len(cmds) != 1:
        return False
    command = cmds[0]
    if not isinstance(command, dict):
        return False
    cmd = (command.get("cmd") or command.get("action") or "").lower().strip()
    return cmd == "say" and not _is_explicit_speech_request(original_text)


def _explicit_interface_request(text):
    """Return text after an explicit request to route through Wactorz main."""
    raw = (text or "").strip()
    patterns = (
        r"^(?:please\s+)?ask\s+wactorz(?:\s+to)?[:,]?\s+(.+)$",
        r"^wactorz[:,]\s*(.+)$",
        r"^(?:please\s+)?use\s+wactorz\s+to\s+(.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, raw, re.IGNORECASE)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


def _extract_ha_request(text):
    """Best-effort smart-home clause for malformed planner HA commands."""
    raw = (text or "").strip()
    if not raw:
        return ""
    ha_words = (
        "light",
        "lamp",
        "switch",
        "plug",
        "scene",
        "thermostat",
        "climate",
        "fan",
        "cover",
        "blind",
        "curtain",
        "heater",
        "ac",
        "home assistant",
    )
    if not any(w in raw.lower() for w in ha_words):
        return raw
    split = re.split(
        r"\b(?:and|then)\s+(?=(?:act|look|be|go|get|do|wiggle|wake|sleep|say|nod|shake|tilt|move)\b)",
        raw,
        maxsplit=1,
        flags=re.IGNORECASE,
    )
    return split[0].strip(" ,.;") or raw


def _repair_ha_commands(cmds, original_text):
    """Ensure planner-generated HA steps have something actionable to forward."""
    if not isinstance(cmds, list):
        return cmds
    fallback = _extract_ha_request(original_text)
    for c in cmds:
        if not isinstance(c, dict):
            continue
        cmd = (c.get("cmd") or c.get("action") or "").lower().strip()
        if cmd != "ha":
            continue
        has_request = any(c.get(k) for k in ("request", "text", "message", "query"))
        has_legacy = c.get("service") and c.get("entity_id")
        if not has_request and not has_legacy and fallback:
            c["request"] = fallback
    return cmds


async def _nl_to_commands(agent, text: str):
    if agent.llm is None:
        return None
    # Light/switch inventory — ONLY used to fill entity_ids in reactive robot-binds
    # (HA control/automation is delegated as natural language, no ids needed). Fetched
    # lazily from the home-assistant-agent and cached; retried at most once a minute
    # while empty so a not-yet-ready HA agent eventually populates it.
    ents = agent.state.get("ha_entities") or {}
    have = ents.get("lights") or ents.get("switches")
    low_text = str(text).lower()
    needs_inventory = any(
        phrase in low_text
        for phrase in (
            "bind",
            "whenever",
            "when the",
            "when my",
            "react to",
            "follow the",
        )
    )
    if needs_inventory and not have and (time.time() - agent.state.get("ha_entities_ts", 0.0)) > 60:
        ents = await _ha_entities_via_agent(agent)
        agent.state["ha_entities"] = ents
        agent.state["ha_entities_ts"] = time.time()
    lines = []
    for kind, items in (("Lights", ents.get("lights", [])), ("Switches", ents.get("switches", []))):
        if not items:
            continue
        lines.append(f"\n{kind} (entity_id for binds):")
        for it in items:
            lines.append(f"  {it['entity_id']:50s}  ({it['name']})")
    ha_section = (
        "\n".join(lines)
        if lines
        else "\n(no entity inventory yet — for binds, use the device name the user gave)"
    )
    # Inject the current speaker volume so the LLM can do relative ("a bit louder")
    # and mute/unmute requests correctly.
    cur_level = agent.state.get("volume_level", 100)
    muted = bool(agent.state.get("muted"))
    vol_section = (
        f"\n\nCurrent speaker volume: level {cur_level} (0-100), muted={'yes' if muted else 'no'}."
    )
    system_with_ents = (
        _NL_SYSTEM + "\n\nEntity inventory (for binds only):" + ha_section + vol_section
    )
    raw = await agent.llm.chat(text, system=system_with_ents)
    raw = (raw or "").strip()
    if raw.startswith("```"):
        # Strip ```json ... ``` fences
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
            raw = raw.removeprefix("json")
    raw = raw.strip()
    # Try direct parse, else find the first JSON array in the response
    try:
        cmds = json.loads(raw)
    except Exception:
        i = raw.find("[")
        j = raw.rfind("]")
        if i >= 0 and j > i:
            try:
                cmds = json.loads(raw[i : j + 1])
            except Exception:
                return None
        else:
            return None
    if isinstance(cmds, list):
        return cmds
    if isinstance(cmds, dict):
        return [cmds]
    return None


def _embodied_command_for_text(text: str):
    """Return a deterministic local command for obvious embodiment requests."""
    low = str(text or "").lower()
    normalized = low.strip().rstrip("!.?")
    if normalized in ("enable debug", "debug on", "show debug", "show action sequences"):
        return {"cmd": "debug", "enabled": True}
    if normalized in ("disable debug", "debug off", "hide debug", "hide action sequences"):
        return {"cmd": "debug", "enabled": False}

    room_scope = bool(
        re.search(
            r"\b(around (?:the )?room|around you|around here|surroundings?|"
            r"whole room|in the room|(?:the|this|my) room)\b",
            normalized,
        )
    )
    vision_intent = bool(
        re.search(r"\b(describe|look|scan|survey|see|show|tell|what)\b", normalized)
    )
    if room_scope and vision_intent:
        return {"cmd": "look_around"}

    if normalized in (
        "what do you see",
        "what can you see",
        "what do you see right now",
        "tell me what you see",
        "what's in the room",
        "what is in the room",
        "describe the room",
        "look around",
        "look around the room",
        "scan the room",
        "look around you",
        "check out the room",
        "survey the room",
    ):
        return {"cmd": "look_around"}
    if normalized in (
        "what's in front of you",
        "what is in front of you",
        "describe what you see",
        "describe the scene",
        "what's there",
        "what is this",
        "look at this",
    ):
        return {"cmd": "describe"}

    greek_voice = any(stem in low for stem in ("φων", "έντασ", "τόνο"))
    greek_softer = any(stem in low for stem in ("χαμήλω", "χαμηλώ", "πιο σιγά", "σιγανά"))
    greek_louder = any(stem in low for stem in ("δυνάμω", "πιο δυνατά", "ανέβασε"))
    if greek_voice and greek_softer:
        delta = -15 if "λίγο" in low else -25
        return {"cmd": "volume", "delta": delta}
    if greek_voice and greek_louder:
        delta = 15 if "λίγο" in low else 25
        return {"cmd": "volume", "delta": delta}

    english_voice = bool(re.search(r"\b(voice|volume|speak|talk)\b", low))
    if english_voice and re.search(r"\b(lower|quieter|softer|turn (?:it|your voice) down)\b", low):
        delta = -15 if re.search(r"\b(a little|a bit|slightly)\b", low) else -25
        return {"cmd": "volume", "delta": delta}
    if english_voice and re.search(r"\b(louder|speak up|turn (?:it|your voice) up)\b", low):
        delta = 15 if re.search(r"\b(a little|a bit|slightly)\b", low) else 25
        return {"cmd": "volume", "delta": delta}

    turn_prefix = (
        r"(?:(?:can|could|would) you\s+)?(?:please\s+)?"
        r"(?:turn|rotate)(?:\s+yourself)?"
    )
    turn_suffix = r"(?:\s+please)?"
    direction_first = re.fullmatch(
        rf"{turn_prefix}\s+(left|right)"
        rf"(?:\s+(?:by\s+)?(\d+(?:\.\d+)?)\s*(?:degrees?|°)?)?"
        rf"{turn_suffix}",
        normalized,
    )
    amount_first = re.fullmatch(
        rf"{turn_prefix}\s+(\d+(?:\.\d+)?)\s*(?:degrees?|°)?\s+(left|right)"
        rf"{turn_suffix}",
        normalized,
    )
    if direction_first or amount_first:
        if direction_first:
            direction = direction_first.group(1)
            magnitude = float(direction_first.group(2) or 45.0)
        else:
            magnitude = float(amount_first.group(1))  # pyright: ignore[reportOptionalMemberAccess]
            direction = amount_first.group(2)  # pyright: ignore[reportOptionalMemberAccess]
        magnitude = max(1.0, min(155.0, magnitude))
        angle = magnitude if direction == "left" else -magnitude
        return {"cmd": "turn", "angle": int(angle) if angle.is_integer() else angle}

    if re.search(
        r"\b(behind you|look behind(?: you)?|what(?:'s| is) behind you|check behind you)\b",
        low,
    ):
        return {"cmd": "look_behind", "question": "What is behind you?"}
    if re.search(r"\b(turn around|face (?:the )?other way)\b", low) and re.search(
        r"\b(describe|what|tell me|look|see)\b", low
    ):
        return {"cmd": "look_behind", "question": str(text).strip()}
    if re.search(r"\b(face me|face forward|turn back|turn back around|look at me)\b", low):
        return {"cmd": "face_forward"}
    if re.search(r"\b(turn around|face (?:the )?other way|spin around)\b", low):
        return {"cmd": "gesture", "name": "turn_around"}
    if re.search(r"\b(dance|boogie|robot shuffle)\b", low):
        return {"cmd": "gesture", "name": "dance"}
    if re.search(r"\b(nod|nod your head)\b", low):
        return {"cmd": "gesture", "name": "nod"}
    if re.search(r"\b(shake your head|head shake)\b", low):
        return {"cmd": "gesture", "name": "shake"}
    if re.search(r"\b(wiggle|move)\b.*\b(antenna|antennas)\b", low):
        return {"cmd": "gesture", "name": "wiggle"}
    if re.search(r"\b(look curious|be curious|curious gesture)\b", low):
        return {"cmd": "gesture", "name": "curious"}
    return None


async def handle_task(agent, payload):
    # Direct send_to(reachy-mini, {...}) — same dispatch as MQTT.
    # A caller wraps user text as: {"text": "...", "_task_id": ..., "reply_to": ...}
    payload = payload or {}
    _tid = payload.get("_task_id") or payload.get("task")

    if "cmd" not in payload and "action" not in payload:
        # Pull NL text from any common field. Planner-generated agents often send
        # dicts like {"gesture":"shrug","description":"do a shrug"} — we accept
        # anything that looks like a description so the NL planner can interpret it.
        text = (
            payload.get("text")
            or payload.get("content")
            or payload.get("message")
            or payload.get("query")
            or payload.get("description")
            or payload.get("gesture")
            or payload.get("instruction")
        )
        # If we have a dict with multiple text-ish fields, glue them — gives the
        # LLM more context to decide what gesture was meant.
        if not text and isinstance(payload, dict):
            text_bits = [
                str(v)
                for k, v in payload.items()
                if k
                not in ("_task_id", "_reply_to", "task", "command", "name", "context", "source")
                and isinstance(v, str)
            ]
            if text_bits:
                text = " ".join(text_bits)
        if isinstance(text, str):
            stripped = text.strip()
            interface_text = _explicit_interface_request(stripped)
            if interface_text:
                await agent.log(
                    f"routing explicit interface request to main: {interface_text[:80]}"
                )
                bridged = await _bridge_to_main(agent, interface_text, _tid)
                if bridged is not None:
                    return bridged
                return {
                    "ok": False,
                    "cmd": "bridge",
                    "error": "Wactorz main is unavailable",
                    "result": "I could not reach Wactorz main.",
                    "_task_id": _tid,
                    "task": _tid,
                }
            # Structured JSON object: if it has cmd/action, adopt as the new payload.
            # If it's a payload-dict without cmd (e.g. planner-generated {gesture,
            # description, context}), flatten its text-ish fields back to NL input
            # so the LLM has something natural to work with.
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, dict):
                        if "cmd" in parsed or "action" in parsed:
                            payload = parsed
                        else:
                            # Re-extract NL text from the inner dict
                            inner = (
                                parsed.get("description")
                                or parsed.get("text")
                                or parsed.get("instruction")
                                or parsed.get("gesture")
                                or parsed.get("message")
                                or parsed.get("query")
                            )
                            if not inner:
                                inner = " ".join(
                                    str(v)
                                    for k, v in parsed.items()
                                    if isinstance(v, str)
                                    and k
                                    not in (
                                        "_task_id",
                                        "_reply_to",
                                        "task",
                                        "command",
                                        "source",
                                        "context",
                                    )
                                )
                            stripped = inner.strip() if isinstance(inner, str) else stripped
                except Exception:
                    pass
            if "cmd" not in payload and "action" not in payload and stripped:
                low = stripped.lower().rstrip("!.?")
                # Single-verb shortcuts (no LLM call needed)
                embodied = (
                    None
                    if _parse_speak_compound(stripped)
                    else _embodied_command_for_text(stripped)
                )
                if embodied:
                    payload = embodied
                elif low in ("wake", "wake up"):
                    payload = {"cmd": "wake"}
                elif low in ("sleep", "go to sleep"):
                    payload = {"cmd": "sleep"}
                elif low == "stop":
                    payload = {"cmd": "shutup"}
                elif low in ("list emotions", "emotions"):
                    payload = {"cmd": "list_emotions"}
                # Perception: grab a camera frame / a short mic clip (no LLM needed).
                elif low in (
                    "what do you see",
                    "what can you see",
                    "what's in front of you",
                    "what is in front of you",
                    "describe what you see",
                    "describe the scene",
                    "what do you see right now",
                    "tell me what you see",
                    "what's there",
                ):
                    payload = {"cmd": "describe"}
                # Room sweep — pan across a few angles and describe the whole room.
                elif low in (
                    "look around",
                    "look around the room",
                    "scan the room",
                    "what's in the room",
                    "what is in the room",
                    "describe the room",
                    "look around you",
                    "check out the room",
                    "survey the room",
                ):
                    payload = {"cmd": "look_around"}
                # Follow-up to the brief look — ask for the full description.
                elif low in (
                    "look closer",
                    "closer look",
                    "take a closer look",
                    "describe in detail",
                    "in detail",
                    "more detail",
                    "tell me more",
                    "describe more",
                    "look again",
                    "look closely",
                    "what else do you see",
                ):
                    payload = {"cmd": "describe", "detail": True}
                # A bare 'yes' right after "Want me to look closer?" means: do it.
                # Only hijacks the affirmative while that offer is still open, so a
                # normal 'yes' elsewhere still falls through to the planner.
                elif low in _AFFIRMATIVES and _pending_look_closer(agent):
                    agent.state["_pending_detail"] = None
                    payload = {"cmd": "describe", "detail": True}
                elif low in (
                    "health",
                    "how are you feeling",
                    "how do you feel",
                    "are you ok",
                    "are you okay",
                    "are you overheating",
                    "temperature",
                    "what is your temperature",
                    "are you hot",
                    "battery",
                    "battery level",
                    "how is your battery",
                    "hardware status",
                    "any faults",
                ):
                    payload = {"cmd": "health"}
                elif low in (
                    "take a photo",
                    "take a picture",
                    "take a snapshot",
                    "snapshot",
                    "photo",
                    "picture",
                    "capture",
                    "camera",
                ):
                    payload = {"cmd": "camera"}
                # Interruptible conversation is opt-in: the mic hears Reachy's
                # own speaker, so barge-in is only safe where echo cancellation
                # works. Asking for it in words beats hand-writing the JSON,
                # which was the only way to reach the flag.
                elif low in (
                    "start conversation with interruption",
                    "start a conversation with interruption",
                    "conversation mode with interruption",
                    "start interruptible conversation",
                    "start conversation with barge in",
                    "start conversation with barge-in",
                    "begin conversation with interruption",
                ):
                    payload = {"cmd": "conversation_start", "barge_in": True}
                elif low in (
                    "start conversation",
                    "start a conversation",
                    "conversation mode",
                    "begin conversation",
                ):
                    payload = {"cmd": "conversation_start"}
                elif low in _CONVERSATION_STOP_COMMANDS:
                    payload = {"cmd": "conversation_stop"}
                elif low in (
                    "listen and ask wactorz",
                    "listen then ask wactorz",
                    "ask wactorz by voice",
                    "voice ask wactorz",
                    "push to talk",
                    "push-to-talk",
                ):
                    payload = {"cmd": "ask_voice"}
                elif low in (
                    "listen",
                    "record",
                    "record audio",
                    "take a listen",
                    "what do you hear",
                ):
                    payload = {"cmd": "listen"}
                # Motor torque toggle — the control app's enable/disable.
                elif low in (
                    "enable motors",
                    "motors on",
                    "enable your motors",
                    "turn on motors",
                    "turn your motors on",
                    "stiffen",
                ):
                    payload = {"cmd": "motors", "on": True}
                elif low in (
                    "disable motors",
                    "motors off",
                    "disable your motors",
                    "turn off motors",
                    "turn your motors off",
                    "go limp",
                    "relax your motors",
                ):
                    payload = {"cmd": "motors", "on": False}
                # Re-open the robot link. Deterministic keywords (not the LLM
                # planner) because these are asked exactly when the robot is
                # offline — the planner would otherwise fall through to the main
                # bridge, which has no robot state and answers connection
                # questions with a confident, invented "I'm reconnected!".
                elif low in (
                    "reconnect",
                    "re-connect",
                    "connect",
                    "reconnect reachy",
                    "connect to reachy",
                    "try again",
                    "try to connect",
                    "try connecting",
                    "try connecting again",
                    "retry",
                    "retry connection",
                    "reconnect to the robot",
                    "connection",
                    "reconnect now",
                ):
                    payload = {"cmd": "reconnect"}
                elif low in (
                    "reconnect force",
                    "force reconnect",
                    "reconnect anyway",
                    "reconnect force it",
                ):
                    payload = {"cmd": "reconnect", "force": True}
                elif low in (
                    "diag",
                    "diagnostics",
                    "diagnose",
                    "self test",
                    "self-test",
                    "why won't you move",
                    "why wont you move",
                    "why aren't you moving",
                    "why arent you moving",
                    "run diagnostics",
                ):
                    payload = {"cmd": "diag"}
                # "Stop talking NOW" — cut the current utterance (not persistent
                # mute). This is the shutup the user reaches for mid-sentence.
                elif low in (
                    "shut up",
                    "shutup",
                    "stop talking",
                    "stop speaking",
                    "be quiet",
                    "quiet",
                    "hush",
                    "enough",
                    "silence",
                    "stop it",
                    "that's enough",
                    "thats enough",
                ):
                    payload = {"cmd": "shutup"}
                # Persistent volume mute (stays muted until unmuted).
                elif low in ("mute", "mute yourself"):
                    payload = {"cmd": "volume", "mute": True}
                elif low in ("unmute", "sound on", "speak up", "speak up again"):
                    payload = {"cmd": "volume", "mute": False}
                elif low in ("louder", "turn it up", "volume up", "speak louder"):
                    payload = {"cmd": "volume", "delta": 25}
                elif low in ("quieter", "turn it down", "volume down", "too loud"):
                    payload = {"cmd": "volume", "delta": -25}
                elif low in ("max volume", "full volume", "loudest", "maximum volume"):
                    payload = {"cmd": "volume", "level": 100}
                # Human-friendly speaking modes (whisper/normal/louder/presenter).
                elif low in ("whisper", "whisper mode", "speak softly", "speak quietly", "softly"):
                    payload = {"cmd": "volume", "preset": "whisper"}
                elif low in ("normal volume", "speak normally", "normal", "conversational"):
                    payload = {"cmd": "volume", "preset": "normal"}
                elif low in (
                    "presenter mode",
                    "presentation mode",
                    "presentation",
                    "audience",
                    "audience mode",
                    "fill the room",
                ):
                    payload = {"cmd": "volume", "preset": "presenter"}
                else:
                    # Deterministic 'whisper X' / 'say X softly|loudly' first so
                    # they actually drive the robot (volume + speak) instead of the
                    # LLM occasionally answering them as chat/roleplay. Falls back to
                    # the NL planner for everything else (60s budget — an LLM call +
                    # a few short motions fits comfortably).
                    cmds = _parse_speak_compound(stripped) or await _nl_to_commands(agent, stripped)
                    cmds = _repair_ha_commands(cmds, stripped)
                    if not cmds or _is_invented_say_plan(cmds, stripped):
                        # Not a robot/HA command — act as an interface: pipe it
                        # through the main orchestrator and voice the answer.
                        bridged = await _bridge_to_main(agent, stripped, _tid)
                        if bridged is not None:
                            return bridged
                        # Bridge unavailable — fall back to a clear result. Do
                        # not return the raw input under ``text``: a caller's
                        # reply picker could otherwise echo the user's words.
                        return {
                            "ok": False,
                            "error": "could not parse instruction",
                            "result": (
                                f'I couldn\'t turn "{stripped}" into a robot action. '
                                'Try something like "wake", "say hello", '
                                '"whisper hi", or "presenter mode".'
                            ),
                            "_task_id": _tid,
                            "task": _tid,
                        }
                    # If reachy is offline, skip robot commands but still run HA ones.
                    ok_link, link_reason = _is_connected(agent)
                    steps = []
                    skipped = []
                    for c in cmds:
                        if not isinstance(c, dict):
                            continue
                        c_cmd = c.get("cmd") or c.get("action")
                        if not ok_link and c_cmd not in ("ha", "list_emotions", "reconnect"):
                            skipped.append(c_cmd)
                            continue
                        r = await _dispatch(agent, c_cmd, c, return_result=True)
                        steps.append(r)
                    # Build a human-readable summary of what actually ran.
                    summary_parts = []
                    failures = []
                    spoken_replies = []
                    step_iter = iter(steps)
                    for c in cmds:
                        if not isinstance(c, dict):
                            continue
                        cc = c.get("cmd") or c.get("action") or "?"
                        if cc == "ha":
                            label = f"ha:{c.get('request') or c.get('service') or '?'}"
                        elif cc == "say":
                            label = "say"
                        elif cc == "pose":
                            label = f"pose(y={c.get('yaw', 0)},p={c.get('pitch', 0)})"
                        elif cc == "antennas":
                            label = f"antennas(l={c.get('left', '?')},r={c.get('right', '?')})"
                        else:
                            label = str(cc)

                        if cc in skipped:
                            summary_parts.append(f"{label} SKIPPED")
                            continue
                        step = next(step_iter, None)
                        if (
                            isinstance(step, dict)
                            and cc in ("say", "describe")
                            and step.get("said")
                        ):
                            spoken_replies.append(str(step["said"]))
                        if isinstance(step, dict) and cc == "ha" and step.get("ha_result"):
                            ha_text = str(step["ha_result"]).strip()
                            if ha_text:
                                label = f"{label} ({ha_text[:100]})"
                        if isinstance(step, dict) and step.get("ok") is False:
                            error = step.get("error") or "failed"
                            failures.append({"cmd": cc, "error": error})
                            summary_parts.append(f"{label} FAILED ({error})")
                        else:
                            summary_parts.append(label)

                    successes = sum(
                        1
                        for step in steps
                        if not (isinstance(step, dict) and step.get("ok") is False)
                    )
                    single_result = None
                    if len(cmds) == 1 and len(steps) == 1 and isinstance(steps[0], dict):  # noqa: SIM102
                        if steps[0].get("ok") is not False and steps[0].get("result"):
                            single_result = str(steps[0]["result"])
                    receipt = f"ran {successes} of {len(cmds)}: [{' -> '.join(summary_parts)}]"
                    if skipped:
                        receipt += f"  (skipped {len(skipped)}: {link_reason})"

                    if agent.state.get("debug"):
                        result_msg = receipt if len(cmds) > 1 else (single_result or receipt)
                        _queue_spoken_replies(agent, spoken_replies)
                    elif failures:
                        result_msg = f"I couldn't complete that: {failures[0]['error']}."
                    elif skipped:
                        result_msg = f"I couldn't complete that: {link_reason}."
                    elif spoken_replies:
                        # The final human-facing output is the useful answer. Keep
                        # the execution plan in structured fields, not chat bubbles.
                        result_msg = "\n\n".join(spoken_replies)
                    elif single_result:
                        result_msg = single_result
                    else:
                        natural_results = [
                            str(step.get("result")).strip()
                            for step in steps
                            if isinstance(step, dict) and step.get("result")
                        ]
                        result_msg = natural_results[-1] if natural_results else "Done."
                    return {
                        "ok": not failures and not skipped,
                        "cmd": "nl",
                        "steps_run": successes,
                        "failed": failures,
                        "skipped": skipped,
                        "plan": cmds,
                        "result": result_msg,
                        "_task_id": _tid,
                        "task": _tid,
                    }
    cmd = payload.get("cmd") or payload.get("action")
    # Friendly fail-fast for ROBOT commands when reachy isn't connected.
    # HA-only commands ("ha") still work — that's the whole point of the
    # "stay alive in disconnected mode" design. "reconnect" is exempt for the
    # obvious reason: it is the command you reach for BECAUSE we're offline.
    if cmd not in ("ha", "list_emotions", "conversation_stop", "reconnect", "debug", None):
        ok, reason = _is_connected(agent)
        if not ok:
            return {
                "ok": False,
                "error": reason,
                "result": reason,
                "cmd": cmd,
                "_task_id": _tid,
                "task": _tid,
            }
    result = await _dispatch(agent, cmd, payload, return_result=True)
    if isinstance(result, dict):
        if cmd in ("stop", "shutup") and not agent.state.get("debug"):
            result["_suppress_reply"] = True
        if cmd in ("say", "describe") and result.get("said"):
            if agent.state.get("debug"):
                _queue_spoken_replies(agent, [str(result["said"])])
                result["result"] = f"ran 1 of 1: [{cmd}]"
            else:
                result["result"] = str(result["said"])
        result.setdefault("_task_id", _tid)
        result.setdefault("task", _tid)
    return result


def _queue_spoken_replies(agent, spoken_replies: list[str]) -> None:
    texts = [str(t).strip() for t in (spoken_replies or []) if str(t).strip()]
    if not texts:
        return

    async def _send():
        await asyncio.sleep(0.08)
        await agent.notify_user("\n\n".join(texts))

    agent.run_in_background(_send())


_AFFIRMATIVES = {
    "yes",
    "yeah",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "please",
    "yes please",
    "go on",
    "do it",
    "please do",
    "go ahead",
    "yes go on",
    "sure do",
    "yeah go on",
}


def _pending_look_closer(agent) -> bool:
    """True if a 'Want me to look closer?' offer is still open (within 60s), so a
    bare 'yes' means 'give me the detailed description' rather than being echoed.
    """
    ts = agent.state.get("_pending_detail")
    return bool(ts and (time.time() - ts) < 60)


_REACHY_NAME_VARIANTS = (
    # "ricci" and "richi" are what the large-v3-turbo model actually returns for
    # "Reachy" on this setup - confirmed by round-tripping edge-tts speech back
    # through the recogniser, not guessed from the shape of the other variants.
    r"(?:reachy|richie|ritchie|richy|reachie|rechi|ricci|richi|"
    r"riti|rity|ritty|ritsi|ritsie|ritsy|ritzi|ritzie|ritzy|rizzi|rizzy|lizzy|lizzie|lizi|lissy)"
)


def _normalize_reachy_transcript(text):
    """Repair common STT spellings when the user is addressing Reachy."""
    value = str(text or "").strip()
    variants = _REACHY_NAME_VARIANTS
    if re.fullmatch(
        rf"(?:e|a)\s+{variants}[.!?]?",
        value,
        flags=re.IGNORECASE,
    ):
        return "Hey Reachy"
    value = re.sub(
        rf"\b(hey|hi|hello|okay|ok|listen)\s*,?\s+{variants}\b",
        lambda match: f"{match.group(1)} Reachy",
        value,
        flags=re.IGNORECASE,
    )
    if re.fullmatch(variants + r"[.!?]?", value, flags=re.IGNORECASE):
        return "Reachy"
    value = re.sub(r"\bman light\b", "main light", value, flags=re.IGNORECASE)
    value = re.sub(
        r"\bthen\s+on\s+the\s+light\b",
        "turn on the light",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"\bthey(?:'ve| have)\s+known\s+the\s+light\b",
        "turn on the light",
        value,
        flags=re.IGNORECASE,
    )


def _explicit_user_name(text: str):
    """Return a name only from an unmistakable user naming statement."""
    match = re.search(
        r"\b(?:my name is|please call me|call me)\s+([^\W\d_][\w'’-]{0,39})\b",
        str(text or ""),
        flags=re.IGNORECASE | re.UNICODE,
    )
    return match.group(1).casefold() if match else None


def _sanitize_reachy_identity_reply(text: str, user_text: str = "") -> str:
    """Keep the robot named Reachy and never mistake that name for the user."""
    value = str(text or "")
    variants = _REACHY_NAME_VARIANTS
    value = re.sub(
        rf"\b(?:and\s+)?it(?:'s| is)\s+{variants}\s*,?\s*not\s+{variants}\b[.!]?",
        "I'm Reachy.",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        rf"\b(?:I(?:'m| am)|my name is)\s+{variants}\b",
        "I'm Reachy",
        value,
        flags=re.IGNORECASE,
    )

    explicit_name = _explicit_user_name(user_text)
    explicit_is_alias = bool(
        explicit_name and re.fullmatch(variants, explicit_name, flags=re.IGNORECASE)
    )
    if not explicit_is_alias:
        value = re.sub(
            rf"\b(hey|hi|hello)\s*,?\s+{variants}\b",
            lambda match: match.group(1),
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(
            rf",\s*{variants}(?=[?!.])",
            "",
            value,
            flags=re.IGNORECASE,
        )
    return value


def _natural_actuation_speech(value: str, user_text: str) -> str | None:
    """Turn technical HA acknowledgements into short human confirmations."""
    match = re.search(
        r"\b(?:done|success)\s*:\s*([a-z_]+)\.([a-z_]+)\s*->\s*[^\s]+",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    domain, service = match.group(1).lower(), match.group(2).lower()
    request = str(user_text or "").lower()
    if domain == "light":
        if service == "turn_off":
            return "Okay, the light is off."
        if service == "turn_on":
            colors = (
                "pink",
                "red",
                "orange",
                "yellow",
                "green",
                "cyan",
                "blue",
                "purple",
                "violet",
                "white",
                "warm white",
                "cool white",
            )
            color = next((item for item in colors if item in request), None)
            if color:
                return f"Okay, the light is {color}."
            if any(word in request for word in ("dim", "bright", "brightness")):
                return "Okay, I've adjusted the light."
            return "Okay, the light is on."
    return "Okay, done."


def _voice_workflow_reply(value):
    """Hide planner internals while keeping voice approvals unambiguous."""
    lowered = str(value or "").lower()
    if "proposed pipeline" in lowered and "reply" in lowered and "approve" in lowered:
        return (
            "I can set that up as an automation. Say yes to approve it, "
            "no to cancel, or tell me what to change."
        )
    if "approved plan" in lowered:
        return "Done. The automation is running."
    if "discarded plan" in lowered:
        return "Okay, I cancelled that automation."
    if "revising plan" in lowered and "superseded" in lowered:
        return "Okay, I updated the automation. Say yes to approve it or no to cancel."
    return None


def _voice_friendly_reply(text, limit=None, user_text=""):
    """Turn a visual dashboard answer into natural human-only spoken text."""
    value = _sanitize_reachy_identity_reply(text, user_text=user_text).strip()
    if not value:
        return ""
    workflow = _voice_workflow_reply(value)
    if workflow:
        return workflow
    actuation = _natural_actuation_speech(value, user_text)
    if actuation:
        return actuation
    value = re.sub(r"```[\s\S]*?```", " I've put the code in Wactorz chat. ", value)
    value = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", value)
    # Italic role-play directions and emoji are visual flourishes, not speech.
    value = re.sub(r"(?<!\*)\*[^*\n]+\*(?!\*)", " ", value)
    value = "".join(
        " "
        if (
            0x1F1E6 <= ord(ch) <= 0x1FAFF
            or 0x2600 <= ord(ch) <= 0x27BF
            or ord(ch) in (0x200D, 0xFE0F)
        )
        else ch
        for ch in value
    )
    had_url = bool(re.search(r"https?://\S+", value))
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"(?m)^\s{0,3}(?:#{1,6}\s*|[-*+]\s+|\d+[.)]\s+)", "", value)
    value = re.sub(r"[*_~`]+", "", value)
    value = " ".join(value.split())
    if had_url:
        value = f"{value} The link is in Wactorz chat.".strip()
    if limit is None or len(value) <= limit:
        return value or "Okay."
    cut = max(value.rfind(mark, 0, limit) for mark in (". ", "! ", "? ", "; "))
    if cut < max(80, limit // 2):
        cut = value.rfind(" ", 0, limit)
    cut = cut if cut > 0 else limit
    return value[: cut + 1].rstrip() + " I've put the rest in Wactorz chat."


# Gap between the sentences of one spoken reply. The wait before it already
# covers the utterance's measured duration, so this is only margin for daemon
# start/stop latency; the 0.55s used between separate utterances reads as a
# stall when it lands between two sentences of the same answer.
_CHUNK_TAIL_PAD = 0.15


def _speech_chunks(text, max_chars=180):
    """Split spoken text at sentence boundaries for quicker TTS startup."""
    words = str(text or "").split()
    if not words:
        return []
    chunks, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        sentence_end = bool(re.search(r"[.!?][\"')\]]*$", word))
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = word
        else:
            current = candidate
        if current and sentence_end and len(current) >= 45:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _barge_check_verdict(session):
    """Take a finished interruption check's answer, or False while it still runs."""
    task = (session or {}).get("barge_check")
    if task is None or not task.done():
        return False
    session["barge_check"] = None
    try:
        return bool(task.result())
    except (asyncio.CancelledError, Exception):
        return False


async def _barge_check_settled(session, timeout=6.0):
    """Wait out a check still running when the reply ends, then take its answer."""
    task = (session or {}).get("barge_check")
    if task is None:
        return False
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    return _barge_check_verdict(session)


async def _speak_reply(agent, text, *, await_playback=False, session=None):
    """Speak a voice-friendly answer in chunks and preserve a barged-in turn."""
    chunks = _speech_chunks(text) if await_playback else [text]
    # Once per reply, not once per sentence: a 'shutup' during any chunk has to
    # survive into the next one, or it only silences the sentence in progress.
    agent.state["stop_speaking"] = False
    spoken, interrupted, stopped = [], False, False
    # One sentence is always being synthesized while another is being spoken, so
    # the network round trip stops landing in the gap between them. Only one
    # runs ahead: any further and a reply cut short would have paid for
    # sentences nobody hears.
    ahead = asyncio.create_task(_prepare_speech(agent, chunks[0], {})) if chunks else None
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        prepared = await ahead if ahead is not None else None
        ahead = None if last else asyncio.create_task(_prepare_speech(agent, chunks[index + 1], {}))
        payload = {
            "text": chunk,
            "await_playback": await_playback,
            "_barge_in_session": session,
            "_continuation": index > 0,
            "_prepared": prepared,
        }
        if not last:
            # Mid-reply the gap is heard as a pause in one answer, so it only has
            # to cover daemon latency. The full pad stays on the final chunk,
            # where it separates the answer from whatever follows it.
            payload["tail_pad"] = _CHUNK_TAIL_PAD
        result = await _say(agent, payload)
        spoken.append(chunk)
        if result.get("interrupted"):
            interrupted = True
            break
        if result.get("stopped"):
            stopped = True
            break
        # A check started during this sentence may have finished while it was
        # still playing. Taking the answer here is what keeps interruption
        # responsive now that checking no longer blocks the sentence.
        if _barge_check_verdict(session):
            interrupted = True
            break
    # A sentence prepared for a reply that stopped early is never spoken; drop
    # it rather than leave its file behind.
    await _discard_prepared(ahead)
    # The last sentence has nowhere to hand a late answer on to, so this one is
    # waited for: without it, talking over the final sentence would be noticed
    # only after the reply had already been treated as finished.
    if not interrupted and not stopped and await _barge_check_settled(session):
        interrupted = True
    return {
        "spoke": bool(spoken),
        "interrupted": interrupted,
        "stopped": stopped,
        "spoken_result": " ".join(spoken).strip(),
    }


_REACHY_INTERFACE_GESTURES = (
    "dance",
    "nod",
    "shake",
    "wiggle",
    "curious",
    "turn_around",
)


async def _bridge_to_main(
    agent,
    text,
    task_id=None,
    *,
    await_playback=False,
    before_speak=None,
    session_id=None,
    voice_friendly=False,
    barge_in_session=None,
    conversation_history=None,
    voice_input=False,
) -> dict[str, Any] | None:
    """Reachy-as-interface bridge.

    Anything Reachy can't turn into a robot/HA command is piped to the MAIN
    orchestrator (send_to('main', _via_interface=True) — full delegation, HA,
    sub-agents) and the answer is spoken back through the robot. Returns a task
    result dict, or None when main is unreachable or answers with nothing, so
    the caller can fall back to the local parse-error hint.
    """
    try:
        interface_name = getattr(agent, "name", "reachy-mini")
        bridge_payload = {
            "text": text,
            "_via_interface": True,
            "_interface_source": interface_name,
            "_interface_context": {
                "display_name": "Reachy",
                "kind": "embodied_robot",
                "capabilities": {
                    "gesture": list(_REACHY_INTERFACE_GESTURES),
                },
                # Goes into main's system prompt verbatim (sanitized there). This
                # robot's name and the ways STT mishears it are ours to state —
                # main has no business knowing them.
                "prompt_note": (
                    "The robot's name is Reachy. Likely transcript spellings Richie, "
                    "Riti, Ritzy, and Lizzy refer to Reachy, never to the user."
                ),
            },
        }
        if voice_input:
            bridge_payload["_interface_voice"] = True
        if conversation_history:
            bridge_payload["_interface_history"] = list(conversation_history)[-4:]
        if session_id:
            bridge_payload["_interface_session_id"] = session_id
        resp = await agent.send_to("main", bridge_payload, timeout=60.0)
    except Exception as e:
        await agent.log(f"bridge to main failed: {e}", level="warning")
        return None

    reply = ""
    interface_actions = []
    if isinstance(resp, dict):
        # A bare error with no answer text means the bridge didn't work.
        if resp.get("error") and not (resp.get("text") or resp.get("result")):
            return None
        reply = str(resp.get("text") or resp.get("result") or resp.get("reply") or "").strip()
        raw_actions = resp.get("interface_actions")
        if isinstance(raw_actions, list):
            for action in raw_actions[:3]:
                if not isinstance(action, dict):
                    continue
                command = str(action.get("cmd") or "").lower().strip()
                name = str(action.get("name") or "").lower().strip()
                if command == "gesture" and name in _REACHY_INTERFACE_GESTURES:
                    interface_actions.append({"cmd": command, "name": name})
    elif isinstance(resp, str):
        reply = resp.strip()
    if not reply and not interface_actions:
        return None

    raw_reply = reply
    action_results = []
    for action in interface_actions:
        result = await _dispatch(agent, action["cmd"], action, return_result=True)
        action_results.append(result or {"ok": False, "cmd": action["cmd"]})
    if action_results and not all(result.get("ok") for result in action_results):
        reply = "I wanted to move, but my motors aren't responding right now."
    elif not reply:
        reply = "Okay."

    reply = _sanitize_reachy_identity_reply(reply, user_text=text)
    # Voice it through the robot when connected; the text is returned either way
    # (and is the only channel when the robot is offline).
    spoke = False
    interrupted = False
    spoken_reply = _voice_friendly_reply(reply, user_text=text) if voice_friendly else reply
    # An actuation acknowledgement — "Done: light.turn_on -> light.tapo_l920." —
    # is the one answer whose raw form is worse than its spoken one in every
    # channel: it names a service and an entity id, and drops the colour or
    # brightness that was actually asked for, so three different requests all
    # read identically. Chat gets the human sentence for those.
    #
    # Deliberately only for those. A long answer's spoken form is truncated and
    # ends "I've put the rest in Wactorz chat" — show that in chat and the
    # sentence points at itself, so everything else keeps its full text.
    display_reply = _natural_actuation_speech(reply, text) or reply
    spoken_result = ""
    speech_error = None
    if before_speak is not None:
        try:
            await before_speak(display_reply)
        except Exception as exc:
            await agent.log(f"reply display failed: {exc}", level="warning")
    ok_link, link_reason = _is_connected(agent)
    if ok_link:
        try:
            # Conversation mode speaks short chunks, which gets the first phrase
            # onto the speaker sooner and gives barge-in a clean cancellation point.
            speech = await _speak_reply(
                agent,
                spoken_reply,
                await_playback=await_playback,
                session=barge_in_session,
            )
            spoke = speech["spoke"]
            interrupted = speech["interrupted"]
            spoken_result = speech["spoken_result"]
        except Exception as e:
            speech_error = str(e)
            await agent.log(f"bridge speak failed: {e}", level="warning")
    else:
        speech_error = link_reason or "reachy is not connected"
    return {
        "ok": True,
        "cmd": "bridge",
        "bridged": True,
        "spoke": spoke,
        "interrupted": interrupted,
        "said": spoken_result if spoke else None,
        "spoken_result": spoken_result,
        "result": display_reply,
        "raw_result": raw_reply,
        "interface_actions": interface_actions,
        "interface_action_results": action_results,
        "speech_error": speech_error,
        "_task_id": task_id,
        "task": task_id,
    }


async def cleanup(agent):
    # Stop a voice session before releasing the SDK/media objects it may use.
    try:
        await _conversation_stop(agent, {"reason": "cleanup"})
    except Exception:
        pass
    mini = agent.state.get("mini")
    if not mini:
        return
    # NOTE: we intentionally do NOT call mini.goto_sleep() — it disables motors
    # and the daemon often drops us, requiring a full restart on next spawn.
    try:
        mini.__exit__(None, None, None)
    except Exception as e:
        await agent.log(f"ReachyMini __exit__ failed: {e}", level="warning")
    # Persist bindings on graceful shutdown
    agent.persist("bindings", agent.state.get("bindings", {}))


async def _reconnect(agent, payload=None):
    """Re-open the robot link on a live agent — the recovery for 'robot was off
    at spawn'.

    setup() connects once; before this existed a robot powered on afterwards was
    unreachable for the agent's whole life and the only fix was a restart. Runs
    the same _open_robot ladder against the CURRENT config, so a
    custom/reachy/config change lands here too.
    """
    payload = payload or {}
    # NOTE: failures RAISE rather than return {"ok": False} — _dispatch stamps
    # ok:true over any returned dict, so a returned failure would publish a
    # successful-looking event for a robot that is still offline.
    if agent.state.get("_reconnecting"):
        raise _CommandStageError(
            "reconnect",
            "already in progress",
            {"connected": False, "result": "Already trying to reconnect — give it a few seconds."},
        )

    ok, _reason = _is_connected(agent)
    if ok and not payload.get("force"):
        where = agent.state.get("robot_host") or "autodetect"
        return {
            "connected": True,
            "reconnected": False,
            "robot_host": where,
            "connection_mode": agent.state.get("connection_mode", "auto"),
            "result": (
                f"Already connected to Reachy ({where}, "
                f"{agent.state.get('connection_mode', 'auto')} mode). "
                'Say "reconnect force" if you want to re-open the link anyway.'
            ),
        }

    # Claim the slot BEFORE the first await: the guard above and this set must
    # not be separated by a suspension point, or two concurrent reconnects both
    # get past the check and race to open a second link.
    agent.state["_reconnecting"] = True
    try:
        # A dropped-link handle still holds a websocket + media objects; release
        # it before opening a new one so we don't leak a connection per reconnect.
        old = agent.state.get("mini")
        if old is not None:
            agent.state["mini"] = None  # refuse robot commands mid-reconnect
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: old.__exit__(None, None, None)
                )
            except Exception as e:
                await agent.log(f"releasing stale handle failed (continuing): {e}", level="warning")

        mini, last_err, tried = await _open_robot(agent)
        agent.state["mini"] = mini
        if mini is None:
            hint = ""
            if not (agent.state.get("robot_host") or "").strip():
                # No pinned host is the usual reason a wireless robot is missed:
                # autodetect leans on mDNS, which is what fails on most LANs.
                # Point at the config topic, not .env: only the topic is picked
                # up by a reconnect (.env is read once, at spawn).
                hint = (
                    " No robot host is pinned — publish "
                    '{"robot_host":"<robot ip>","connection_mode":"network"} to '
                    'custom/reachy/config, then say "reconnect" again.'
                )
            raise _CommandStageError(
                "connect",
                f"{last_err} (tried: {tried})",
                {
                    "connected": False,
                    "reconnected": False,
                    "tried": tried,
                    "result": (
                        f"Still can't reach Reachy (tried: {tried}). "
                        f"Last error: {last_err}. Check the robot is powered on "
                        f'and on the network, then say "reconnect" to try again.' + hint
                    ),
                },
            )
        await _bring_up_robot(agent)
    finally:
        agent.state["_reconnecting"] = False

    where = agent.state.get("robot_host") or "autodetect"
    return {
        "connected": True,
        "reconnected": True,
        "robot_host": where,
        "connection_mode": agent.state.get("connection_mode", "auto"),
        "audio_on_robot": bool(agent.state.get("audio_on_robot")),
        "result": f"Reconnected to Reachy ({where}) — awake and ready.",
    }


def _is_connected(agent):
    """Quick check that the SDK handle is alive. Returns (ok, reason)."""
    mini = agent.state.get("mini")
    if mini is None:
        # Surface WHY setup couldn't connect + how to run without the control app,
        # instead of an opaque "no SDK handle" the user has to hunt logs for.
        err = agent.state.get("last_connect_error")
        base = f"reachy not connected: {err}" if err else "reachy not connected (no SDK handle)"
        return False, (
            base + ' — power the robot on, then say "reconnect" to try again '
            "(no agent restart needed). To run without the Reachy Mini control app, "
            'publish {"connection_mode":"network","robot_host":"<robot ip/hostname>"} '
            'to custom/reachy/config and say "reconnect" — or set '
            "REACHY_CONNECTION_MODE=network and REACHY_ROBOT_HOST=<robot ip/hostname> "
            "in .env, which is only re-read on a restart"
        )
    # The SDK exposes _connected / _is_connected / ws on various versions — be tolerant.
    for attr in ("_connected", "is_connected", "connected"):
        v = getattr(mini, attr, None)
        if callable(v):
            try:
                if not v():
                    return False, (
                        "reachy not connected (daemon link dropped) — say "
                        '"reconnect" to re-open the link'
                    )
            except Exception:
                pass
        elif v is False:
            return False, (
                'reachy not connected (daemon link dropped) — say "reconnect" to re-open the link'
            )
    return True, None


# ============================================================
# Dispatcher — the single place every command flows through.
# ============================================================


class _CommandStageError(RuntimeError):
    """A command failed at a named stage and has useful partial event fields."""

    def __init__(self, stage, message, fields=None):
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.fields = dict(fields or {})


async def _dispatch(agent, cmd, payload, return_result=False):
    if not cmd:
        if return_result:
            return {"ok": False, "cmd": None, "error": "missing cmd field"}
        return None
    cmd = str(cmd).lower().strip()
    agent.state["last_cmd"] = cmd
    started = time.time()

    try:
        if cmd == "wake":
            result = await _wake(agent)
        elif cmd == "sleep":
            result = await _sleep(agent)
        elif cmd == "reconnect":
            result = await _reconnect(agent, payload)
        elif cmd == "pose":
            result = await _pose(agent, payload)
        elif cmd == "turn":
            result = await _turn(agent, payload)
        elif cmd == "antennas":
            result = await _antennas(agent, payload)
        elif cmd == "gesture":
            result = await _gesture(agent, payload)
        elif cmd == "look_at":
            result = await _look_at(agent, payload)
        elif cmd == "look_pixel":
            result = await _look_pixel(agent, payload)
        elif cmd == "camera":
            result = await _camera(agent, payload)
        elif cmd == "describe":
            result = await _describe(agent, payload)
        elif cmd == "look_behind":
            result = await _look_behind(agent, payload)
        elif cmd == "look_around":
            result = await _look_around(agent, payload)
        elif cmd == "listen":
            result = await _listen(agent, payload)
        elif cmd == "ask_voice":
            result = await _ask_voice(agent, payload)
        elif cmd == "conversation_start":
            result = await _conversation_start(agent, payload)
        elif cmd == "conversation_stop":
            result = await _conversation_stop(agent, payload)
        elif cmd == "doa":
            result = await _doa(agent, payload)
        elif cmd == "emotion":
            result = await _emotion(agent, payload)
        elif cmd == "motors":
            result = await _motors(agent, payload)
        elif cmd == "diag":
            result = await _diag(agent, payload)
        elif cmd == "set_pose":
            result = await _set_pose(agent, payload)
        elif cmd == "bind":
            result = await _bind(agent, payload)
        elif cmd == "unbind":
            result = await _unbind(agent, payload)
        elif cmd == "list_emotions":
            result = {"emotions": agent.state.get("emotion_names", [])}
        elif cmd == "stop":
            result = await _stop(agent)
        elif cmd == "shutup":
            result = await _shutup(agent, payload)
        elif cmd == "health":
            result = await _health(agent, payload)
        elif cmd == "say":
            result = await _say(agent, payload)
        elif cmd == "volume":
            result = await _volume(agent, payload)
        elif cmd == "debug":
            result = await _debug(agent, payload)
        elif cmd == "face_forward":
            result = await _face_forward(agent, payload)
        elif cmd == "ha":
            result = await _ha(agent, payload)
        else:
            raise ValueError(f"unknown cmd: {cmd}")

        ack = {"ok": True, "cmd": cmd, "duration_s": round(time.time() - started, 3)}
        if isinstance(result, dict):
            ack.update(result)
        # Per-command result — correlation id if provided
        rid = payload.get("id")
        if rid:
            await agent.publish(f"custom/reachy/cmd_result/{rid}", ack)
        # Publish the same command result fields on the shared event topic.  The
        # envelope is applied last so a handler cannot replace type/ok/ts.
        event = dict(ack)
        event.update({"type": cmd, "ok": True, "ts": time.time()})
        await agent.publish("custom/reachy/events", event)
        if return_result:
            return ack
    except Exception as e:
        err = dict(getattr(e, "fields", {}) or {})
        if isinstance(e, _CommandStageError):
            err["stage"] = e.stage
        err.update(
            {
                "ok": False,
                "cmd": cmd,
                "error": str(e),
                "duration_s": round(time.time() - started, 3),
            }
        )
        rid = payload.get("id")
        if rid:
            await agent.publish(f"custom/reachy/cmd_result/{rid}", err)
        event = dict(err)
        event.update({"type": cmd, "ok": False, "ts": time.time()})
        await agent.publish("custom/reachy/events", event)
        await agent.log(f"cmd '{cmd}' failed: {e}", level="error")
        if return_result:
            return err


# ============================================================
# Command implementations
# ============================================================


async def _wake(agent):
    mini = agent.state["mini"]
    # Torque must be ON or wake_up plays its sound but the head/antennas don't
    # move. Re-enable every wake in case motors were disabled meanwhile.
    await _ensure_motors_enabled(agent)
    async with agent.state["motion_lock"]:
        agent.state["busy"] = True
        try:
            await _do(mini.wake_up)
            agent.state["awake"] = True
        finally:
            agent.state["busy"] = False
    return {}


async def _ensure_motors_enabled(agent):
    """Turn motor torque ON (best-effort).

    Without the Reachy Mini control app the daemon can come up with motors
    DISABLED (compliant/safe), so goto_target quietly moves nothing while
    play_sound still works — the robot 'talks but won't move'. Older SDKs
    without enable_motors just skip this.
    """
    mini = agent.state.get("mini")
    if mini is None:
        return False
    fn = getattr(mini, "enable_motors", None)
    if not callable(fn):
        return False
    try:
        await _do(fn)
        agent.state["motors_enabled"] = True
        return True
    except Exception as e:
        await agent.log(f"enable_motors failed: {e}", level="warning")
        return False


async def _motors(agent, payload: dict[str, Any]) -> dict[str, Any]:
    """Enable or disable motor torque — the toggle the control app gave you.

    {"cmd":"motors","on":true}    -> torque ON  (needed to move)
    {"cmd":"motors","on":false}   -> torque OFF (compliant; hand-pose the robot)
    """
    mini = agent.state["mini"]
    on = payload.get("on")
    if on is None:
        on = not (payload.get("off") or payload.get("disable"))
    if isinstance(on, str):
        on = on.strip().lower() not in ("off", "false", "no", "0", "disable")
    on = bool(on)
    fn = getattr(mini, "enable_motors" if on else "disable_motors", None)
    if not callable(fn):
        raise RuntimeError("SDK has no motor enable/disable")
    await _do(fn)
    agent.state["motors_enabled"] = on
    return {
        "motors_enabled": on,
        "result": f"Motors {'enabled — the robot can move now' if on else 'disabled (compliant)'}.",
    }


async def _diag(agent, payload):
    """Diagnose 'the robot won't move'.

    Motion commands are fire-and-forget over the websocket, so goto_target
    returning cleanly means only that the daemon RECEIVED the command — not that
    a motor turned. This reports what the daemon actually is (version vs the SDK
    — a mismatch makes the daemon silently ignore motion commands) and runs a
    real motion self-test: enable torque, read joint positions, command a small
    move, read them again, and report whether they actually changed.
    """
    import reachy_mini as _rm  # pyright: ignore[reportMissingImports]

    mini = agent.state["mini"]
    info = {
        "sdk_version": getattr(_rm, "__version__", "?"),
        "connection_mode": agent.state.get("connection_mode"),
        "robot_host": agent.state.get("robot_host") or "(autodetect)",
        "motors_enabled": bool(agent.state.get("motors_enabled")),
    }

    # ---- Daemon status: the version is the key signal ----
    try:
        st = await _do(mini.client.get_status)
        info["daemon_version"] = getattr(st, "version", None)
        info["daemon_wlan_ip"] = getattr(st, "wlan_ip", None)
        info["daemon_no_media"] = getattr(st, "no_media", None)
    except Exception as e:
        info["daemon_status_error"] = str(e)
    sdk_v = str(info.get("sdk_version") or "").strip()
    dae_v = str(info.get("daemon_version") or "").strip()
    info["version_mismatch"] = bool(sdk_v and dae_v and sdk_v != dae_v)

    # ---- Motion self-test: does a commanded move change real joint angles? ----
    def _positions(raw):
        # get_current_joint_positions returns (positions, velocities) on this SDK.
        seq = raw[0] if isinstance(raw, tuple) else raw
        return [float(x) for x in seq]

    try:
        await _ensure_motors_enabled(agent)
        create_head_pose = agent.state["create_head_pose"]
        before = _positions(await _do(mini.get_current_joint_positions))
        await _do(mini.goto_target, head=create_head_pose(yaw=15, degrees=True), duration=0.6)
        await asyncio.sleep(0.9)
        after = _positions(await _do(mini.get_current_joint_positions))
        delta = max((abs(a - b) for a, b in zip(after, before, strict=False)), default=0.0)
        info["joint_delta_max_rad"] = round(delta, 4)
        info["moved"] = delta > 0.01
        # Re-centre.
        await _do(mini.goto_target, head=create_head_pose(yaw=0, degrees=True), duration=0.6)
    except Exception as e:
        info["motion_test_error"] = str(e)

    # ---- Human-readable verdict ----
    if info.get("version_mismatch"):
        info["result"] = (
            f"SDK {sdk_v} does not match the robot daemon {dae_v}. A version mismatch "
            f"makes the daemon silently ignore motion commands (audio still works). Fix: "
            f"pip install reachy_mini=={dae_v} (match the robot), then restart the agent."
        )
    elif info.get("motion_test_error"):
        info["result"] = f"Motion self-test errored: {info['motion_test_error']}"
    elif info.get("moved") is False:
        info["result"] = (
            "Motors enabled and a move was commanded, but joint angles did not change - "
            "the daemon is not driving the motors (torque not applied, or a hardware/"
            "calibration issue on the robot)."
        )
    elif info.get("moved"):
        info["result"] = (
            f"Robot moved (Δ={info['joint_delta_max_rad']} rad) - the motion path works. "
            "If a plan still looks static, the moves may just be small/fast."
        )
    else:
        info["result"] = "Diagnostics collected (see fields)."
    return info


async def _sleep(agent):
    """Animated 'sleep' — head droops, antennas fall. We DO NOT call mini.goto_sleep()
    because that disables motors and often loses the daemon connection, requiring a
    full agent restart. This is purely cosmetic.
    """
    mini = agent.state["mini"]
    np = agent.state["np"]
    create_head_pose = agent.state["create_head_pose"]
    async with agent.state["motion_lock"]:
        agent.state["busy"] = True
        try:
            # Head droops down slowly
            await _do(
                mini.goto_target,
                head=create_head_pose(pitch=25, degrees=True),
                antennas=np.deg2rad([-30, -30]),
                duration=1.5,
            )
            agent.state["awake"] = False
        finally:
            agent.state["busy"] = False
    return {"animated": True}


async def _pose(agent, payload):
    """Interpolated head pose (and optional antennas + body_yaw)."""
    mini = agent.state["mini"]
    np = agent.state["np"]
    create_head_pose = agent.state["create_head_pose"]

    head_pose = create_head_pose(
        x=float(payload.get("x", 0)),
        y=float(payload.get("y", 0)),
        z=float(payload.get("z", 0)),
        roll=float(payload.get("roll", 0)),
        pitch=float(payload.get("pitch", 0)),
        yaw=float(payload.get("yaw", 0)),
        mm=bool(payload.get("mm", True)),
        degrees=bool(payload.get("degrees", True)),
    )
    kw = {"head": head_pose, "duration": float(payload.get("duration", 1.0))}
    if "antennas" in payload:
        a = payload["antennas"]
        # Accept degrees ([45,-45]) or radians ([0.78,-0.78])
        if payload.get("antennas_degrees", True):
            kw["antennas"] = np.deg2rad(a)
        else:
            kw["antennas"] = np.array(a, dtype=float)
    if "body_yaw" in payload:
        by = float(payload["body_yaw"])
        by_degrees = by if payload.get("body_yaw_degrees", True) else float(np.rad2deg(by))
        kw["body_yaw"] = float(np.deg2rad(by_degrees))
    else:
        # Reachy Mini's goto_target defaults body_yaw to 0.0. Explicit None is
        # required to preserve a persistent rear-facing orientation.
        by_degrees = None
        kw["body_yaw"] = None
    method = payload.get("method")
    if method:
        kw["method"] = str(method)

    async with agent.state["motion_lock"]:
        agent.state["busy"] = True
        try:
            await _do(mini.goto_target, **kw)
            if by_degrees is not None:
                agent.state["_facing_body_yaw_deg"] = by_degrees
        finally:
            agent.state["busy"] = False
    return {}


async def _antennas(agent, payload):
    """Antennas-only motion. Accepts left/right (degrees by default) or angles list."""
    mini = agent.state["mini"]
    np = agent.state["np"]
    if "angles" in payload:
        angles = payload["angles"]
    else:
        # left/right convention — confirm against your robot orientation
        left = float(payload.get("left", 0))
        right = float(payload.get("right", 0))
        # SDK expects [right, left] per docs; we keep it consistent with payload semantics:
        angles = [right, left]
    if payload.get("degrees", True):
        angles = np.deg2rad(angles)
    duration = float(payload.get("duration", 0.3))
    method = payload.get("method")
    kw = {"antennas": angles, "duration": duration}
    if method:
        kw["method"] = str(method)
    async with agent.state["motion_lock"]:
        agent.state["busy"] = True
        try:
            await _do(mini.goto_target, **kw)
        finally:
            agent.state["busy"] = False
    return {}


async def _look_at(agent, payload):
    """Look at a 3D point in world coordinates (meters)."""
    mini = agent.state["mini"]
    fn = getattr(mini, "look_at_world", None) or getattr(mini, "look_at", None)
    if not fn:
        raise RuntimeError("SDK has no look_at_world / look_at method")
    x = float(payload.get("x", 0.5))
    y = float(payload.get("y", 0.0))
    z = float(payload.get("z", 0.2))
    duration = float(payload.get("duration", 1.0))
    async with agent.state["motion_lock"]:
        agent.state["busy"] = True
        try:
            await _do(fn, x, y, z, duration=duration)
        finally:
            agent.state["busy"] = False
    return {"target": {"x": x, "y": y, "z": z}}


async def _look_pixel(agent, payload) -> dict[str, dict[str, int]]:
    """Look at a pixel coordinate in the onboard camera image. Useful for chaining vision agents."""
    mini = agent.state["mini"]
    fn = getattr(mini, "look_at_image", None)
    if not fn:
        raise RuntimeError("SDK has no look_at_image method")
    u = int(payload.get("u", 0))
    v = int(payload.get("v", 0))
    duration = float(payload.get("duration", 0.5))
    async with agent.state["motion_lock"]:
        agent.state["busy"] = True
        try:
            await _do(fn, u, v, duration=duration)
        finally:
            agent.state["busy"] = False
    return {"pixel": {"u": u, "v": v}}


async def _current_body_yaw_deg(agent) -> float:
    """Read body yaw from the robot, falling back to the last commanded value."""
    fallback = float(agent.state.get("_facing_body_yaw_deg", 0.0))
    mini = agent.state.get("mini")
    getter = getattr(mini, "get_current_joint_positions", None) if mini else None
    if not callable(getter):
        return fallback
    try:
        positions = await _do(getter)
        head = positions[0] if isinstance(positions, (tuple, list)) and positions else None
        if head is not None and len(head):
            value = float(agent.state["np"].rad2deg(float(head[0])))
            agent.state["_facing_body_yaw_deg"] = value
            return value
    except Exception as e:
        await agent.log(f"could not read body yaw ({e}); using last target", level="warning")
    return fallback


async def _face_body(agent, target_degrees, payload=None):
    """Face an absolute body direction and leave Reachy there.

    Reachy's automatic IK is intentionally free to redistribute a world-space
    head yaw onto the body. A 155-degree gaze can therefore stop the body near
    90 degrees (155 - the 65-degree neck allowance). For an explicit embodied
    turn, interpolate the body joint directly while preserving every other
    joint. That makes the physical base reach the requested rear-facing limit.
    """
    payload = payload or {}
    target = max(-155.0, min(155.0, float(target_degrees)))
    duration = max(0.5, min(3.0, float(payload.get("duration", 1.2))))
    mini = agent.state["mini"]
    np = agent.state["np"]
    await _ensure_motors_enabled(agent)
    async with agent.state["motion_lock"]:
        agent.state["busy"] = True
        try:
            goto_joints = getattr(mini, "goto_joint_positions", None)
            getter = getattr(mini, "get_current_joint_positions", None)
            if callable(goto_joints) and callable(getter):
                positions = await _do(getter)
                head = positions[0] if isinstance(positions, (tuple, list)) else None
                joints = (
                    np.asarray(head, dtype=float).reshape(-1)
                    if head is not None
                    else np.asarray([])
                )
            else:
                joints = np.asarray([])
            if len(joints) >= 7:
                joints[0] = float(np.deg2rad(target))
                await _do(
                    goto_joints,
                    head_joint_positions=joints.tolist(),
                    duration=duration,
                )
            else:
                # Compatibility fallback for older SDKs without joint-space
                # interpolation. These may still redistribute some yaw.
                create_head_pose = agent.state["create_head_pose"]
                await _do(
                    mini.goto_target,
                    head=create_head_pose(yaw=target, degrees=True),
                    body_yaw=float(np.deg2rad(target)),
                    duration=duration,
                )
            agent.state["_facing_body_yaw_deg"] = target
        finally:
            agent.state["busy"] = False
    return {"body_yaw": target, "facing": "rear" if abs(target) >= 80 else "forward"}


async def _turn(agent, payload=None):
    """Turn the body relative to its current heading; left is positive."""
    payload = payload or {}
    delta = max(-155.0, min(155.0, float(payload.get("angle", 45.0))))
    current = await _current_body_yaw_deg(agent)
    target = max(-155.0, min(155.0, current + delta))
    actual = target - current
    direction = "left" if delta >= 0 else "right"
    if abs(actual) < 0.5:
        return {
            "body_yaw": current,
            "turn_degrees": 0.0,
            "result": f"I can't turn any farther {direction}.",
        }
    result = await _face_body(agent, target, payload)
    degrees = round(abs(actual), 1)
    shown = int(degrees) if degrees.is_integer() else degrees
    result.update(
        {
            "turn_degrees": actual,
            "result": f"I turned {direction} {shown} degrees.",
        }
    )
    return result


async def _turn_around(agent, payload=None):
    """Toggle between forward and a persistent, mechanically safe rear view."""
    current = await _current_body_yaw_deg(agent)
    if abs(current) >= 80:  # noqa: SIM108
        target = 0.0
    else:
        target = -155.0 if current < 0 else 155.0
    result = await _face_body(agent, target, payload)
    result.update({"gesture": "turn_around", "result": "I turned around."})
    return result


async def _face_forward(agent, payload=None):
    result = await _face_body(agent, 0.0, payload)
    result["result"] = "I'm facing you again."
    return result


async def _debug(agent, payload: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(payload.get("enabled", payload.get("on", True)))
    agent.state["debug"] = enabled
    return {
        "debug": enabled,
        "result": "Debug details enabled." if enabled else "Debug details disabled.",
    }


async def _gesture(agent, payload: dict[str, Any]) -> dict[str, Any]:
    """Play a small safe physical gesture with every pose field explicit."""
    name = str(payload.get("name") or payload.get("gesture") or "").lower().strip()
    duration = max(0.12, min(0.8, float(payload.get("duration", 0.3))))
    if name == "turn_around":
        return await _turn_around(agent, payload)
    gestures = {
        "dance": [
            (-10, -3, -8, 28, 52, -12),
            (10, -3, 8, 52, 28, 12),
            (-8, -5, -5, 35, 58, -8),
            (8, -5, 5, 58, 35, 8),
            (0, -6, 0, 45, 45, 0),
            (0, 0, 0, 0, 0, 0),
        ],
        "nod": [
            (0, -10, 0, 18, 18, 0),
            (0, 12, 0, 5, 5, 0),
            (0, -8, 0, 18, 18, 0),
            (0, 0, 0, 0, 0, 0),
        ],
        "shake": [
            (-14, 0, 0, 12, 5, -5),
            (14, 0, 0, 5, 12, 5),
            (-10, 0, 0, 12, 5, -4),
            (10, 0, 0, 5, 12, 4),
            (0, 0, 0, 0, 0, 0),
        ],
        "wiggle": [
            (0, 0, 0, 55, 20, 0),
            (0, 0, 0, 20, 55, 0),
            (0, 0, 0, 55, 20, 0),
            (0, 0, 0, 0, 0, 0),
        ],
        "curious": [
            (8, -5, 12, 38, 55, 4),
            (0, 0, 0, 12, 12, 0),
        ],
    }
    steps = gestures.get(name)
    if not steps:
        raise ValueError("gesture name must be dance, nod, shake, wiggle, curious, or turn_around")
    mini = agent.state["mini"]
    np = agent.state["np"]
    create_head_pose = agent.state["create_head_pose"]
    await _ensure_motors_enabled(agent)
    async with agent.state["motion_lock"]:
        agent.state["busy"] = True
        try:
            for yaw, pitch, roll, left, right, body_yaw in steps:
                await _do(
                    mini.goto_target,
                    head=create_head_pose(yaw=yaw, pitch=pitch, roll=roll, degrees=True),
                    antennas=np.deg2rad([right, left]),
                    body_yaw=float(np.deg2rad(body_yaw)),
                    duration=duration,
                )
                await asyncio.sleep(duration + 0.04)
                agent.state["_facing_body_yaw_deg"] = float(body_yaw)
        finally:
            agent.state["busy"] = False
    labels = {
        "dance": "Ta-da! I did a little dance.",
        "nod": "I nodded.",
        "shake": "I shook my head.",
        "wiggle": "I wiggled my antennas.",
        "curious": "Curious mode.",
    }
    return {"gesture": name, "result": labels[name]}


async def _emotion(agent, payload):
    """Play a recorded emotion clip from the HF dataset (e.g. 'happy', 'curious1', 'success1')."""
    moves = agent.state.get("moves")
    if not moves:
        raise RuntimeError("emotion library not loaded")
    name = payload.get("name") or payload.get("emotion")
    if not name:
        raise ValueError("emotion requires {'name': '<clip-name>'}")
    mini = agent.state["mini"]
    initial_goto = float(payload.get("initial_goto_duration", 1.0))
    clip = moves.get(name)
    async with agent.state["motion_lock"]:
        agent.state["busy"] = True
        try:
            await _do(mini.play_move, clip, initial_goto_duration=initial_goto)
        finally:
            agent.state["busy"] = False
    return {"played": name}


async def _set_pose(agent, payload):
    """Non-interpolated raw target — for high-frequency streaming. No lock (caller's responsibility)."""
    mini = agent.state["mini"]
    np = agent.state["np"]
    create_head_pose = agent.state["create_head_pose"]
    kw = {}
    if any(k in payload for k in ("x", "y", "z", "roll", "pitch", "yaw")):
        kw["head"] = create_head_pose(
            x=float(payload.get("x", 0)),
            y=float(payload.get("y", 0)),
            z=float(payload.get("z", 0)),
            roll=float(payload.get("roll", 0)),
            pitch=float(payload.get("pitch", 0)),
            yaw=float(payload.get("yaw", 0)),
            mm=bool(payload.get("mm", True)),
            degrees=bool(payload.get("degrees", True)),
        )
    if "antennas" in payload:
        a = payload["antennas"]
        kw["antennas"] = (
            np.deg2rad(a) if payload.get("antennas_degrees", True) else np.array(a, dtype=float)
        )
    if "body_yaw" in payload:
        by = float(payload["body_yaw"])
        kw["body_yaw"] = float(np.deg2rad(by)) if payload.get("body_yaw_degrees", True) else by
    await _do(mini.set_target, **kw)
    return {}


async def _stop_audio(agent):
    """Halt any in-progress speech immediately (best-effort across backends).

    Sets stop_speaking so an interruptible say-wait bails, then tells the audio
    backend to stop the current playback (GStreamer local OR the WebRTC/robot
    speaker). Runs on the concurrent per-verb MQTT listener, so it can cut audio
    even while a say is otherwise holding the (serial) actor mailbox.
    """
    agent.state["stop_speaking"] = True
    mini = agent.state.get("mini")
    if mini is None:
        return False
    media = getattr(mini, "media", None) or getattr(mini, "media_manager", None)
    audio = getattr(media, "audio", None) if media else None
    # For the WebRTC backend, go straight to the daemon endpoint that owns the
    # robot speaker. This avoids depending on SDK wrapper versions and gives a
    # spoken interruption the shortest possible path to silence.
    stopped = await _stop_daemon_sound(agent)
    candidates = (
        ()
        if stopped
        else (
            (media, "stop_playing"),
            (audio, "stop_playing"),
            (media, "stop_sound"),
            (audio, "stop_sound"),
        )
    )
    for obj, meth in candidates:
        fn = getattr(obj, meth, None) if obj is not None else None
        if callable(fn):
            try:
                await _do(fn)
                stopped = True
                break
            except Exception as e:
                await agent.log(f"{meth} failed: {e}", level="warning")
    agent.state["_speaking"] = False
    return stopped


async def _stop_daemon_sound(agent):
    """Stop robot-speaker file playback through the daemon's canonical API."""
    url = _daemon_url(agent)
    if not url:
        return False
    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=2.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{url.rstrip('/')}/api/media/stop_sound") as response:
                if response.status < 400:
                    return True
                await agent.log(
                    f"robot speaker stop returned HTTP {response.status}",
                    level="warning",
                )
    except Exception as e:
        await agent.log(f"robot speaker stop failed: {e}", level="warning")
    return False


async def _shutup(agent, payload=None):
    """Stop talking right now — cut the current utterance. Plain English 'shut
    up' / 'stop talking' / 'be quiet' route here (vs. 'mute' which is volume).
    """
    stopped = await _stop_audio(agent)
    return {
        "stopped_speaking": stopped,
        "result": "Stopped talking." if stopped else "Nothing was playing.",
    }


async def _stop(agent):
    """Best-effort motion abort — not all SDK versions have a stop primitive, so we re-target current pose."""
    await _stop_audio(agent)  # a full 'stop' also cuts any speech
    mini = agent.state["mini"]
    # If the SDK exposes a stop, use it.
    fn = getattr(mini, "stop", None) or getattr(mini, "cancel", None)
    if fn:
        await _do(fn)
        return {"stopped": True}
    # Fallback: short re-target to current pose with very short duration.
    create_head_pose = agent.state["create_head_pose"]
    await _do(mini.goto_target, head=create_head_pose(), body_yaw=None, duration=0.1)
    return {"stopped": True, "fallback": True}


def _voice_for_text(text, default_voice):
    """Pick a TTS voice whose language matches the script of `text`.

    An English neural voice given text in a non-Latin script (e.g. Greek)
    reads out the Unicode letter NAMES instead of pronouncing the word. We
    only auto-switch for scripts with an unambiguous language mapping, and we
    never override a default that's already in the target language.

    Returns default_voice when the script is Latin/ambiguous or already matches.
    """
    # Count alphabetic chars per detectable script.
    el = latin = 0
    for ch in text:
        if not ch.isalpha():
            continue
        cp = ord(ch)
        if 0x0370 <= cp <= 0x03FF or 0x1F00 <= cp <= 0x1FFF:  # Greek + Greek Extended
            el += 1
        elif 0x0041 <= cp <= 0x024F:  # Latin + Latin Extended-A/B
            latin += 1
    # Greek dominates and the default isn't already a Greek voice -> use one.
    if el > 0 and el >= latin and not default_voice.lower().startswith("el-"):
        return "el-GR-AthinaNeural"
    return default_voice


def _say_playback_pad(speech_seconds, payload):
    """Seconds to wait after starting playback so back-to-back says don't stomp.

    play_sound is fire-and-forget, so without a wait a second utterance cuts the
    first off mid-word (and any volume change between them lands on the wrong
    one). Returns 0 when the caller opts out with await_playback:false, or when
    the duration is unknown. tail_pad covers trailing silence / daemon latency.
    """
    if not payload.get("await_playback", True):
        return 0.0
    if not speech_seconds or speech_seconds <= 0:
        return 0.0
    return float(speech_seconds) + float(payload.get("tail_pad", 0.55))


def _speech_duration_seconds(text, speech_ticks):
    """Return measured TTS duration, with a conservative metadata-free fallback."""
    measured = max(0.0, float(speech_ticks or 0) / 1e7)
    if measured > 0:
        return measured
    value = str(text or "").strip()
    if not value:
        return 0.0
    words = len(value.split())
    return max(0.6, words / 2.6, len(value) / 15.0)


def _edge_tts_communicate(edge_tts, text, voice):
    """Request word timing when supported, retaining older edge-tts support."""
    try:
        return edge_tts.Communicate(text, voice, boundary="WordBoundary")
    except TypeError:
        return edge_tts.Communicate(text, voice)


async def _wait_for_barge_guard(cancel, seconds):
    """Ignore the robot speaker's startup transient without delaying playback."""
    deadline = asyncio.get_running_loop().time() + max(0.0, float(seconds))
    while not cancel.is_set():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(0.05, remaining))
    return False


async def _begin_barge_in_monitor(agent, session, speech_seconds):
    """Listen during playback and signal as soon as sustained user speech starts."""
    if not session or not session.get("payload", {}).get("barge_in", False):
        return None
    if session.get("cancel_event") and session["cancel_event"].is_set():
        return None

    from wactorz.catalogue_agents.reachy_vad import VADConfig, capture_utterance

    media = _require_mic(agent, record=True)
    onset = threading.Event()
    cancel = threading.Event()
    payload = session.get("payload", {})
    guard_s = max(0.0, min(2.0, float(payload.get("barge_guard_s", 0.45))))
    config = VADConfig(
        speech_start_timeout_s=max(0.5, float(speech_seconds or 0) + 1.0),
        speech_start_s=max(0.06, min(1.0, float(payload.get("barge_onset_s", 0.21)))),
        silence_s=max(0.3, min(1.5, float(payload.get("barge_silence_s", 0.65)))),
        max_utterance_s=max(1.0, min(30.0, float(payload.get("max_utterance_s", 12.0)))),
        min_speech_s=max(0.09, min(1.0, float(payload.get("barge_min_speech_s", 0.12)))),
        pre_roll_s=max(0.0, min(0.5, float(payload.get("pre_roll_s", 0.3)))),
        flush_s=max(0.0, min(0.5, float(payload.get("barge_flush_s", 0.0)))),
        mode=max(0, min(3, int(payload.get("barge_vad_mode", 1)))),
        min_rms=max(0.0, min(1.0, float(payload.get("barge_min_rms", 0.006)))),
    )

    async def _capture_after_guard():
        await _wait_for_barge_guard(cancel, guard_s)
        return await _do(capture_utterance, media, cancel, config, onset.set)

    task = asyncio.create_task(_capture_after_guard())
    session["barge_task"] = task
    session["barge_cancel_event"] = cancel
    return task, onset, cancel


def _barge_in_sounds_like_reachy(transcript, spoken_text):
    """Whether a transcript is Reachy's own sentence arriving back through the mic.

    The one thing that separates his voice from a person's here is *what was
    said*: he knows the words he is playing, so a transcript made of them is
    echo however loud or well-formed it looks. Energy and duration cannot make
    that call — his voice is real speech by every acoustic measure.
    """
    heard = " ".join(str(transcript or "").lower().split())
    said = " ".join(str(spoken_text or "").lower().split())
    if not heard or not said:
        return False
    if heard in said:
        return True
    # The longest shared run, not a whole-string ratio: the microphone catches
    # part of a sentence, so what matters is whether that part is his — not
    # whether the two strings came out the same length.
    match = difflib.SequenceMatcher(None, heard, said).find_longest_match(
        0, len(heard), 0, len(said)
    )
    return match.size >= max(12, int(len(heard) * 0.6))


async def _verified_barge_in(agent, session, capture, spoken_text):
    """Whether audio captured during playback is a person, not the loudspeaker.

    Onset alone used to stop the reply, which is why Reachy cut himself off
    mid-word: his own voice clears any onset test, because it is speech. The
    capture is therefore checked before it is believed — enough voice to be a
    turn, a transcript the recogniser stands behind, and words that are not the
    ones he was just saying. Only then does it become the next turn.
    """
    payload = session.get("payload", {})
    # Defaults to the same floor the capture layer used to call this an
    # utterance at all. A separate, stricter number here second-guessed that
    # decision and threw away real interruptions: "stop" is about 0.2s of voice,
    # and echo suppression during playback trims what little there is. The floor
    # exists only to skip the recogniser on a click — telling his voice from a
    # person's is the transcript's job, and it is the only thing that can.
    min_voiced = max(
        0.0,
        min(
            3.0,
            float(
                payload.get("barge_verify_min_speech_s", payload.get("barge_min_speech_s", 0.12))
            ),
        ),
    )
    voiced = float(getattr(capture, "voiced_duration_s", 0.0) or 0.0)
    if voiced < min_voiced:
        await agent.log(
            f"ignored a suspected interruption: {voiced:.2f}s of voice is under {min_voiced:.2f}s",
            level="info",
        )
        return False

    stt_payload = dict(_conversation_stt_payload(payload))
    # Our own VAD already found the voice in this clip and measured it, so the
    # recogniser's VAD has nothing left to protect and a great deal to lose: run
    # on a recording made while the loudspeaker was playing, it removed all
    # 4.3 seconds of someone saying "stop talking" and returned an empty
    # transcript — which read as "no interruption" and let Reachy talk over the
    # person asking him to stop.
    stt_payload["stt_vad_filter"] = False
    try:
        wav_b64, _frames = await _do(
            _pcm_to_wav_b64, capture.audio, capture.samplerate, capture.channels
        )
        transcription, _retried = await _conversation_transcribe(
            agent, base64.b64decode(wav_b64), stt_payload, session
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Unverifiable is not the same as real. Keep speaking rather than let a
        # recogniser failure become a way to silence him.
        await agent.log(f"could not check a suspected interruption: {exc}", level="warning")
        return False
    transcript = _normalize_reachy_transcript(str(getattr(transcription, "text", "") or "").strip())
    if not _conversation_transcript_is_meaningful(transcript):
        return False
    credible, reason = _conversation_transcription_is_credible(transcription, stt_payload)
    if not credible:
        await agent.log(f"ignored a suspected interruption: {reason}", level="info")
        return False
    if _barge_in_sounds_like_reachy(transcript, spoken_text):
        await agent.log(
            f"ignored a suspected interruption: those were my own words ({transcript!r})",
            level="info",
        )
        return False
    session["pending_capture"] = capture
    # Paired with the clip it came from, so the turn that consumes the clip can
    # reuse this instead of transcribing it a second time — which would re-run
    # the recogniser's VAD over it and lose the words all over again.
    session["pending_transcript"] = (capture, transcription)
    return True


async def _check_barge_in_later(agent, session, monitor, spoken_text):
    """Judge a suspected interruption without holding the next sentence up.

    Waiting for the recording to close and then transcribing it is seconds of
    work, and it would otherwise land between every pair of sentences, because
    Reachy's own voice trips the onset almost every time. Speaking continues
    while this runs; the cost of being wrong is that he finishes one more
    sentence than he strictly had to.
    """
    try:
        capture = await _finish_barge_in_monitor(session, monitor, True)
        if capture is None:
            return False
        if not await _verified_barge_in(agent, session, capture, spoken_text):
            return False
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await agent.log(f"could not check a suspected interruption: {exc}", level="warning")
        return False
    try:
        await _conversation_publish(agent, session, "listening", {"interrupted": True})
    except Exception:
        pass
    return True


async def _finish_barge_in_monitor(session, monitor, onset_seen):
    """Stop a playback monitor and return any completed capture for checking.

    Deliberately no longer decides anything: a clip recorded while Reachy was
    speaking may be Reachy, so whether it becomes the next turn is settled by
    `_verified_barge_in` after the sentence has finished.
    """
    if monitor is None:
        return None
    task, _onset, cancel = monitor
    if not onset_seen:
        cancel.set()
    try:
        capture = await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel.set()
        await asyncio.gather(task, return_exceptions=True)
        raise
    except Exception:
        return None
    finally:
        if session.get("barge_task") is task:
            session["barge_task"] = None
            session["barge_cancel_event"] = None
    if onset_seen and getattr(capture, "audio", None) is not None and capture.audio.size:
        return capture
    return None


async def _prepare_speech(agent, text: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Turn one sentence into a file ready for play_sound.

    Split out of `_say` so a reply's next sentence can be synthesized while the
    current one is still playing. Every sentence is its own edge-tts request —
    a network round trip, and on the robot the loudness boost on top of it — so
    doing this between sentences put the whole of it into every gap. That is the
    pause heard as "Sure! ... I'm Reachy ... I love a good chat".
    """
    # Voice precedence: explicit payload voice > script auto-detect > configured default.
    # Auto-detect matters because an English voice fed literal Greek text spells out
    # the Unicode letter names ("kappa alpha lambda...") instead of pronouncing the
    # word. Picking a voice that matches the text's script fixes that.
    # Every term joined with `or` so an empty one falls through. A default
    # argument would not: it applies only when the name is absent, and this one
    # is documented as "leave empty for the default".
    default_voice = (
        agent.state.get("tts_voice")
        or os.environ.get("TTS_VOICE", "").strip()
        or "en-US-JennyNeural"
    )
    voice = payload.get("voice") or _voice_for_text(text, default_voice)

    # -- Synthesize to a temp MP3 file (path-based, GStreamer playbin decodes it) --
    try:
        import edge_tts
    except ImportError as error:
        raise RuntimeError("edge-tts not installed — pip install edge-tts") from error

    raw_path = os.path.join(tempfile.gettempdir(), f"reachy_say_{uuid.uuid4().hex}.mp3")
    communicate = _edge_tts_communicate(edge_tts, text, voice)
    # Stream (what .save() does internally) so we can capture the total speech
    # duration from the WordBoundary offsets for free — used to wait out
    # playback so sequential says don't cut each other off.
    speech_ticks = 0
    with open(raw_path, "wb") as _f:
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                _f.write(chunk["data"])  # pyright: ignore[reportTypedDictNotRequiredAccess]
            elif chunk.get("type") in ("WordBoundary", "SentenceBoundary"):
                speech_ticks = max(
                    speech_ticks,
                    int(chunk.get("offset", 0)) + int(chunk.get("duration", 0)),
                )
    # edge-tts uses 100-ns ticks. Some versions default to SentenceBoundary
    # metadata while older versions may emit no timing metadata at all. Never
    # let an unknown duration become a zero wait: the next play_sound call would
    # then replace this clip while it is still speaking.
    speech_seconds = _speech_duration_seconds(text, speech_ticks)

    # -- Loudness boost (default on) --------------------------------------------
    # The boost compresses + limits the TTS file to the digital ceiling (loudest
    # the file can be). Robot speaker loudness on top of that is the persistent
    # volume (daemon gain, set via cmd=volume). per-call gain_db (<=0) trims this
    # one utterance quieter at the file level without touching the volume setting.
    trim_db = min(0.0, float(payload.get("gain_db", 0)))
    play_path = raw_path
    if payload.get("loud", True):
        boosted = await _boost_audio(agent, raw_path, trim_db)
        if boosted:
            try:
                os.unlink(raw_path)  # raw is consumed by the boost step
            except Exception:
                pass
            play_path = boosted
    return {
        "raw_path": raw_path,
        "play_path": play_path,
        "voice": voice,
        "speech_seconds": speech_seconds,
        "trim_db": trim_db,
    }


async def _discard_prepared(task):
    """Throw away a sentence prepared for a reply that stopped before reaching it."""
    if task is None:
        return
    task.cancel()
    prepared = (await asyncio.gather(task, return_exceptions=True))[0]
    if not isinstance(prepared, dict):
        return
    for key in ("play_path", "raw_path"):
        path = prepared.get(key)
        if not path:
            continue
        try:
            os.unlink(path)
        except Exception:
            pass


async def _say(agent, payload):
    """Speak text through Reachy's speaker using edge-tts for synthesis.

    Pipeline: edge-tts synthesizes the text to an MP3 file, then the Reachy
    SDK's media manager plays it via GStreamer (mini.media.play_sound, which
    decodes MP3 through playbin and routes to the robot/host audio sink).

    play_sound is fire-and-forget (GStreamer set_state(PLAYING) returns
    immediately), so we keep the temp file on disk and only delete the
    PREVIOUS utterance's file on the next call — deleting too early would
    cut playback off mid-sentence.

    Voice is any edge-tts voice name; defaults to TTS_VOICE env var or
    en-US-JennyNeural. Override per-call: {"cmd":"say","text":"hi","voice":"en-GB-RyanNeural"}

    Loudness: edge-tts output is quiet (~-22 dB mean) — too soft for a room/
    audience. When ffmpeg is available we compress + limit the speech to the
    digital ceiling (~-11 dB mean, roughly 3-4x perceived loudness) before
    playback. That is the loudest software can make it; the persistent volume
    setting and gain_db only attenuate DOWN from there. Controls:
      {"loud": false}      → skip the boost, play the raw (quiet) TTS
      {"gain_db": -8}      → this utterance only, 8 dB below max (quieter)
    """
    text = (payload.get("text") or payload.get("message") or payload.get("say") or "").strip()
    if not text:
        raise ValueError("say requires {'text': '...'}")
    text = text[:500]

    mini = agent.state.get("mini")
    if mini is None:
        raise RuntimeError("reachy not connected")

    # The media manager only has a live audio backend when media_backend != "no_media".
    media = getattr(mini, "media", None) or getattr(mini, "media_manager", None)
    if media is None:
        raise RuntimeError("reachy SDK exposes no media manager (mini.media)")
    audio = getattr(media, "audio", None)
    if audio is None:
        raise RuntimeError(
            "reachy audio backend is not initialized — media_backend is "
            f"'{agent.state.get('media_backend') or 'default'}'. Publish "
            '{"media_backend": ""} to custom/reachy/config, then say "reconnect".'
        )

    # Where will the sound come out? Only the WebRTC backend routes to the ROBOT
    # speaker (play_sound uploads the file and calls the daemon's
    # /api/media/play_sound). The LOCAL/gstreamer backend plays on THIS host's
    # speakers. Detect via the daemon_url the WebRTC client carries.
    daemon_url = getattr(audio, "daemon_url", None) or getattr(media, "_daemon_url", None)
    plays_on_robot = bool(daemon_url)
    if not plays_on_robot:
        await agent.log(
            "say will play on the HOST machine, not the robot — this backend "
            f"('{agent.state.get('media_backend') or 'default'}') uses local "
            'audio. Publish {"media_backend": "webrtc"} to custom/reachy/config '
            "and restart so play_sound routes to the robot's speaker.",
            level="warning",
        )

    # Already synthesized by the sentence before this one, when there was one.
    prepared = payload.get("_prepared") or await _prepare_speech(agent, text, payload)
    raw_path = prepared["raw_path"]
    play_path = prepared["play_path"]
    speech_seconds = prepared["speech_seconds"]
    voice = prepared["voice"]
    trim_db = prepared["trim_db"]

    # Clear any stale 'shutup' request from a previous utterance before we start.
    # NOT for a continuation chunk: the sentences of one reply are separate says,
    # so clearing here let a 'shutup' silence the sentence being spoken and then
    # play the rest of the reply anyway. _speak_reply clears once per reply.
    if not payload.get("_continuation"):
        agent.state["stop_speaking"] = False

    # -- Play through the robot's speaker (non-blocking GStreamer playbin) --
    await _do(media.play_sound, play_path)
    agent.state["_speaking"] = True

    # play_sound returns immediately; block for the utterance's length so a
    # following say (or volume change) in the same plan doesn't stomp this one
    # mid-word. Opt out with {"await_playback": false} for a single fire-and-
    # forget say. The wait is INTERRUPTIBLE: a concurrent 'shutup'/'stop' sets
    # stop_speaking (and cuts the audio), so a long utterance ends early instead
    # of blocking for its full length.
    pad = _say_playback_pad(speech_seconds, payload)
    session = payload.get("_barge_in_session")
    monitor = None
    interrupted = False
    if pad > 0 and session is not None:
        try:
            monitor = await _begin_barge_in_monitor(agent, session, speech_seconds)
        except Exception as e:
            # Some SDK/media combinations cannot record while playing. Keep
            # ordinary playback working and expose the degraded UX in the log.
            await agent.log(f"barge-in unavailable for this utterance: {e}", level="warning")
    waited = 0.0
    stopped = False
    onset_seen = False
    try:
        while waited < pad:
            if agent.state.get("stop_speaking"):
                stopped = True
                break
            if monitor is not None and not onset_seen and monitor[1].is_set():
                # Onset is a suspicion, not a verdict. Stopping here is what cut
                # him off mid-word, because the microphone hears his own speaker
                # and his voice satisfies any onset test. The sentence is short,
                # so it finishes and the recording is judged afterwards; a real
                # interruption costs the rest of one sentence, which reads as
                # letting him finish rather than as ignoring you.
                onset_seen = True
            chunk = min(0.05, pad - waited)
            await asyncio.sleep(chunk)
            waited += chunk
    finally:
        # Cancellation (conversation_stop) must never leave the microphone
        # gate believing Reachy is still speaking.
        agent.state["_speaking"] = False
        pending_check = session.get("barge_check") if session else None
        if pending_check is not None and pending_check.done():
            pending_check = None
        if (
            monitor is not None
            and onset_seen
            and not stopped
            and pending_check is None
            and session is not None
        ):
            # Checking costs a recogniser pass — about two seconds on the robot
            # — and his own voice trips the onset on nearly every sentence, so
            # doing it here put that pause between all of them and made him
            # sound slow. It runs alongside the next sentence instead, and
            # `_speak_reply` stops as soon as it comes back confirmed.
            session["barge_check"] = asyncio.create_task(
                _check_barge_in_later(agent, session, monitor, text)
            )
        else:
            # A check already running is looking at an interruption; a second
            # one would queue another recogniser pass and, worse, replace the
            # answer the first is about to give.
            await _finish_barge_in_monitor(session, monitor, False)

    # Clean up the previous utterance now that a new one is playing.
    prev = agent.state.get("_say_tmp")
    if prev and prev != play_path:
        try:
            os.unlink(prev)
        except Exception:
            pass
    agent.state["_say_tmp"] = play_path

    return {
        "said": text,
        "voice": voice,
        "trim_db": trim_db,
        "interrupted": interrupted,
        # Explicit 'shutup'/'stop', as opposed to `interrupted` (the user
        # talking over Reachy). The caller must stop feeding it sentences.
        "stopped": stopped,
        "duration_s": round(speech_seconds, 2),
        "volume_level": agent.state.get("volume_level", 100),
        "boosted": play_path != raw_path,
        "on_robot": plays_on_robot,
        "output": "robot" if plays_on_robot else "host (set media_backend=webrtc)",
    }


async def _boost_audio(agent, src_path, attenuation_db=0.0):
    """Compress + limit speech to the loudest clean level via ffmpeg.

    Returns the path to a new boosted MP3, or None if ffmpeg is unavailable or
    fails (caller falls back to the raw file). Raw edge-tts is ~-22 dB mean;
    the chain brings it to ~-11 dB at the digital ceiling (roughly 3-4x
    perceived loudness) — that's the maximum. attenuation_db (<=0) dials the
    final level DOWN from there for quieter playback.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        # Not an error: speech already synthesized fine and WILL play — ffmpeg only
        # makes it louder. Say so clearly, and only once per session so a tester
        # isn't spooked by a warning on every single utterance.
        if not agent.state.get("_ffmpeg_missing_logged"):
            agent.state["_ffmpeg_missing_logged"] = True
            await agent.log(
                "ffmpeg not installed — Reachy will still speak, just at a lower "
                "volume (the optional loudness boost is skipped). Install ffmpeg on "
                "this host if the speech is too quiet for the room. This is the only "
                "time this notice will be logged.",
                level="info",
            )
        return None
    af = "acompressor=threshold=-20dB:ratio=9:attack=5:release=50:makeup=10,alimiter=limit=0.97"
    attenuation_db = min(0.0, float(attenuation_db))
    if attenuation_db < 0:
        af += f",volume={attenuation_db:.1f}dB"
    out_path = os.path.join(tempfile.gettempdir(), f"reachy_say_{uuid.uuid4().hex}.mp3")

    def _run():
        # Generous rather than tight: this is a few seconds of speech, so 60s
        # only ever fires on a wedged ffmpeg. Without it a hung process holds an
        # executor thread for the life of the agent, and `say` never returns.
        return subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                src_path,
                "-af",
                af,
                out_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

    try:
        proc = await _do(_run)
    except Exception as e:
        # A timeout or a crash can still have created the output file; the
        # success path renames it away, this one has to clear it up itself.
        try:
            os.unlink(out_path)
        except OSError:
            pass
        await agent.log(f"ffmpeg boost failed ({e}) — playing raw TTS", level="warning")
        return None
    if proc.returncode != 0 or not os.path.exists(out_path):
        await agent.log(
            f"ffmpeg boost error — playing raw TTS: {proc.stderr[:200]}", level="warning"
        )
        try:
            os.unlink(out_path)
        except Exception:
            pass
        return None
    return out_path


# Daemon audio-gain API (reachy_mini PR #1187): POST /api/audio/gain {"gain_db": x}
# applies a GStreamer volume+rglimiter on the robot's playback pipeline, so this
# is the REAL robot speaker loudness (same lever as the dashboard SPEAKER slider).
_ROBOT_GAIN_MIN_DB = -20.0
_ROBOT_GAIN_MAX_DB = 24.0

# Human-friendly speaking modes -> 0-100 speaker level. The Mini's speaker curve
# is heavily top-loaded: measured on real hardware, ~35 is inaudible, ~60 sounds
# like a whisper, and 100 is a satisfactory shout. So the usable band is roughly
# 65-100 — these presets live there instead of the dead lower half.
_VOLUME_PRESETS = {
    "whisper": 70,  # soft but reliably audible (below ~65 is wasted)
    "normal": 85,  # clear conversational, one-on-one
    "louder": 93,  # room-filling / small group
    "presenter": 100,  # presentation / audience / shout — the ceiling
}


def _level_to_db(level):
    """Map a 0-100 volume level to the daemon gain range (-20..+24 dB)."""
    level = max(0.0, min(100.0, float(level)))
    return _ROBOT_GAIN_MIN_DB + level / 100.0 * (_ROBOT_GAIN_MAX_DB - _ROBOT_GAIN_MIN_DB)


def _db_to_level(db):
    """Inverse of _level_to_db: dB -> 0-100 level."""
    return round((float(db) - _ROBOT_GAIN_MIN_DB) / (_ROBOT_GAIN_MAX_DB - _ROBOT_GAIN_MIN_DB) * 100)


def _daemon_url(agent):
    """Base URL of the robot daemon's HTTP API, if reachable (WebRTC backend only)."""
    mini = agent.state.get("mini")
    media = getattr(mini, "media", None) or getattr(mini, "media_manager", None)
    audio = getattr(media, "audio", None)
    return (
        getattr(audio, "daemon_url", None)
        or getattr(media, "_daemon_url", None)
        or getattr(mini, "_daemon_http_url", None)
    )


async def _get_daemon_volume(agent):
    """GET the robot speaker volume (0-100) from the daemon, or None if unavailable."""
    import aiohttp

    url = _daemon_url(agent)
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{url.rstrip('/')}/api/volume/current", timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                if r.status != 200:
                    return None
                data = await r.json()
        v = data.get("volume")
        return int(v) if v is not None else None
    except Exception:
        return None


async def _apply_volume(agent, level):
    """Set the robot speaker volume (0-100) on the daemon. Returns (ok, detail).

    Primary: v1.7.x native POST /api/volume/set {"volume": 0-100} (what the
    dashboard slider uses; controls the "Reachy Mini Audio" device). Falls back
    to POST /api/audio/gain {"gain_db": ...} for daemons that ship PR #1187.
    """
    import aiohttp

    url = _daemon_url(agent)
    if not url:
        return False, (
            "no daemon URL — robot volume needs the WebRTC backend. "
            "Set REACHY_MEDIA_BACKEND=webrtc (or publish media_backend) and restart."
        )
    base = url.rstrip("/")
    level = int(max(0, min(100, round(level))))
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{base}/api/volume/set",
                json={"volume": level},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status < 400:
                    return True, f"volume/set={level}"
                if r.status != 404:
                    body = await r.text()
                    return False, f"volume/set {r.status}: {body[:160]}"
            # Older/newer daemon without /api/volume — try the PR #1187 gain endpoint.
            async with s.post(
                f"{base}/api/audio/gain",
                json={"gain_db": _level_to_db(level)},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status < 400:
                    return True, f"audio/gain={_level_to_db(level):.1f}dB"
                if r.status == 404:
                    return False, (
                        "daemon exposes neither /api/volume/set nor /api/audio/gain — "
                        "use the dashboard SPEAKER slider."
                    )
                body = await r.text()
                return False, f"audio/gain {r.status}: {body[:160]}"
    except Exception as e:
        return False, f"could not reach daemon at {base}: {e}"


async def _volume(agent, payload):
    """Set / adjust / mute the robot speaker volume (persists; survives restart).

    Drives the daemon's native speaker volume (0-100) — the actual "Reachy Mini
    Audio" output, same lever as the dashboard SPEAKER slider. The ffmpeg boost
    on each `say` is separate (it maximizes the file's digital level); this is the
    hardware output level on top. NOTE: the daemon plays a short test sound on set.

    One of (checked in this order):
      {"cmd":"volume","mute": true}       silence (remembers current level)
      {"cmd":"volume","mute": false}      restore the level from before muting
      {"cmd":"volume","preset": "whisper|normal|louder|presenter"}  human modes
      {"cmd":"volume","level": <0-100>}   absolute: 0=quietest, 100=loudest
      {"cmd":"volume","delta": <+/-pts>}  relative change (e.g. +15, -25)
      {"cmd":"volume","db": <-20..24>}    legacy: dB mapped onto 0-100
    """
    cur = int(agent.state.get("volume_level", 100) or 0)
    mute = payload.get("mute")
    preset = payload.get("preset")

    if mute is True:
        if not agent.state.get("muted"):
            agent.state["premute_level"] = cur  # remember to restore later
            agent.persist("premute_level", cur)
        level = 0
        agent.state["muted"] = True
        agent.persist("muted", True)
    elif mute is False:
        level = int(agent.state.get("premute_level", 100) or 0)
        agent.state["muted"] = False
        agent.persist("muted", False)
    elif preset is not None:
        key = str(preset).strip().lower()
        if key not in _VOLUME_PRESETS:
            raise ValueError(
                f"unknown volume preset {preset!r}; use one of {', '.join(_VOLUME_PRESETS)}"
            )
        level = _VOLUME_PRESETS[key]
    elif "level" in payload:
        level = float(payload["level"])
    elif "delta" in payload:
        level = cur + float(payload["delta"])
    elif "db" in payload:
        level = _db_to_level(float(payload["db"]))
    else:
        raise ValueError("volume requires mute, preset, level (0-100), delta, or db")

    level = int(max(0, min(100, round(level))))
    if mute is None:  # any explicit set clears mute
        agent.state["muted"] = False
        agent.persist("muted", False)
    agent.state["volume_level"] = level
    agent.persist("volume_level", level)

    ok, detail = await _apply_volume(agent, level)
    muted = bool(agent.state.get("muted"))
    # Always print volume changes to the CLI for debug/visibility.
    src = (
        "mute"
        if mute is True
        else "unmute"
        if mute is False
        else f"preset:{str(preset).lower()}"
        if preset is not None
        else "delta"
        if "delta" in payload
        else "level"
        if "level" in payload
        else "db"
    )
    if ok:
        await agent.log(f"🔊 volume -> {level}/100 (muted={muted}) via {src} [{detail}]")
    else:
        await agent.log(
            f"🔊 volume -> {level}/100 (muted={muted}) via {src} NOT applied: {detail}",
            level="warning",
        )
    result = {"level": level, "muted": muted, "applied": ok}
    if not ok:
        result["reason"] = detail
    return result


async def _ha_entities_via_agent(agent):
    """Light/switch inventory obtained FROM the home-assistant-agent — no direct HA
    REST. Only needed so the planner can fill real entity_ids into reachy-side
    reactive binds ('when the living-room light turns on, wake up'). Best-effort:
    returns {'lights': [...], 'switches': [...]}, empty on any failure.
    """
    try:
        res = await agent.send_to(
            "home-assistant-agent", {"text": "list all entities"}, timeout=20.0
        )
    except Exception as e:
        await agent.log(f"HA entity inventory via agent failed: {e}", level="warning")
        return {"lights": [], "switches": []}
    if not isinstance(res, dict):
        return {"lights": [], "switches": []}
    rows = res.get("entities") or (res.get("data") or {}).get("entities") or []
    lights, switches = [], []
    for r in rows:
        if not isinstance(r, dict):
            continue
        eid = str(r.get("entity_id", ""))
        name = r.get("name") or eid
        if eid.startswith("light."):
            lights.append({"entity_id": eid, "name": name})
        elif eid.startswith("switch."):
            switches.append({"entity_id": eid, "name": name})
    await agent.log(f"HA inventory via agent: {len(lights)} light(s), {len(switches)} switch(es)")
    return {"lights": lights, "switches": switches}


async def _ha(agent, payload):
    """Delegate Home Assistant work to the right Wactorz route.

    Reachy never calls Home Assistant directly. One-shot device control goes to
    main so it can use the OneOffActuatorAgent. HA metadata, questions, and
    automation CRUD go to home-assistant-agent.
    """
    request = (
        payload.get("request")
        or payload.get("text")
        or payload.get("message")
        or payload.get("query")
        or ""
    ).strip()
    if not request:
        # Legacy structured form -> natural language for the actuator route.
        service = payload.get("service")
        entity_id = payload.get("entity_id")
        if service and "." in service:
            _, action = service.split(".", 1)
            verb = (
                "turn on"
                if action.endswith("turn_on")
                else "turn off"
                if action.endswith("turn_off")
                else action.replace("_", " ")
            )
            request = f"{verb} {entity_id}".strip() if entity_id else f"call service {service}"
        else:
            raise ValueError(
                "ha requires a natural-language 'request' (or legacy service+entity_id)"
            )

    delegate = _ha_delegate_for_request(request)
    if delegate == "actuator":
        result = await _ha_actuate(agent, request)
    else:
        result = await agent.send_to(delegate, {"text": request}, timeout=60.0)
        if result is None:
            raise RuntimeError(f"{delegate} did not respond (not running / no reply)")
    if isinstance(result, dict):
        if result.get("error") and not result.get("result"):
            raise RuntimeError(f"{delegate} error: {result['error']}")
        human_result = result.get("result") or result.get("response") or str(result)
        return {
            "delegated_to": delegate,
            "request": request,
            "ha_result": human_result,
            "result": human_result,
            "data": result.get("data"),
        }
    return {
        "delegated_to": delegate,
        "request": request,
        "ha_result": str(result),
        "result": str(result),
    }


def _ha_delegate_for_request(request):
    """Pick actuator for one-shot control, home-assistant-agent for HA management/info."""
    low = (request or "").lower()
    ha_agent_markers = (
        "automation",
        "automations",
        "list",
        "show",
        "what",
        "which",
        "who",
        "where",
        "history",
        "historical",
        "state",
        "status",
        "sensor",
        "sensors",
        "entity",
        "entities",
        "area",
        "areas",
        "device",
        "devices",
        "camera",
        "snapshot",
        "stream",
        "recommend",
        "recommendation",
    )
    return (
        "home-assistant-agent" if any(marker in low for marker in ha_agent_markers) else "actuator"
    )


async def _ha_actuate(agent, request):
    """Run the same one-off Home Assistant actuator used by main chat actuation."""
    from wactorz.agents.one_off_actuator_agent import OneOffActuatorAgent

    actor = agent._actor

    # Keying the future is the whole setup: `_result_futures` is on Actor, and
    # `Actor._dispatch` settles it from the RESULT's `_task_id` before any
    # handler runs. No correlation handler to install here.
    task_id = f"reachy_ha_{uuid.uuid4().hex[:8]}"
    future = asyncio.get_running_loop().create_future()
    actor._result_futures[task_id] = future
    try:
        await actor.spawn(
            OneOffActuatorAgent,
            request=request,
            llm_provider=getattr(actor, "_llm_provider", None),
            task_id=task_id,
            reply_to_id=actor.actor_id,
            persistence_dir=str(actor._persistence_dir.parent),
        )
        return await asyncio.wait_for(future, timeout=120.0)
    except asyncio.TimeoutError:
        return {"result": "Actuation timed out, please retry."}
    finally:
        actor._result_futures.pop(task_id, None)


# ============================================================
# Camera & microphone (onboard sensors)
# ============================================================
# These read the robot's perception hardware through the SDK MediaManager
# (mini.media). The captured bytes travel ONLY in the command's own result and,
# optionally, a one-shot event topic or a file on disk — they are NEVER written
# into the retained custom/reachy/state heartbeat, which republishes every few
# seconds and would balloon with image/audio blobs.


def _media(agent):
    """Return the SDK MediaManager (mini.media), or raise a clear error."""
    mini = agent.state.get("mini")
    if mini is None:
        raise RuntimeError("reachy not connected")
    media = getattr(mini, "media", None) or getattr(mini, "media_manager", None)
    if media is None:
        raise RuntimeError("reachy SDK exposes no media manager (mini.media)")
    return media


def _require_mic(agent, *, record=False, doa=False):
    """Return the media manager, or raise an actionable error when the mic array
    is not usable.

    Capability is judged ONLY by the input methods the mic path actually calls —
    start_recording / get_audio_sample (recording) and get_DoA (direction of
    arrival). It is deliberately NOT coupled to the output/playback object
    (mini.media.audio): that carries speech playback, not mic input, and can be
    absent on a working-mic backend. Dead-but-present mics are caught downstream
    by the no-samples and get_DoA-exception guards.
    """
    media = _media(agent)
    backend = agent.state.get("media_backend") or "default"
    missing = []
    if record:
        for m in ("start_recording", "get_audio_sample"):
            if not callable(getattr(media, m, None)):
                missing.append(m)
    if doa and not callable(getattr(media, "get_DoA", None)):
        missing.append("get_DoA")
    if missing:
        raise RuntimeError(
            "Reachy microphone array is unavailable: required microphone "
            "capability is not exposed by the current backend or SDK build "
            f"(backend '{backend}'; missing {', '.join(missing)}). A media-capable "
            'backend may help — e.g. publish {"media_backend": ""} to '
            'custom/reachy/config then say "reconnect" (or set '
            "REACHY_MEDIA_BACKEND= blank and restart) — but this is not "
            "guaranteed to be the cause."
        )
    return media


async def _read_daemon_doa(agent):
    """Read onboard DoA from the robot daemon for network/WebRTC clients."""
    import aiohttp

    url = _daemon_url(agent)
    if not url:
        return None
    endpoint = f"{url.rstrip('/')}/api/state/doa"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(endpoint, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 404:
                    return None
                if response.status != 200:
                    raise RuntimeError(f"daemon DoA endpoint returned HTTP {response.status}")
                data = await response.json()
        angle = data.get("angle")
        if angle is None:
            return None
        return float(angle), bool(data.get("speech_detected", False))
    except Exception as e:
        await agent.log(f"remote DoA read failed ({endpoint}): {e}", level="warning")
        return None


async def _read_doa(agent, media):
    """Read DoA locally, falling back to the robot daemon for WebRTC clients."""
    local_error = None
    try:
        doa = await _do(media.get_DoA)
        if doa:
            return doa
    except Exception as e:
        local_error = e

    # reachy_mini 1.8.0 WebRTC get_DoA() probes USB on this host instead of
    # transporting the robot's reading. The daemon exposes the onboard value.
    remote = await _read_daemon_doa(agent)
    if remote:
        return remote

    if local_error is not None:
        await agent.log(
            f"get_DoA failed (media_backend="
            f"'{agent.state.get('media_backend') or 'default'}'): {local_error}",
            level="error",
        )
        raise RuntimeError(f"direction-of-arrival read failed: {local_error}")
    return None


def _doa_angle_deg(doa):
    """Convert a raw SDK DoA reading to degrees — the single DoA-unit boundary.

    reachy_mini (1.8.x) reports direction-of-arrival in RADIANS (the ReSpeaker
    DOA_VALUE_RADIANS register); the rest of the mic path uses degrees for
    human-facing reporting. Convert here and NOWHERE else.
    Raises ValueError when the reading is non-numeric or non-finite (NaN / +/-inf)
    so a garbage value is never mapped to a motor target.
    """
    try:
        rad = float(doa[0])
    except (TypeError, ValueError, IndexError) as e:
        raise ValueError(f"unreadable DoA value: {doa!r}") from e
    if not math.isfinite(rad):
        raise ValueError(f"non-finite DoA value: {doa!r}")
    return math.degrees(rad)


def _encode_frame(frame, fmt="jpeg", quality=85):
    """Encode a BGR uint8 HxWx3 numpy frame to JPEG/PNG bytes via PIL.

    The daemon delivers frames in BGR order (OpenCV convention); PIL expects RGB,
    so we flip the channel axis before encoding. Returns (bytes, width, height).
    """
    from PIL import Image

    arr = frame
    # BGR -> RGB for correct colours (3-channel frames come out BGR).
    if getattr(arr, "ndim", 0) == 3 and arr.shape[2] == 3:
        arr = arr[:, :, ::-1]
    img = Image.fromarray(arr)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    fmt = (fmt or "jpeg").lower().lstrip(".")
    if fmt == "jpg":
        fmt = "jpeg"
    buf = io.BytesIO()
    if fmt == "jpeg":
        img.save(buf, format="JPEG", quality=int(quality))
    else:
        img.save(buf, format=fmt.upper())
    w, h = img.size
    return buf.getvalue(), w, h


async def _camera(agent, payload):
    """Grab one still frame from Reachy's onboard camera.

    Returns it as base64 (JPEG by default) so it rides task results / MQTT and
    can be chained into a vision agent — see also cmd=look_pixel for gaze.

      {"cmd":"camera"}                        -> {"image_b64":.., "format":"jpeg", "width":.., "height":..}
      {"cmd":"camera","format":"png"}
      {"cmd":"camera","quality":60}           -> JPEG quality (default 85)
      {"cmd":"camera","path":"/tmp/shot.jpg"} -> also save to disk
      {"cmd":"camera","publish":true}         -> also publish b64 to custom/reachy/camera
      {"cmd":"camera","include_b64":false}    -> omit the blob (pair with path/publish)
    """
    media = _media(agent)
    frame = await _do(media.get_frame)
    if frame is None:
        raise RuntimeError(
            "no camera frame available — this media backend has no video "
            f"(media_backend='{agent.state.get('media_backend') or 'default'}'). "
            "Use a video-capable backend and make sure the daemon holds the camera "
            "(no HF app running on the robot)."
        )
    fmt = str(payload.get("format", "jpeg")).lower().lstrip(".")
    data, w, h = await _do(_encode_frame, frame, fmt, int(payload.get("quality", 85)))
    b64 = base64.b64encode(data).decode("ascii")
    result = {
        "format": "jpeg" if fmt in ("jpg", "jpeg") else fmt,
        "width": w,
        "height": h,
        "bytes": len(data),
    }
    path = payload.get("path")
    if path:

        def _write():
            with open(path, "wb") as f:
                f.write(data)

        await _do(_write)
        result["path"] = path
    if payload.get("publish"):
        await agent.publish(
            "custom/reachy/camera",
            {
                "image_b64": b64,
                "format": result["format"],
                "width": w,
                "height": h,
                "ts": time.time(),
            },
        )
        result["published"] = "custom/reachy/camera"
    if payload.get("include_b64", True):
        result["image_b64"] = b64
    # Human-facing summary so the chat shows a line, not the base64 blob.
    extra = f", saved to {result['path']}" if result.get("path") else ""
    if result.get("published"):
        extra += ", published"
    result["result"] = f"Captured a {w}x{h} {result['format']} image ({len(data)} bytes){extra}."
    return result


async def _vision_describe(agent, b64_jpeg, question, detail=False):
    """Ask the vision-capable LLM to describe a base64 JPEG frame.

    Uses the Anthropic image content-block shape, which the configured provider
    forwards to the model unchanged. Returns the description text, or raises with
    the provider's error if the LLM is unavailable. `detail=False` keeps the
    answer to one short spoken sentence; `detail=True` gives the full paragraph.
    """
    llm = getattr(agent, "llm", None)
    if llm is None:
        raise RuntimeError("vision needs an LLM provider (none configured)")
    if detail:
        system = (
            "You are the eyes of a Reachy Mini robot. Describe what is ACTUALLY visible "
            "in the image in a natural paragraph the robot can say aloud: the main objects, "
            "any people, the layout, colours, and notable details. Do not invent anything "
            "you cannot see. If the image is black, blank, or unreadable, say exactly that."
        )
    else:
        system = (
            "You are the eyes of a Reachy Mini robot. In ONE short, natural sentence "
            "(roughly 15-25 words) say the gist of what you see — the main subject and "
            "setting — not a full inventory. Speak it aloud, plainly. Do not invent "
            "anything you cannot see. If the image is black, blank, or unreadable, say "
            "exactly that in a few words."
        )
    content = [
        {"type": "text", "text": question or "What do you see?"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_jpeg},
        },
    ]
    answer = await llm.complete(messages=[{"role": "user", "content": content}], system=system)
    answer = (answer or "").strip()
    # The LLM helper returns sentinel strings like "[No LLM configured]" / "[LLM error: ..]"
    # instead of raising — surface those as real errors rather than speaking them.
    if answer.startswith("[") and ("LLM error" in answer or "No LLM" in answer):
        raise RuntimeError(answer.strip("[]"))
    return answer


_AIM_DOWN = (
    "look down",
    "downward",
    "at the desk",
    "on the desk",
    "my desk",
    "the floor",
    "at the floor",
    "below",
)
_AIM_UP = ("look up", "upward", "at the ceiling", "the ceiling", "up high", "above")
_AIM_LEFT = ("look left", "to the left", "on the left", "to your left")
_AIM_RIGHT = ("look right", "to the right", "on the right", "to your right")


def _describe_aim(payload, q_low):
    """Head aim (pitch, yaw in degrees) for a look. Explicit {pitch,yaw} win;
    otherwise parse direction words; default (0, 0) = level and forward.

    pitch: down=+, up=-. yaw: left=+, right=-. Kept modest so the camera still
    frames the scene rather than staring at the floor/ceiling.
    """
    if "pitch" in payload or "yaw" in payload:
        return float(payload.get("pitch", 0)), float(payload.get("yaw", 0))
    pitch = yaw = 0.0
    if any(w in q_low for w in _AIM_DOWN):
        pitch = 22
    elif any(w in q_low for w in _AIM_UP):
        pitch = -22
    if any(w in q_low for w in _AIM_LEFT):
        yaw = 32
    elif any(w in q_low for w in _AIM_RIGHT):
        yaw = -32
    return pitch, yaw


async def _orient_head(agent, payload, q_low):
    """Point the head at the look aim before capturing (best-effort).

    Skipped by {"orient": false} or when the robot isn't connected. Failure is
    non-fatal — we just capture from the current pose and log a warning.
    """
    if not payload.get("orient", True):
        return
    ok, _reason = _is_connected(agent)
    if not ok:
        return
    pitch, relative_yaw = _describe_aim(payload, q_low)
    dur = float(payload.get("look_duration", 0.5))
    body_yaw = await _current_body_yaw_deg(agent)
    try:
        await _pose(
            agent,
            {
                "pitch": pitch,
                "yaw": body_yaw + relative_yaw,
                "body_yaw": body_yaw,
                "duration": dur,
            },
        )
        # goto_target is fire-and-forget, so wait out the move + a short settle
        # BEFORE the caller captures — otherwise the frame is blurred mid-turn.
        await asyncio.sleep(dur + 0.25)
    except Exception as e:
        await agent.log(f"orient before look failed ({e}); capturing as-is", level="warning")


async def _describe(agent, payload):
    """Capture one camera frame, describe it with the vision LLM, and speak the result.

    Unlike camera->say (which would just make up a line), this actually sends the
    frame to the model, so the spoken answer reflects what the robot really sees.

      {"cmd":"describe"}                          -> capture + vision + speak (BRIEF: one
                                                     short sentence, then offers a closer look)
      {"cmd":"describe","detail":true}            -> full detailed paragraph (also: say
                                                     "look closer" / "in detail" / "tell me more")
      {"cmd":"describe","question":"how many people?"}  -> answer a specific question
      {"cmd":"describe","say":false}              -> return the text, don't speak it
      {"cmd":"describe","path":"C:/shot.jpg"}     -> also save the frame to disk
      {"cmd":"describe","quality":60}             -> JPEG quality sent to the model (default 85)

    Before capturing, the head is oriented to the look aim (default level/forward)
    so a prior gesture or droop doesn't leave Reachy staring at the floor. Aim it
    with {"pitch":<deg>,"yaw":<deg>} or by saying "look down/up/left/right"; pass
    {"orient": false} to capture from the current pose without moving. To sweep the
    whole room instead of one view, use {"cmd":"look_around"}.
    """
    media = _media(agent)

    q = (payload.get("question") or payload.get("text") or payload.get("prompt") or "").strip()
    # Detail is opt-in: a keyword in the request, or {"detail": true}. Otherwise a
    # brief one-liner so Reachy doesn't monologue every glance.
    _DETAIL_HINTS = (
        "detail",
        "more",
        "closer",
        "thorough",
        "everything",
        "full",
        "elaborate",
        "look again",
        "closely",
    )
    detail = bool(payload.get("detail")) or any(h in q.lower() for h in _DETAIL_HINTS)
    is_general = not q  # no specific user question — a plain "what do you see"
    question = q or "What do you see?"

    # Orient BEFORE capturing: point the head where we're going to look (default:
    # level/forward) so we frame the scene instead of whatever pose a gesture or a
    # droop left us in. Aim with {pitch,yaw} or words like "look down/up/left".
    await _orient_head(agent, payload, q.lower())

    frame = await _do(media.get_frame)
    if frame is None:
        raise RuntimeError(
            "no camera frame available — this media backend has no video "
            f"(media_backend='{agent.state.get('media_backend') or 'default'}'). "
            "Make sure the daemon holds the camera (no HF app running on the robot)."
        )
    data, w, h = await _do(_encode_frame, frame, "jpeg", int(payload.get("quality", 85)))
    b64 = base64.b64encode(data).decode("ascii")

    # Save only when explicitly asked (frames are not persisted by default).
    path = payload.get("path")
    if path:

        def _write():
            with open(path, "wb") as f:
                f.write(data)

        await _do(_write)

    answer = await _vision_describe(agent, b64, question, detail=detail)
    if not answer:
        answer = "I captured an image but couldn't make out what's in it."

    # Nudge that more is available — only after a brief, general look, and not when
    # the view was unreadable. Kept short so the offer isn't itself a monologue.
    said = answer
    _unreadable = any(
        w in answer.lower() for w in ("black", "blank", "couldn't", "can't", "cannot")
    )
    if is_general and not detail and not _unreadable and not answer.rstrip().endswith("?"):
        said = answer.rstrip(".") + ". Want me to look closer?"
        # Arm the follow-up: a 'yes' next means 'do the detailed look'.
        agent.state["_pending_detail"] = time.time()
    else:
        # Detailed look, a specific question, or an unreadable frame — no open offer.
        agent.state["_pending_detail"] = None

    result = {
        "description": answer,
        "result": said,
        "said": said,
        "detail": detail,
        "width": w,
        "height": h,
        "bytes": len(data),
    }
    if path:
        result["path"] = path

    # Speak the real description on the robot (skip with say:false).
    # await_playback:false so this (often long) utterance doesn't hold the serial
    # actor mailbox — that way a "shut up" typed while it's talking gets through
    # and can cut it off.
    if payload.get("say", True):
        try:
            await _say(agent, {"text": said, "await_playback": False})
        except Exception as e:
            await agent.log(
                f"describe: speaking failed ({e}); returning text only", level="warning"
            )
    return result


async def _look_behind(agent, payload):
    """Turn toward the rear, capture that view, and keep the rear orientation."""
    current = await _current_body_yaw_deg(agent)
    if abs(current) < 80:  # noqa: SIM108
        target = -155.0 if current < 0 else 155.0
    else:
        target = current
    await _face_body(agent, target, payload)

    describe_payload = dict(payload)
    describe_payload["orient"] = False
    describe_payload["question"] = str(payload.get("question") or "Describe what is behind you.")
    result = await _describe(agent, describe_payload)
    result["facing"] = "rear"
    result["body_yaw"] = target
    return result


async def _vision_describe_multi(agent, b64_jpegs, question=None):
    """Describe a SCENE from several camera views (one multi-image vision call).

    Used by look_around: the frames are the same room from different head angles,
    so we ask for one coherent overview, not a per-frame list.
    """
    llm = getattr(agent, "llm", None)
    if llm is None:
        raise RuntimeError("vision needs an LLM provider (none configured)")
    system = (
        "You are the eyes of a Reachy Mini robot. The images are several camera views "
        "the robot captured while looking AROUND one room (e.g. left, ahead, right, up). "
        "Give ONE natural, moderately detailed description of the overall scene and room — "
        "combine the views into a coherent whole, do NOT list them separately or say "
        "'image 1/2'. Do not invent anything you cannot see. If the views are black or "
        "unreadable, say exactly that."
    )
    content = [{"type": "text", "text": question or "Look around and describe the room."}]
    for b in b64_jpegs:
        content.append(
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
        )
    answer = await llm.complete(messages=[{"role": "user", "content": content}], system=system)
    answer = (answer or "").strip()
    if answer.startswith("[") and ("LLM error" in answer or "No LLM" in answer):
        raise RuntimeError(answer.strip("[]"))
    return answer


async def _look_around(agent, payload) -> dict[str, Any]:
    """Sweep the head across a few angles, capture a frame at each, and describe
    the whole room from the combined views.

      {"cmd":"look_around"}                         -> pan left/ahead/right + up, one summary
      {"cmd":"look_around","angles":[[0,40],[0,-40]]}  -> custom [pitch,yaw] stops (degrees)
      {"cmd":"look_around","question":"is anyone here?"}  -> ask about the whole room
      {"cmd":"look_around","say":false}             -> return text without speaking

    Costs one vision call over N frames; it re-centres the head when done.
    """
    media = _media(agent)
    # (pitch, yaw) stops in degrees. Default: pan left→ahead→right, then a glance up.
    angles = payload.get("angles") or [[0, 40], [0, 0], [0, -40], [-18, 0]]
    quality = int(payload.get("quality", 80))
    dur = float(payload.get("look_duration", 0.5))

    frames = []
    for stop in angles:
        try:
            p, y = float(stop[0]), float(stop[1])
        except (TypeError, ValueError, IndexError):
            continue
        try:
            await _pose(agent, {"pitch": p, "yaw": y, "duration": dur})
            # Wait out the move + settle BEFORE grabbing the frame — goto_target is
            # fire-and-forget, so capturing right away yields a motion-blurred shot.
            await asyncio.sleep(dur + 0.3)
            frame = await _do(media.get_frame)
            if frame is None:
                continue
            data, _w, _h = await _do(_encode_frame, frame, "jpeg", quality)
            frames.append(base64.b64encode(data).decode("ascii"))
        except Exception as e:
            await agent.log(f"look_around: view {stop} failed: {e}", level="warning")

    # Always try to re-centre so we don't leave the head cocked to one side.
    try:
        await _pose(agent, {"pitch": 0, "yaw": 0, "duration": dur})
    except Exception:
        pass

    if not frames:
        raise RuntimeError("look_around captured no frames (camera unavailable?)")

    answer = await _vision_describe_multi(agent, frames, payload.get("question"))
    if not answer:
        answer = "I looked around but couldn't make out the room."
    if payload.get("say", True):
        try:
            await _say(agent, {"text": answer, "await_playback": False})
        except Exception as e:
            await agent.log(
                f"look_around: speaking failed ({e}); returning text only", level="warning"
            )
    return {"description": answer, "result": answer, "said": answer, "views": len(frames)}


def _flush_audio_queue(media, max_reads=64, max_seconds=0.15) -> int:
    """Discard a bounded amount of queued WebRTC audio before push-to-talk starts."""
    deadline = time.monotonic() + max(0.0, float(max_seconds))
    reads = 0
    while reads < max_reads and time.monotonic() < deadline:
        if media.get_audio_sample() is None:
            break
        reads += 1
    return reads


def _trim_audio_window(audio, samplerate, channels, duration):
    """Keep only the newest requested-duration samples from an SDK buffer."""
    import numpy as _np

    arr = _np.asarray(audio)
    target_frames = max(1, round(float(duration) * int(samplerate or 16000)))
    if arr.ndim == 2:
        return arr[-target_frames:]
    target_values = target_frames * max(1, int(channels or 1))
    return arr[-target_values:]


def _record_audio_blocking(media, duration, max_frames):
    """Blocking: record ~`duration` seconds of mic audio off the event loop.

    Runs the SDK start/get_sample/stop loop, accumulating float32 samples until
    the deadline (or a defensive frame cap). Returns (ndarray, samplerate,
    channels). ndarray is 1-D (mono) or 2-D (frames, channels).
    """
    import numpy as _np

    media.start_recording()
    chunks = deque()
    total = 0
    try:
        # WebRTC may retain audio from before the command. Drain a short bounded
        # pre-roll, then capture for the full wall-clock window below.
        _flush_audio_queue(media)
        deadline = time.time() + max(0.05, float(duration))
        while time.time() < deadline:
            s = media.get_audio_sample()
            if s is None:
                time.sleep(0.005)
                continue
            chunks.append(s)
            total += s.shape[0]
            # The SDK can yield queued samples faster than real time. Keep a
            # bounded rolling buffer but never end the requested wall window early.
            while max_frames and total > max_frames and len(chunks) > 1:
                total -= chunks.popleft().shape[0]
    finally:
        try:
            media.stop_recording()
        except Exception:
            pass
    try:
        sr = int(media.get_input_audio_samplerate())
    except Exception:
        sr = 16000
    try:
        ch = int(media.get_input_channels())
    except Exception:
        ch = 1
    audio = _np.concatenate(list(chunks), axis=0) if chunks else _np.zeros((0,), dtype=_np.float32)
    if max_frames and audio.shape[0] > max_frames:
        audio = audio[-max_frames:]
    audio = _trim_audio_window(audio, sr, ch, duration)
    return audio, sr, ch


def _pcm_to_wav_b64(audio, samplerate, channels):
    """Encode a float32 [-1,1] (or int16) numpy array to a base64 WAV string.

    Uses stdlib `wave` (no soundfile dependency): float samples are clipped and
    scaled to signed 16-bit PCM. Returns (b64_str, frames_per_channel).
    """
    import numpy as _np

    arr = _np.asarray(audio)
    if arr.dtype.kind == "f":
        pcm = (_np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2")
    else:
        pcm = arr.astype("<i2")
    if pcm.ndim == 2:
        frames, ch = pcm.shape
    else:
        ch = max(1, int(channels or 1))
        frames = pcm.shape[0] // ch if ch else pcm.shape[0]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(int(samplerate) or 16000)
        w.writeframes(pcm.tobytes())
    return base64.b64encode(buf.getvalue()).decode("ascii"), int(frames)


async def _listen(agent, payload) -> dict[str, Any]:
    """Record a short clip from Reachy's microphone array.

    Returns base64 WAV (16-bit PCM) plus samplerate / channels / duration, and a
    best-effort direction-of-arrival snapshot. Like camera, the audio bytes go
    only into this result / an optional event / a file — never the heartbeat.

      {"cmd":"listen"}                        -> ~3 s clip: {"audio_b64":.., "samplerate":.., ...}
      {"cmd":"listen","duration":5}           -> seconds (clamped 0.1-30)
      {"cmd":"listen","path":"/tmp/clip.wav"} -> also save to disk
      {"cmd":"listen","publish":true}         -> also publish to custom/reachy/audio
      {"cmd":"listen","include_b64":false}    -> omit the blob (pair with path/publish)
    """
    media = _require_mic(agent, record=True)
    backend = agent.state.get("media_backend") or "default"
    duration = max(0.1, min(30.0, float(payload.get("duration", 3.0))))
    # Defensive frame cap: 30 s at 48 kHz.
    capture_started = time.time()
    audio, sr, ch = await _do(_record_audio_blocking, media, duration, 30 * 48000)
    capture_duration = round(time.time() - capture_started, 3)
    b64, frames = await _do(_pcm_to_wav_b64, audio, sr, ch)
    # Silent-failure guard: an initialized-but-dead mic yields an empty buffer.
    # Fail loudly instead of returning ok:true with a 0-second WAV.
    if frames <= 0:
        raise RuntimeError(
            f"reachy recorded no audio — the mic array returned no samples in "
            f"{duration:.1f}s (media_backend='{backend}'). Check the mic is live "
            "and that no HF app on the robot is holding the audio device, then "
            "retry."
        )
    actual = round(frames / sr, 3) if sr else 0.0
    result = {
        "samplerate": sr,
        "channels": ch,
        "duration_s": actual,
        "capture_duration_s": capture_duration,
        "frames": frames,
        "format": "wav",
    }
    # DoA here is a best-effort supplement to the clip: log a failure (never
    # swallow it) but still return the audio we captured.
    try:
        doa = await _read_doa(agent, media)
        if doa:
            result["doa_deg"] = _doa_angle_deg(doa)
            if len(doa) > 1:
                result["voice_detected"] = bool(doa[1])
    except Exception as e:
        await agent.log(f"listen: DoA read failed (non-fatal): {e}", level="warning")
        result["doa_error"] = str(e)
    path = payload.get("path")
    if path:
        raw = base64.b64decode(b64)

        def _write():
            with open(path, "wb") as f:
                f.write(raw)

        await _do(_write)
        result["path"] = path
    if payload.get("publish"):
        await agent.publish(
            "custom/reachy/audio",
            {
                "audio_b64": b64,
                "format": "wav",
                "samplerate": sr,
                "channels": ch,
                "duration_s": actual,
                "ts": time.time(),
            },
        )
        result["published"] = "custom/reachy/audio"
    if payload.get("include_b64", True):
        result["audio_b64"] = b64
    # Human-facing summary so the chat shows a line, not the base64 blob.
    doa_str = f", sound from {result['doa_deg']:.0f}°" if "doa_deg" in result else ""
    result["result"] = f"Recorded {actual:.1f}s of audio ({sr} Hz, {ch}ch){doa_str}."
    return result


def _voice_stage_failure(stage, message, fields, started) -> None:
    fields["total_duration_s"] = round(time.time() - started, 3)
    raise _CommandStageError(stage, message, fields)


async def _ask_voice(agent, payload: dict[str, Any]) -> dict[str, Any]:
    """Push-to-talk: capture WAV -> STT -> existing main bridge -> Reachy speech."""
    started = time.time()
    fields = {
        "transcript": "",
        "capture_duration_s": 0.0,
        "transcription_duration_s": 0.0,
        "response_text": "",
        "total_duration_s": 0.0,
        "error": None,
    }
    listen_payload = {
        "duration": payload.get("duration", 5.0),
        "include_b64": True,
        "publish": False,
    }
    try:
        clip = await _listen(agent, listen_payload)
        fields["capture_duration_s"] = float(
            clip.get("capture_duration_s") or clip.get("duration_s") or 0.0
        )
        wav_bytes = base64.b64decode(clip.get("audio_b64") or "", validate=True)
        if not wav_bytes:
            raise RuntimeError("captured WAV is empty")
    except Exception as e:
        _voice_stage_failure("capture_failed", str(e), fields, started)

    transcription_started = time.time()
    try:
        from wactorz.catalogue_agents.reachy_stt import transcribe_wav

        transcription = await transcribe_wav(wav_bytes, _conversation_stt_payload(payload))
        fields["transcription_duration_s"] = round(time.time() - transcription_started, 3)
        fields["transcript"] = _normalize_reachy_transcript(str(transcription.text or "").strip())
        fields["stt_backend"] = transcription.backend
        fields["stt_model"] = transcription.model
    except Exception as e:
        fields["transcription_duration_s"] = round(time.time() - transcription_started, 3)
        _voice_stage_failure("transcription_failed", str(e), fields, started)
    if not fields["transcript"]:
        _voice_stage_failure(
            "empty_transcript", "speech-to-text returned no words", fields, started
        )

    task_id = payload.get("_task_id") or payload.get("task") or payload.get("id")
    try:
        bridged = await _bridge_to_main(agent, fields["transcript"], task_id, voice_input=True)
    except Exception as e:
        _voice_stage_failure("routing_failed", str(e), fields, started)
    if bridged is None:
        _voice_stage_failure("routing_failed", "main returned no usable response", fields, started)
    if not isinstance(bridged, dict):
        bridged = {}
    fields["response_text"] = str(bridged.get("result") or "").strip()
    if not fields["response_text"]:
        _voice_stage_failure("routing_failed", "main returned an empty response", fields, started)
    fields["result"] = fields["response_text"]
    if not bridged.get("spoke"):
        _voice_stage_failure(
            "speech_failed",
            bridged.get("speech_error") or "Reachy did not play the response",
            fields,
            started,
        )
    fields["total_duration_s"] = round(time.time() - started, 3)
    fields["said"] = fields["response_text"]
    fields["bridged"] = True
    fields["spoke"] = True
    return fields


#: Ways of typing "end the conversation" in chat. The spoken phrases below are
#: built from these, so a wording that works typed cannot fail when said —
#: which is what happened to "stop conversation": chat understood it and the
#: voice session did not, so saying it was answered with "Goodbye!" while the
#: microphone stayed on.
_CONVERSATION_STOP_COMMANDS = (
    "stop conversation",
    "end conversation",
    "stop the conversation",
    "end the conversation",
)

_CONVERSATION_STOP_PHRASES = {
    "stop listening",
    "goodbye reachy",
    "bye",
    "goodbye",
    "that s all",
    "that is all",
    "σταμάτα",
    "σταμάτα να ακούς",
    "τέλος συζήτησης",
    "αντίο",
    "αντίο reachy",
} | set(_CONVERSATION_STOP_COMMANDS)


#: Words that pad a request without changing it. Stripped from the ends only,
#: so "you can stop talking right now" reaches "stop talking" while "stop the
#: music" reaches nothing and stays an ordinary request. None of the phrases
#: below begin or end with one of these, so trimming can never eat a real one —
#: "shut up" keeps its "up" because "up" is not here.
_PHRASE_EDGE_FILLER = frozenset(
    {
        "please",
        "ok",
        "okay",
        "now",
        "just",
        "right",
        "reachy",
        "hey",
        "well",
        "you",
        "can",
        "could",
        "would",
        "will",
        "i",
        "need",
        "want",
        "to",
        "thanks",
        "thank",
        "for",
        "me",
        "a",
        "second",
        "moment",
        "λοιπόν",
        "σε",
        "παρακαλώ",
        "τώρα",
        "ρίτσι",
    }
)


def _phrase_forms(text: str) -> list[str]:
    """The request as said, then with politeness trimmed off either end.

    Matching was exact, so "Stop talking, please!" missed "stop talking" and was
    answered as a new question — Reachy replying "Of course, I'll be quiet!"
    instead of going quiet. Trimming only the ends, and only these words, adds
    the polite forms without letting a sentence that merely mentions stopping
    become a command.
    """
    normalized = re.sub(r"[^\w ]+", " ", str(text or "").lower(), flags=re.UNICODE)
    normalized = " ".join(normalized.replace("_", " ").split())
    forms = [normalized]
    words = normalized.split()
    while words and words[0] in _PHRASE_EDGE_FILLER:
        words.pop(0)
    while words and words[-1] in _PHRASE_EDGE_FILLER:
        words.pop()
    trimmed = " ".join(words)
    if trimmed and trimmed != normalized:
        forms.append(trimmed)
    return forms


def _conversation_stop_phrase(text: str) -> bool:
    return any(form in _CONVERSATION_STOP_PHRASES for form in _phrase_forms(text))


_CONVERSATION_SILENCE_PHRASES = {
    "stop",
    "silence",
    "quiet",
    "be quiet",
    "shut up",
    "stop talking",
    "stop speaking",
    "stop it",
    "hush",
    "enough",
    "that s enough",
}


def _conversation_silence_phrase(text):
    """True when the user wants silence without ending the voice session."""
    return any(form in _CONVERSATION_SILENCE_PHRASES for form in _phrase_forms(text))


def _conversation_stt_payload(payload):
    """Resolve voice-session STT hints without forcing a spoken language."""
    resolved = dict(payload or {})
    reachy_hotwords = "Reachy, Reachy Mini, hey Reachy"
    configured_hotwords = str(
        resolved.get("stt_hotwords") or os.environ.get("REACHY_STT_HOTWORDS") or ""
    ).strip()
    if configured_hotwords:
        resolved["stt_hotwords"] = (
            configured_hotwords
            if "reachy" in configured_hotwords.casefold()
            else f"{reachy_hotwords}, {configured_hotwords}"
        )
    else:
        resolved["stt_hotwords"] = (
            f"{reachy_hotwords}, Wactorz, Home Assistant, main light, light strip, Tapo"
        )
    fallback = resolved.get("stt_fallback_language") or os.environ.get(
        "REACHY_STT_FALLBACK_LANGUAGE"
    )
    if fallback:
        resolved["stt_fallback_language"] = str(fallback).strip().lower()
    return resolved


async def _conversation_transcribe(agent, wav_bytes, payload, session):
    """Retry uncertain auto-language results with a stable session fallback."""
    from wactorz.catalogue_agents.reachy_stt import transcribe_wav

    initial = await transcribe_wav(wav_bytes, payload)
    if payload.get("language") or payload.get("stt_language"):
        return initial, False

    language = str(getattr(initial, "language", "") or "").strip().lower()
    probability = getattr(initial, "language_probability", None)
    minimum = max(0.0, min(1.0, float(payload.get("stt_min_language_probability", 0.60))))
    if probability is None or float(probability) >= minimum:
        if language:
            session["stt_language_hint"] = language
        return initial, False

    fallback = (
        str(payload.get("stt_fallback_language") or session.get("stt_language_hint") or "")
        .strip()
        .lower()
    )
    if not fallback or fallback == language:
        return initial, False

    await agent.log(
        f"uncertain STT language {language or 'unknown'!r} "
        f"({float(probability):.2f}); retrying as {fallback}",
        level="info",
    )
    retry_payload = dict(payload)
    retry_payload["stt_language"] = fallback
    retried = await transcribe_wav(wav_bytes, retry_payload)
    session["stt_retry_count"] = int(session.get("stt_retry_count") or 0) + 1
    return retried, True


def _conversation_transcript_is_meaningful(text):
    """Accept words in any script while rejecting punctuation-only STT output."""
    return any(character.isalnum() for character in str(text or ""))


def _conversation_transcription_is_credible(transcription, payload):
    """Reject model-declared no-speech, low-confidence, or uncertain-language text."""
    confidence = getattr(transcription, "confidence", None)
    no_speech = getattr(transcription, "no_speech_probability", None)
    language_probability = getattr(transcription, "language_probability", None)
    min_confidence = max(0.0, min(1.0, float(payload.get("stt_min_confidence", 0.25))))
    max_no_speech = max(0.0, min(1.0, float(payload.get("stt_max_no_speech", 0.60))))
    min_language = max(0.0, min(1.0, float(payload.get("stt_min_language_probability", 0.60))))
    if no_speech is not None and float(no_speech) > max_no_speech:
        return False, f"no_speech_probability={float(no_speech):.3f}"
    if confidence is not None and float(confidence) < min_confidence:
        return False, f"confidence={float(confidence):.3f}"
    if language_probability is not None and float(language_probability) < min_language:
        return False, f"language_probability={float(language_probability):.3f}"
    return True, ""


async def _conversation_show_transcript(agent, session, transcript):
    """Publish recognized speech as a user-authored dashboard chat turn."""
    try:
        await agent.notify_user(
            transcript,
            **{
                "from": "user",
                "to": getattr(agent, "name", "reachy-mini"),
                "source": "voice",
                "surface": getattr(agent, "name", "reachy-mini"),
                "surface_label": "Reachy",
                "session_id": session.get("session_id", ""),
            },
        )
    except Exception as e:
        await agent.log(f"voice transcript chat publish failed: {e}", level="warning")


def _conversation_turn(session):
    return {
        "session_id": session.get("session_id"),
        "turn_index": int(session.get("turn_index") or 0),
        "state": session.get("state", "idle"),
        "transcript": "",
        "response": "",
        "raw_response": "",
        "capture_duration_s": 0.0,
        "transcription_duration_s": 0.0,
        "stt_confidence": None,
        "stt_no_speech_probability": None,
        "stt_language": None,
        "stt_language_probability": None,
        "stt_retried": False,
        "routing_duration_s": 0.0,
        "spoken_response": "",
        "interrupted": False,
        "stop_reason": None,
        "ok": True,
        "error": None,
    }


async def _conversation_state_motion(agent, state):
    """Set optional antenna cues without issuing a whole-head pose command."""
    poses = {
        "listening": (10, 10),
        "speaking": (3, 3),
        "stopped": (0, 0),
    }
    angles = poses.get(state)
    mini = agent.state.get("mini")
    np = agent.state.get("np")
    lock = agent.state.get("motion_lock")
    if angles is None or mini is None or np is None or lock is None:
        return
    fn = getattr(mini, "set_target", None)
    if not callable(fn):
        return
    try:
        async with lock:
            await _do(
                fn,
                antennas=np.deg2rad([angles[1], angles[0]]),
            )
    except Exception as e:
        await agent.log(f"conversation {state} motion skipped: {e}", level="warning")


async def _conversation_idle_antenna_target(agent, left, right):
    """Move only antennas; never send an implicit head or body target."""
    mini = agent.state.get("mini")
    np = agent.state.get("np")
    lock = agent.state.get("motion_lock")
    fn = getattr(mini, "set_target", None) if mini is not None else None
    if not callable(fn) or np is None or lock is None:
        return
    async with lock:
        await _do(fn, antennas=np.deg2rad([right, left]))


def _cancel_conversation_idle_motion(session):
    """Stop antenna motion without issuing another motor command."""
    task = session.get("_idle_motion_task")
    if task is not None and not task.done():
        task.cancel()
    session["_idle_motion_task"] = None


async def _conversation_idle_sweep(agent, session, start, end, duration=2.8):
    """Ease between tiny antenna poses instead of stepping the servos."""
    steps = max(8, int(duration / 0.14))
    for step in range(1, steps + 1):
        if session.get("state") != "listening" or session.get("cancel_event").is_set():
            return
        progress = step / steps
        eased = progress * progress * (3.0 - 2.0 * progress)
        left = start[0] + (end[0] - start[0]) * eased
        right = start[1] + (end[1] - start[1]) * eased
        await _conversation_idle_antenna_target(agent, left, right)
        session["_idle_angles"] = (left, right)
        await asyncio.sleep(duration / steps)


async def _conversation_idle_motion_loop(agent, session):
    """Give listening an occasional, fluid, low-amplitude antenna breath."""
    patterns = ((5, 7), (7, 5), (4, 6), (6, 4), (0, 0))
    index = int(session.get("turn_index") or 0) % len(patterns)
    current = tuple(session.get("_idle_angles") or (0.0, 0.0))
    try:
        await asyncio.sleep(1.8)
        while session.get("state") == "listening" and not session.get("cancel_event").is_set():
            target = patterns[index % len(patterns)]
            await _conversation_idle_sweep(agent, session, current, target)
            current = target
            index += 1
            await asyncio.sleep(2.5)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await agent.log(f"listening idle motion skipped: {e}", level="warning")


def _schedule_conversation_idle_motion(agent, session, state):
    """Run opt-in idle motion only during listening."""
    previous = session.get("_idle_motion_task")
    enabled = session.get("payload", {}).get("idle_motion", False)
    if state != "listening" or not enabled:
        _cancel_conversation_idle_motion(session)
        return
    if previous is not None and not previous.done():
        return
    session["_idle_motion_task"] = agent.run_in_background(
        _conversation_idle_motion_loop(agent, session)
    )


def _schedule_conversation_state_motion(agent, session, state):
    if not session.get("payload", {}).get("state_motion", False):
        return
    if session.get("_motion_state") == state:
        return
    previous = session.get("_motion_task")
    if previous is not None and not previous.done():
        previous.cancel()
    session["_motion_state"] = state
    session["_motion_task"] = agent.run_in_background(_conversation_state_motion(agent, state))


async def _conversation_publish(
    agent, session, state, turn=None, *, ok=True, error=None, stop_reason=None
):
    session["state"] = state
    agent.state["conversation_state"] = state
    _schedule_conversation_state_motion(agent, session, state)
    # Subtle listening idle "breath" disabled for now — not behaving as wanted.
    # Re-enable by uncommenting; the loop/scheduler below are left intact.
    # _schedule_conversation_idle_motion(agent, session, state)
    event = _conversation_turn(session)
    if turn:
        event.update(turn)
    event.update(
        {
            "session_id": session.get("session_id"),
            "turn_index": int(session.get("turn_index") or 0),
            "state": state,
            "stop_reason": stop_reason,
            "ok": bool(ok),
            "error": str(error) if error else None,
            "type": "conversation",
            "ts": time.time(),
        }
    )
    await agent.publish("custom/reachy/events", event)
    return event


def _conversation_route_text(transcript, history):
    """Keep the executable request pristine; history travels as structured data."""
    del history
    return transcript


async def _conversation_speech_bridge(agent, transcript, commands, task_id, before_speak, session):
    """Speak user-supplied words locally, with optional volume and gesture steps."""
    reply = next(
        (
            str(command.get("text") or "").strip()
            for command in commands
            if isinstance(command, dict) and command.get("cmd") == "say"
        ),
        "",
    )
    if not reply:
        return None

    # Put the words in chat before playback begins, matching normal conversation UX.
    await before_speak(reply)
    action_results = []
    interrupted = False
    speech_error = None
    for command in commands:
        if not isinstance(command, dict):
            continue
        local_command = dict(command)
        cmd = str(local_command.get("cmd") or "").strip().lower()
        if cmd == "say":
            local_command["await_playback"] = True
            local_command["_barge_in_session"] = session
        action = await _dispatch(agent, cmd, local_command, return_result=True)
        action = action or {"ok": False, "cmd": cmd, "error": "no result"}
        action_results.append(action)
        if not action.get("ok"):
            speech_error = str(action.get("error") or f"{cmd} failed")
            break
        if cmd == "say" and action.get("interrupted"):
            interrupted = True
            break

    ok = bool(action_results) and all(item.get("ok") for item in action_results)
    spoke = any(item.get("ok") and item.get("cmd") == "say" for item in action_results)
    return {
        "ok": ok,
        "cmd": "speech_sequence",
        "physical": any(
            isinstance(command, dict) and command.get("cmd") == "gesture" for command in commands
        ),
        "spoke": spoke,
        "interrupted": interrupted,
        "spoken_result": reply if spoke else "",
        "speech_error": speech_error,
        "result": reply,
        "action_results": action_results,
        "_task_id": task_id,
    }


async def _conversation_embodied_bridge(agent, transcript, command, task_id, before_speak, session):
    """Execute an obvious local request, then acknowledge it naturally."""
    local_command = dict(command)
    if command["cmd"] in ("describe", "look_behind", "look_around"):
        # Conversation mode owns TTS so visual answers are spoken exactly once.
        local_command["say"] = False
    action = await _dispatch(agent, command["cmd"], local_command, return_result=True)
    name = command.get("name")
    if action and action.get("ok"):
        if command["cmd"] == "volume":
            level = int(action.get("level", agent.state.get("volume_level", 0)))
            reply = f"Speaker volume is now {level} percent."
            greek = any("\u0370" <= char <= "\u03ff" for char in transcript.lower())
            quieter = float(command.get("delta", 0)) < 0
            if greek:
                spoken = "Εντάξει, πιο σιγά." if quieter else "Εντάξει, πιο δυνατά."
            else:
                spoken = "Okay, a little quieter." if quieter else "Okay, a little louder."
        else:
            reply = str(action.get("result") or "Done.")
            if command["cmd"] in ("describe", "look_behind", "look_around"):
                spoken = _voice_friendly_reply(reply, user_text=transcript)
            elif command["cmd"] in ("debug", "face_forward"):
                spoken = reply
            else:
                spoken = {
                    "dance": "Ta-da!",
                    "nod": "Mm-hm.",
                    "shake": "Nope.",
                    "wiggle": "How's that?",
                    "curious": "Hmm?",
                }.get(name, "Okay.")
    else:
        if command["cmd"] == "volume":
            reply = "I couldn't change my speaker volume right now."
        else:
            reply = "I wanted to move, but my motors aren't responding right now."
        spoken = reply
    await before_speak(reply)
    speech_error = None
    try:
        speech = await _speak_reply(
            agent,
            spoken,
            await_playback=True,
            session=session,
        )
    except Exception as e:
        speech_error = str(e)
        speech = {"spoke": False, "interrupted": False, "stopped": False, "spoken_result": ""}
        await agent.log(f"local acknowledgement failed: {e}", level="warning")
    return {
        "ok": bool(action and action.get("ok")),
        "cmd": command["cmd"],
        "physical": command["cmd"]
        in ("gesture", "turn", "look_behind", "look_around", "face_forward"),
        "spoke": speech["spoke"],
        "interrupted": speech["interrupted"],
        "spoken_result": speech["spoken_result"],
        "speech_error": speech_error,
        "result": reply,
        "_task_id": task_id,
    }


async def _conversation_blocking_worker(session, fn, *args):
    """Await a cancellable mic worker and let its thread observe the stop event."""
    worker = asyncio.create_task(_do(fn, *args))
    session["worker"] = worker
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        session["cancel_event"].set()
        try:
            await worker
        except Exception:
            pass
        raise
    finally:
        if session.get("worker") is worker:
            session["worker"] = None


async def _conversation_capture(agent, session, vad_config):
    pending = session.pop("pending_capture", None)
    if pending is not None:
        return pending
    from wactorz.catalogue_agents.reachy_vad import capture_utterance

    media = _require_mic(agent, record=True)
    loop = asyncio.get_running_loop()

    def _speech_started():
        loop.call_soon_threadsafe(_cancel_conversation_idle_motion, session)

    return await _conversation_blocking_worker(
        session, capture_utterance, media, session["cancel_event"], vad_config, _speech_started
    )


async def _conversation_cooldown(agent, session, turn, seconds):
    from wactorz.catalogue_agents.reachy_vad import drain_audio

    if seconds <= 0:
        return
    await _conversation_publish(agent, session, "cooldown", turn)
    media = _require_mic(agent, record=True)
    await _conversation_blocking_worker(
        session, drain_audio, media, seconds, session["cancel_event"]
    )


async def _conversation_start(agent, payload):
    existing = agent.state.get("conversation_session")
    if existing and existing.get("task") and not existing["task"].done():
        fields = _conversation_turn(existing)
        raise _CommandStageError(
            "session_active",
            f"conversation {existing['session_id']} is already active",
            fields,
        )

    resolved_payload = dict(payload)
    # The XVF configuration improves capture, but on some physical robots the
    # microphone still hears Reachy's speaker clearly enough to trip VAD for
    # the whole utterance. A cascaded STT -> LLM -> TTS pipeline cannot tell
    # that echo from a real interruption reliably, so uninterrupted speech is
    # the safe default. Full-duplex remains an explicit experimental option.
    resolved_payload.setdefault("barge_in", False)
    session = {
        "session_id": str(payload.get("session_id") or uuid.uuid4().hex[:12]),
        "turn_index": 0,
        "state": "idle",
        "cancel_event": threading.Event(),
        "stop_reason": None,
        "task": None,
        "worker": None,
        "pending_capture": None,
        "barge_task": None,
        "barge_cancel_event": None,
        "barge_in_count": 0,
        "_idle_motion_task": None,
        "_idle_angles": (0.0, 0.0),
        "payload": resolved_payload,
        "history": [],
        "stt_language_hint": None,
        "stt_retry_count": 0,
        "consecutive_errors": 0,
    }
    agent.state["conversation_session"] = session
    agent.state["conversation_state"] = "idle"
    session["task"] = agent.run_in_background(_conversation_loop(agent, session))
    result = _conversation_turn(session)
    barge_in = bool(resolved_payload["barge_in"])
    # Say which mode it is. Not knowing interruption was off reads as a broken
    # robot that ignores you, rather than a setting you did not ask for.
    interruption = (
        "Talk over me to interrupt."
        if barge_in
        else 'Wait for me to finish, or say "stop talking" to cut me off.'
    )
    result.update(
        {
            "started": True,
            "barge_in": barge_in,
            "result": (
                "Conversation started. Speak normally; voice turns appear under "
                f"Reachy. {interruption}"
            ),
        }
    )
    return result


async def _conversation_stop(agent, payload=None):
    payload = payload or {}
    session = agent.state.get("conversation_session")
    if not session:
        return {
            "session_id": None,
            "turn_index": 0,
            "state": "stopped",
            "transcript": "",
            "response": "",
            "capture_duration_s": 0.0,
            "transcription_duration_s": 0.0,
            "routing_duration_s": 0.0,
            "stop_reason": payload.get("reason") or "already_stopped",
            "ok": True,
            "error": None,
            "already_stopped": True,
            "result": "No conversation is active.",
        }
    reason = str(payload.get("reason") or "explicit_stop")
    session["stop_reason"] = reason
    session["cancel_event"].set()
    barge_cancel = session.get("barge_cancel_event")
    if barge_cancel is not None:
        barge_cancel.set()
    if session.get("state") == "speaking" or agent.state.get("_speaking"):
        await _stop_audio(agent)
    task = session.get("task")
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    result = _conversation_turn(session)
    result.update(
        {
            "state": "stopped",
            "stop_reason": reason,
            "stopped": True,
            "result": f"Conversation stopped ({reason}).",
        }
    )
    return result


async def _conversation_turn_error(agent, session, turn, error, max_errors):
    """Record one failed turn and say whether the session may continue.

    Turn events reach the dashboard over MQTT, which a session that ends early
    is not around to be read from. The log is what is left to explain why a
    robot that was asked to listen stopped answering, so the reason goes there
    too.
    """
    session["consecutive_errors"] = int(session.get("consecutive_errors") or 0) + 1
    keep_going = session["consecutive_errors"] < max_errors
    await agent.log(
        f"voice turn {int(session.get('turn_index') or 0)} failed "
        f"({session['consecutive_errors']}/{max_errors}): {error}"
        + ("" if keep_going else " — ending the conversation session"),
        level="warning",
    )
    await _conversation_publish(agent, session, "error", turn, ok=False, error=error)
    return keep_going


async def _conversation_loop(agent, session):
    from wactorz.catalogue_agents.reachy_vad import VADConfig

    payload = session["payload"]
    stt_payload = _conversation_stt_payload(payload)
    # Zero means a Siri-like persistent session; inactivity or an explicit phrase ends it.
    max_turns = max(0, min(1000, int(payload.get("max_turns", 0))))
    max_errors = max(1, min(10, int(payload.get("max_consecutive_errors", 3))))
    cooldown_s = max(0.0, min(5.0, float(payload.get("cooldown_s", 0.0))))
    inactivity_timeout = float(payload.get("inactivity_timeout", 0.0))
    vad_config = VADConfig(
        speech_start_timeout_s=(
            0.0 if inactivity_timeout <= 0 else max(1.0, min(86400.0, inactivity_timeout))
        ),
        silence_s=max(0.3, min(3.0, float(payload.get("silence_s", 1.0)))),
        max_utterance_s=max(1.0, min(30.0, float(payload.get("max_utterance_s", 20.0)))),
        min_speech_s=max(0.09, min(2.0, float(payload.get("min_speech_s", 0.2)))),
        pre_roll_s=max(0.0, min(1.0, float(payload.get("pre_roll_s", 0.3)))),
        flush_s=max(0.0, min(2.0, float(payload.get("flush_s", 0.08)))),
        mode=max(0, min(3, int(payload.get("vad_mode", 2)))),
        min_rms=max(0.0, min(1.0, float(payload.get("vad_min_rms", 0.01)))),
    )
    stopped_published = False
    turn: dict[str, Any] = _conversation_turn(session)
    try:
        turn_index = 0
        while max_turns == 0 or turn_index < max_turns:
            turn_index += 1
            session["turn_index"] = turn_index
            turn = _conversation_turn(session)
            await _conversation_publish(agent, session, "listening", turn)

            try:
                capture = await _conversation_capture(agent, session, vad_config)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await agent.log(
                    f"voice capture failed; ending the conversation session: {exc}",
                    level="warning",
                )
                await _conversation_publish(
                    agent, session, "error", turn, ok=False, error=exc, stop_reason="capture_failed"
                )
                session["stop_reason"] = "capture_failed"
                break
            _cancel_conversation_idle_motion(session)
            if tuple(session.get("_idle_angles") or (0, 0)) != (0, 0):
                await _conversation_idle_antenna_target(agent, 0, 0)
                session["_idle_angles"] = (0.0, 0.0)

            if capture.stop_reason == "cancelled":
                raise asyncio.CancelledError
            if capture.stop_reason == "inactivity_timeout":
                session["stop_reason"] = "inactivity_timeout"
                break
            if capture.stop_reason == "noise_rejected":
                # Mechanical clicks are not user turns and must not consume a
                # bounded session's turn count or its error budget.
                turn_index = max(0, turn_index - 1)
                session["turn_index"] = turn_index
                continue
            if not capture.audio.size:
                keep_going = await _conversation_turn_error(
                    agent, session, turn, "capture returned no speech", max_errors
                )
                if not keep_going:
                    session["stop_reason"] = "too_many_errors"
                    break
                continue

            turn["capture_duration_s"] = capture.duration_s
            await _conversation_publish(agent, session, "transcribing", turn)
            transcription_started = time.time()
            carried = session.pop("pending_transcript", None)
            reused = carried[1] if carried is not None and carried[0] is capture else None
            try:
                if reused is not None:
                    transcription, retried = reused, False
                else:
                    wav_b64, _frames = await _do(
                        _pcm_to_wav_b64, capture.audio, capture.samplerate, capture.channels
                    )
                    transcription, retried = await _conversation_transcribe(
                        agent, base64.b64decode(wav_b64), stt_payload, session
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                turn["transcription_duration_s"] = round(time.time() - transcription_started, 3)
                keep_going = await _conversation_turn_error(
                    agent, session, turn, f"transcription_failed: {exc}", max_errors
                )
                if not keep_going:
                    session["stop_reason"] = "too_many_errors"
                    break
                await _conversation_cooldown(agent, session, turn, cooldown_s)
                continue
            turn["transcription_duration_s"] = round(time.time() - transcription_started, 3)
            turn["stt_confidence"] = getattr(transcription, "confidence", None)
            turn["stt_no_speech_probability"] = getattr(
                transcription, "no_speech_probability", None
            )
            turn["stt_language"] = getattr(transcription, "language", None)
            turn["stt_language_probability"] = getattr(transcription, "language_probability", None)
            turn["stt_retried"] = retried
            turn["transcript"] = _normalize_reachy_transcript(str(transcription.text or "").strip())
            if not _conversation_transcript_is_meaningful(turn["transcript"]):
                turn_index = max(0, turn_index - 1)
                session["turn_index"] = turn_index
                continue
            credible, credibility_reason = _conversation_transcription_is_credible(
                transcription, stt_payload
            )
            if not credible:
                await agent.log(
                    f"discarded low-confidence voice transcript: "
                    f"{credibility_reason}; text={turn['transcript']!r}",
                    level="info",
                )
                turn_index = max(0, turn_index - 1)
                session["turn_index"] = turn_index
                continue
            await _conversation_show_transcript(agent, session, turn["transcript"])
            if _conversation_stop_phrase(turn["transcript"]):
                session["stop_reason"] = "stop_phrase"
                break
            if _conversation_silence_phrase(turn["transcript"]):
                # Playback has already stopped by the time this is read. Asking
                # for silence must not be answered out loud: replying "Of
                # course, I'll be quiet!" is the thing being asked to stop.
                session["consecutive_errors"] = 0
                continue

            route_text = _conversation_route_text(turn["transcript"], session.get("history", []))
            await _conversation_publish(agent, session, "routing", turn)
            routing_started = time.time()

            async def _before_speak(reply) -> None:
                turn["routing_duration_s"] = round(time.time() - routing_started, 3)  # noqa: B023
                turn["response"] = str(reply or "").strip()  # noqa: B023
                await _conversation_publish(agent, session, "speaking", turn)  # noqa: B023
                if turn["response"] and not turn.get("_response_notified"):  # noqa: B023
                    await agent.notify_user(
                        turn["response"],  # noqa: B023
                        **{
                            "from": getattr(agent, "name", "reachy-mini"),
                            "to": "user",
                            "source": "voice",
                            "surface": getattr(agent, "name", "reachy-mini"),
                            "surface_label": "Reachy",
                            "brain": "main",
                            "session_id": session.get("session_id", ""),
                        },
                    )
                    turn["_response_notified"] = True  # noqa: B023

            try:
                task_id = f"{session['session_id']}:{turn_index}"
                speech_commands = _parse_speak_compound(turn["transcript"])
                if speech_commands:
                    bridged = await _conversation_speech_bridge(
                        agent, turn["transcript"], speech_commands, task_id, _before_speak, session
                    )
                else:
                    embodied = _embodied_command_for_text(turn["transcript"])
                if not speech_commands and embodied:
                    bridged = await _conversation_embodied_bridge(
                        agent, turn["transcript"], embodied, task_id, _before_speak, session
                    )
                elif not speech_commands:
                    bridged = await _bridge_to_main(
                        agent,
                        route_text,
                        task_id,
                        await_playback=True,
                        before_speak=_before_speak,
                        session_id=session["session_id"],
                        voice_friendly=payload.get("voice_friendly", True),
                        barge_in_session=session,
                        conversation_history=list(session.get("history", [])),
                        voice_input=True,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                turn["routing_duration_s"] = round(time.time() - routing_started, 3)
                keep_going = await _conversation_turn_error(
                    agent, session, turn, f"routing_failed: {exc}", max_errors
                )
                if not keep_going:
                    session["stop_reason"] = "too_many_errors"
                    break
                await _conversation_cooldown(agent, session, turn, cooldown_s)
                continue
            if bridged is None:
                turn["routing_duration_s"] = round(time.time() - routing_started, 3)
                keep_going = await _conversation_turn_error(
                    agent, session, turn, "routing_failed: no response", max_errors
                )
                if not keep_going:
                    session["stop_reason"] = "too_many_errors"
                    break
                await _conversation_cooldown(agent, session, turn, cooldown_s)
                continue
            if not turn["response"]:
                turn["response"] = str(bridged.get("result") or "").strip()
            turn["raw_response"] = str(
                bridged.get("raw_result") or bridged.get("result") or ""
            ).strip()
            if not bridged.get("spoke"):
                keep_going = await _conversation_turn_error(
                    agent,
                    session,
                    turn,
                    f"speech_failed: {bridged.get('speech_error') or 'not spoken'}",
                    max_errors,
                )
                if not keep_going:
                    session["stop_reason"] = "too_many_errors"
                    break
                await _conversation_cooldown(agent, session, turn, cooldown_s)
                continue

            session["consecutive_errors"] = 0
            turn["spoken_response"] = str(bridged.get("spoken_result") or "").strip()
            turn["interrupted"] = bool(bridged.get("interrupted"))
            heard_response = turn["spoken_response"] if turn["interrupted"] else turn["response"]
            session["history"].append(
                {
                    "transcript": turn["transcript"],
                    "response": heard_response,
                    "interrupted": turn["interrupted"],
                }
            )
            if turn["interrupted"]:
                session["barge_in_count"] += 1
                # The monitor retained the user's utterance. Start its STT/routing
                # immediately instead of flushing it during the normal cooldown.
                continue
            if max_turns and turn_index >= max_turns:
                session["stop_reason"] = "max_turns"
                break
            await _conversation_cooldown(agent, session, turn, cooldown_s)
    except asyncio.CancelledError:
        session["stop_reason"] = session.get("stop_reason") or "cancelled"
    except Exception as exc:
        session["stop_reason"] = "error"
        try:
            await _conversation_publish(
                agent, session, "error", turn, ok=False, error=exc, stop_reason="error"
            )
        except Exception:
            pass
    finally:
        reason = session.get("stop_reason") or "stopped"
        try:
            await _conversation_publish(
                agent,
                session,
                "stopped",
                turn,
                ok=reason not in ("error", "capture_failed", "too_many_errors"),
                error=(
                    reason if reason in ("error", "capture_failed", "too_many_errors") else None
                ),
                stop_reason=reason,
            )
            stopped_published = True
        except Exception:
            pass
        agent.state["conversation_state"] = "stopped"
        if agent.state.get("conversation_session") is session:
            agent.state["conversation_session"] = None
        if not stopped_published:
            agent.state["conversation_state"] = "error"


async def _doa(agent, payload: dict[str, Any]) -> dict[str, Any]:
    """Report the mic array's current direction of arrival.

    Angle in degrees plus whether a voice/source is presently detected.
    """
    media = _require_mic(agent, doa=True)
    doa = await _read_doa(agent, media)
    if not doa:
        return {"detected": False, "result": "No sound localized."}
    result = {"angle_deg": _doa_angle_deg(doa), "detected": True}
    if len(doa) > 1:
        result["voice_detected"] = bool(doa[1])
    v = result.get("voice_detected")
    vstr = "" if v is None else (", voice detected" if v else ", no voice")
    result["result"] = f"Sound from {result['angle_deg']:.0f}°{vstr}."
    return result


# ============================================================
# Reactive bindings
# ============================================================


async def _bind(agent, payload: dict[str, Any]) -> dict[str, Any]:
    """Add a reactive binding.

    payload = {
      'topic': 'home/state/light.living_room',
      'when':  {'new_state.state': 'on'},   # equality on dotted-path fields
      'do':    {'cmd': 'emotion', 'name': 'curious1'}
    }
    Persists across restarts. Re-bindings overwrite by topic+do.
    """
    topic = payload.get("topic")
    do = payload.get("do")
    when = payload.get("when") or {}
    if not topic or not isinstance(do, dict):
        raise ValueError("bind requires {'topic': str, 'when': dict, 'do': {cmd:..., ...}}")
    rules = agent.state["bindings"].setdefault(topic, [])
    rules.append({"when": when, "do": do})
    agent.persist("bindings", agent.state["bindings"])
    await _wire_binding(agent, topic)
    return {"bound": topic, "rules": len(rules)}


async def _unbind(agent, payload):
    """Remove all bindings for a topic (full unsubscribe is best-effort — the callback simply becomes a no-op)."""
    topic = payload.get("topic")
    if not topic:
        raise ValueError("unbind requires 'topic'")
    agent.state["bindings"].pop(topic, None)
    agent.persist("bindings", agent.state["bindings"])
    return {"unbound": topic}


async def _wire_binding(agent, topic_pattern):
    """Subscribe once per topic; the callback iterates rules and dispatches matches."""
    # Avoid double-wiring on restart — cheap idempotency via a set of wired topics
    wired = agent.state.setdefault("_wired", set())
    if topic_pattern in wired:
        return
    wired.add(topic_pattern)

    async def on_event(payload):
        rules = agent.state.get("bindings", {}).get(topic_pattern, [])
        for rule in rules:
            if _match(payload, rule.get("when") or {}):
                action = dict(rule["do"])
                cmd = action.pop("cmd", None)
                if cmd:
                    await _dispatch(agent, cmd, action)

    agent.subscribe(topic_pattern, on_event)


def _match(payload, when):
    """Equality match on dotted-path fields.  when={'new_state.state': 'on'}."""
    if not isinstance(payload, dict):
        return False
    for path, expected in when.items():
        cur = payload
        for part in str(path).split("."):
            if not isinstance(cur, dict) or part not in cur:
                return False
            cur = cur[part]
        if cur != expected:
            return False
    return True


'''
