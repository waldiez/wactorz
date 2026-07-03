"""CATALOG RECIPE — reachy-body-agent
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Drives a Reachy Mini robot (Wireless over WiFi, or Lite over USB) as an
embodied output channel for the wactorz fleet. Subscribes to:

    custom/reachy/cmd            — structured commands (one verb per payload)
    custom/reachy/cmd/<verb>     — per-verb shortcut topics
    custom/reachy/config         — runtime config (currently: robot_host)
    <bound topics>               — dynamically bound via cmd=bind

Publishes:

    custom/reachy/state          — retained: awake, busy, robot_host, bindings
    custom/reachy/events         — per-command success/failure
    custom/reachy/cmd_result/<id>— per-correlated-command ack

Reactive bindings are persisted so they survive /agents restart.

DEPENDENCIES
────────────
Python:
    reachy-mini    → Pollen Robotics SDK (and its transitive deps)
    numpy

Outside wactorz:
    Wireless: robot powered on, same WiFi as the wactorz host, no client
              isolation on the network (test with `ping reachy-mini.local`).
    Lite:     daemon running locally — `reachy-mini-daemon -p <serial_port>`.

    NO HF App may be running on the robot — Apps take exclusive control.

SPAWN
─────
    @catalog spawn reachy-body
    /agents                    → confirms reachy-body is running
    /agents restart reachy-body → re-runs setup() if connection dropped

PINNING THE HOST (Wireless only — optional)
───────────────────────────────────────────
The SDK autodetects Lite vs Wireless and chooses the right transport. If you
have multiple robots on the LAN, or mDNS is flaky, pin a host once:

    publish to:  custom/reachy/config
    payload:     {"robot_host": "192.168.1.42"}

The agent persists it; restart picks it up. Current host is published to
custom/reachy/state under `robot_host`.

TASK PAYLOAD EXAMPLES
─────────────────────
Wake / sleep:
    {"cmd": "wake"}      {"cmd": "sleep"}

Head pose (yaw 30°, smooth):
    {"cmd": "pose", "yaw": 30, "duration": 0.6, "method": "minjerk"}

Antennas (degrees, default):
    {"cmd": "antennas", "left": 45, "right": -45, "duration": 0.3}

Look at a world point:
    {"cmd": "look_at", "x": 0.5, "y": 0.0, "z": 0.2, "duration": 1.0}

Play recorded emotion:
    {"cmd": "emotion", "name": "curious1"}

Reactive binding — robot looks curious when living-room lamp turns on:
    {"cmd": "bind",
     "topic": "home/state/light.living_room",
     "when":  {"new_state.state": "on"},
     "do":    {"cmd": "emotion", "name": "curious1"}}

Unbind:
    {"cmd": "unbind", "topic": "home/state/light.living_room"}

For full payload shapes, see reachy-body.README.md alongside this file.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

# ──────────────────────────────────────────────────────────────────────────────
# AGENT_CODE — loaded by CatalogAgent._load_recipe()
# ──────────────────────────────────────────────────────────────────────────────

AGENT_CODE = r'''
import asyncio
import time as _time

async def _do(fn, *args, **kwargs):
    """Run a blocking SDK call in the default executor so the actor loop stays free."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


async def setup(agent):
    # ---- Heavy imports inside setup (never at module level) ----
    import numpy as np
    from reachy_mini import ReachyMini
    from reachy_mini.utils import create_head_pose

    # ---- Resolve robot host (Wireless on LAN OR Lite via USB) ----
    # Priority: persisted robot_host  >  agent.recall fallback  >  autodetect.
    # The user can pin it any of these ways:
    #   1. add "robot_host": "reachy-mini.local" (or IP) to the spawn config
    #      and call: agent.persist("robot_host", "reachy-mini.local") from a setup hook
    #   2. publish once to custom/reachy/config: {"robot_host": "192.168.1.42"}
    #   3. leave blank — SDK autodetect chooses Lite (localhost) vs Wireless (LAN).
    robot_host = agent.recall("robot_host") or ""
    agent.state["robot_host"] = robot_host

    # Live update channel for the host (no restart needed)
    async def on_config(payload):
        if not isinstance(payload, dict):
            return
        if "robot_host" in payload:
            h = payload["robot_host"]
            agent.persist("robot_host", h)
            agent.state["robot_host"] = h
            await agent.log(f"robot_host updated to {h} — restart agent to reconnect")
        if "media_backend" in payload:
            m = payload["media_backend"]
            agent.persist("media_backend", m)
            agent.state["media_backend"] = m
            await agent.log(f"media_backend updated to {m} — restart agent to apply")
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
    import os as _os
    media_backend = (agent.recall("media_backend")
                     or _os.environ.get("REACHY_MEDIA_BACKEND") or "").strip()
    agent.state["media_backend"] = media_backend

    mini = None
    last_err = None
    attempts = []
    base = {"media_backend": media_backend} if media_backend else {}
    # Connection: keep auto-detect first (the desktop app's local daemon is the
    # bridge that actually reaches the robot, same path the dashboard uses), with
    # a forced-network fallback. The backend choice above — not the connection
    # mode — is what routes audio to the robot.
    if robot_host:
        attempts.append({**base, "host": robot_host})                                 # explicit host pin
        attempts.append({**base, "host": robot_host, "connection_mode": "network"})
    attempts.append({**base})                                                          # autodetect (default)
    attempts.append({**base, "connection_mode": "network"})                            # force network mode

    # IMPORTANT: ReachyMini().__enter__() does blocking websocket / media handshakes
    # that take 5–10 seconds. Running it directly here freezes the asyncio event
    # loop long enough for MQTT keepalives to time out and the monitor server
    # (port 8887) to fail to bind. Always run it in an executor.
    loop = asyncio.get_event_loop()
    def _open_sync(kw):
        return ReachyMini(**kw).__enter__()
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

    # Note: numpy + create_head_pose are stored regardless — they're pure helpers
    # that the LLM-planning path doesn't actually need a live robot to use.
    agent.state["np"] = np
    agent.state["create_head_pose"] = create_head_pose
    agent.state["last_connect_error"] = str(last_err) if mini is None else None

    if mini is None:
        # Stay alive in a "disconnected" mode so HA-only commands keep working
        # and so users get a friendly "reachy not connected" instead of a crash
        # loop. Setup() must NOT raise — the supervisor would auto-restart us.
        agent.state["mini"] = None
        await agent.log(
            f"Reachy daemon unreachable. Agent will stay up and refuse robot "
            f"commands with 'reachy not connected'. Last error: {last_err}",
            level="warning",
        )
    else:
        agent.state["mini"] = mini
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
                    "custom/reachy/config and restart.", level="warning")
        except Exception:
            pass

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
        from reachy_mini.motion.recorded_move import RecordedMoves
        moves = RecordedMoves("pollen-robotics/reachy-mini-emotions-library")
        agent.state["moves"] = moves
        # Best-effort list — the lib usually exposes .available()/.list()/dict-like access
        names = []
        for attr in ("available", "list", "keys"):
            f = getattr(moves, attr, None)
            if callable(f):
                try:
                    names = list(f())
                    break
                except Exception:
                    continue
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

    # ---- Reactive bindings ----
    # bindings :: { topic_pattern: { 'when': {field: value}, 'do': {cmd, payload} } }
    # Loaded from persistent state so they survive restarts.
    agent.state["bindings"] = agent.recall("bindings") or {}

    # ---- Robot speaker volume (0-100, daemon-native; set via cmd=volume) ----
    # The daemon persists its own volume, so sync FROM it at startup (a GET, no
    # test sound) rather than re-applying our value. Fall back to persisted.
    agent.state["muted"] = bool(agent.recall("muted"))
    agent.state["premute_level"] = int(agent.recall("premute_level") or 100)
    live = await _get_daemon_volume(agent) if mini is not None else None
    if live is not None:
        agent.state["volume_level"] = live
        await agent.log(f"Robot speaker volume is {live}/100 (from daemon).")
    else:
        agent.state["volume_level"] = int(agent.recall("volume_level") or 100)

    # ---- Wake up so the robot is ready for commands (skip if disconnected) ----
    if mini is not None:
        try:
            await _do(mini.wake_up)
            agent.state["awake"] = True
            await agent.publish("custom/reachy/events", {"type": "wake", "ts": _time.time()})
        except Exception as e:
            await agent.alert(f"wake_up failed: {e}", severity="warning")

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
    for verb in ("wake", "sleep", "pose", "antennas", "look_at",
                 "look_pixel", "emotion", "set_pose", "bind",
                 "unbind", "list_emotions", "stop", "say", "volume"):
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
    await agent.publish("custom/reachy/state", {
        "awake":   agent.state["awake"],
        "busy":    False,
        "emotions": agent.state["emotion_names"],
        "bindings": list(agent.state["bindings"].keys()),
        "robot_host":    agent.state.get("robot_host") or "(autodetect)",
        "media_backend": agent.state.get("media_backend") or "default",
        "volume_level":  agent.state.get("volume_level", 100),
        "muted":         bool(agent.state.get("muted")),
        "ts":            _time.time(),
    })

    await agent.log("reachy-body ready")


async def process(agent):
    # Periodic heartbeat: republish the current state (retained) so dashboards
    # always show a fresh value. All actual work is callback-driven via subscribe().
    connected, reason = _is_connected(agent)
    await agent.publish("custom/reachy/state", {
        "connected": connected,
        "reason":    reason,
        "awake":   agent.state.get("awake", False),
        "busy":    agent.state.get("busy", False),
        "last_cmd": agent.state.get("last_cmd"),
        "emotions": agent.state.get("emotion_names", []),
        "bindings": list(agent.state.get("bindings", {}).keys()),
        "robot_host":    agent.state.get("robot_host") or "(autodetect)",
        "media_backend": agent.state.get("media_backend") or "default",
        "volume_level":  agent.state.get("volume_level", 100),
        "muted":         bool(agent.state.get("muted")),
        "ts":            _time.time(),
    })


_NL_SYSTEM = """You drive a Reachy Mini robot AND a Home Assistant smart home.
Convert the user's instruction to a JSON array of commands, executed in order.

Robot commands:
  {"cmd":"wake"}
  {"cmd":"sleep"}
  {"cmd":"stop"}
  {"cmd":"pose","yaw":<deg>,"pitch":<deg>,"roll":<deg>,"duration":<sec>}
  {"cmd":"antennas","left":<deg>,"right":<deg>,"duration":<sec>}
  {"cmd":"look_at","x":<m>,"y":<m>,"z":<m>,"duration":<sec>}
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

Home Assistant — DELEGATED to the home-assistant-agent (you NEVER call HA directly):
  {"cmd":"ha","request":"<the smart-home request in plain natural language>"}
Use this for ANY smart-home thing — lights, switches, plugs, climate, scenes, sensors —
AND for creating / editing / listing / deleting Home Assistant AUTOMATIONS. Put the user's
full intent in 'request' (keep the room/device names they said). The home-assistant-agent
resolves the real entities and automations itself, so you do NOT pass entity_ids or HA
service names. Examples of 'request' values: "turn on the kitchen light",
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
  {"cmd":"ha","request":"..."} that forwards the user's words to the home-assistant-agent.
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

Examples (pick the entity_id from the "Live HA inventory" section below — these
examples use <LIGHT> / <SWITCH> as placeholders, substitute the real id):
User: "turn on the light"
  → [{"cmd":"ha","service":"light.turn_on","entity_id":"<LIGHT>"}]

User: "turn off the lamp"
  → [{"cmd":"ha","service":"light.turn_off","entity_id":"<LIGHT>"}]

User: "wake up and turn on the light"
  → [{"cmd":"wake"},
     {"cmd":"ha","service":"light.turn_on","entity_id":"<LIGHT>"}]

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


def _parse_speak_compound(text):
    """Deterministically handle 'whisper X' / 'say X softly|loudly' so they NEVER
    get answered as chat. Returns a [volume-preset, say] command list, or None.

    'whisper hello' means: set the whisper speaker level, then SPEAK 'hello' aloud
    on the robot — not roleplay a whisper in text. Bare 'whisper'/'whisper mode'
    are already caught by the exact-match shortcuts before we get here."""
    import re
    t = (text or "").strip()
    # Skip preset-keyword-only tails like "whisper softly" → would say "softly".
    _STOP = {"mode", "softly", "quietly", "loudly", "back", "again", "now"}
    patterns = (
        (r"^whisper(?:\s+that)?\s+(.+)$",                                          "whisper"),
        (r"^(?:say|announce|tell\s+them)\s+(.+?)\s+(?:softly|quietly|in\s+a\s+whisper)$", "whisper"),
        (r"^(?:say|announce|tell\s+them)\s+(.+?)\s+(?:loudly|to\s+the\s+audience|for\s+everyone|for\s+the\s+room)$", "presenter"),
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


async def _nl_to_commands(agent, text):
    import json as _json
    if agent.llm is None:
        return None
    # Light/switch inventory — ONLY used to fill entity_ids in reactive robot-binds
    # (HA control/automation is delegated as natural language, no ids needed). Fetched
    # lazily from the home-assistant-agent and cached; retried at most once a minute
    # while empty so a not-yet-ready HA agent eventually populates it.
    ents = agent.state.get("ha_entities") or {}
    have = ents.get("lights") or ents.get("switches")
    if not have and (_time.time() - agent.state.get("ha_entities_ts", 0.0)) > 60:
        ents = await _ha_entities_via_agent(agent)
        agent.state["ha_entities"] = ents
        agent.state["ha_entities_ts"] = _time.time()
    lines = []
    for kind, items in (("Lights", ents.get("lights", [])), ("Switches", ents.get("switches", []))):
        if not items:
            continue
        lines.append(f"\n{kind} (entity_id for binds):")
        for it in items:
            lines.append(f"  {it['entity_id']:50s}  ({it['name']})")
    ha_section = ("\n".join(lines) if lines
                  else "\n(no entity inventory yet — for binds, use the device name the user gave)")
    # Inject the current speaker volume so the LLM can do relative ("a bit louder")
    # and mute/unmute requests correctly.
    cur_level = agent.state.get("volume_level", 100)
    muted = bool(agent.state.get("muted"))
    vol_section = (f"\n\nCurrent speaker volume: level {cur_level} (0-100), "
                   f"muted={'yes' if muted else 'no'}.")
    system_with_ents = _NL_SYSTEM + "\n\nEntity inventory (for binds only):" + ha_section + vol_section
    raw = await agent.llm.chat(text, system=system_with_ents)
    raw = (raw or "").strip()
    if raw.startswith("```"):
        # Strip ```json ... ``` fences
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
            if raw.startswith("json"):
                raw = raw[4:]
    raw = raw.strip()
    # Try direct parse, else find the first JSON array in the response
    try:
        cmds = _json.loads(raw)
    except Exception:
        i = raw.find("[")
        j = raw.rfind("]")
        if i >= 0 and j > i:
            try:
                cmds = _json.loads(raw[i:j+1])
            except Exception:
                return None
        else:
            return None
    if isinstance(cmds, list):
        return cmds
    if isinstance(cmds, dict):
        return [cmds]
    return None


async def handle_task(agent, payload):
    # Direct send_to(reachy-body, {...}) — same dispatch as MQTT.
    # IOAgent wraps user text as: {"text": "...", "_task_id": ..., "reply_to": ...}
    payload = payload or {}
    _tid = payload.get("_task_id") or payload.get("task")

    if "cmd" not in payload and "action" not in payload:
        # Pull NL text from any common field. Planner-generated agents often send
        # dicts like {"gesture":"shrug","description":"do a shrug"} — we accept
        # anything that looks like a description so the NL planner can interpret it.
        text = (payload.get("text") or payload.get("content") or payload.get("message")
                or payload.get("query") or payload.get("description")
                or payload.get("gesture") or payload.get("instruction"))
        # If we have a dict with multiple text-ish fields, glue them — gives the
        # LLM more context to decide what gesture was meant.
        if not text and isinstance(payload, dict):
            text_bits = [str(v) for k, v in payload.items()
                         if k not in ("_task_id", "_reply_to", "task", "command", "name", "context", "source")
                         and isinstance(v, str)]
            if text_bits:
                text = " ".join(text_bits)
        if isinstance(text, str):
            stripped = text.strip()
            # Structured JSON object: if it has cmd/action, adopt as the new payload.
            # If it's a payload-dict without cmd (e.g. planner-generated {gesture,
            # description, context}), flatten its text-ish fields back to NL input
            # so the LLM has something natural to work with.
            if stripped.startswith("{") and stripped.endswith("}"):
                import json as _json
                try:
                    parsed = _json.loads(stripped)
                    if isinstance(parsed, dict):
                        if "cmd" in parsed or "action" in parsed:
                            payload = parsed
                        else:
                            # Re-extract NL text from the inner dict
                            inner = (parsed.get("description") or parsed.get("text")
                                     or parsed.get("instruction") or parsed.get("gesture")
                                     or parsed.get("message") or parsed.get("query"))
                            if not inner:
                                inner = " ".join(str(v) for k, v in parsed.items()
                                                 if isinstance(v, str)
                                                 and k not in ("_task_id", "_reply_to", "task",
                                                               "command", "source", "context"))
                            stripped = inner.strip() if isinstance(inner, str) else stripped
                except Exception:
                    pass
            if "cmd" not in payload and "action" not in payload and stripped:
                low = stripped.lower().rstrip("!.")
                # Single-verb shortcuts (no LLM call needed)
                if   low in ("wake", "wake up"):           payload = {"cmd": "wake"}
                elif low in ("sleep", "go to sleep"):      payload = {"cmd": "sleep"}
                elif low in ("stop",):                     payload = {"cmd": "stop"}
                elif low in ("list emotions", "emotions"): payload = {"cmd": "list_emotions"}
                # Common volume phrases — handled without an LLM round-trip.
                elif low in ("mute", "silence", "be quiet", "quiet", "shut up", "stop talking"):
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
                elif low in ("presenter mode", "presentation mode", "presentation",
                             "audience", "audience mode", "fill the room"):
                    payload = {"cmd": "volume", "preset": "presenter"}
                else:
                    # Deterministic 'whisper X' / 'say X softly|loudly' first so
                    # they actually drive the robot (volume + speak) instead of the
                    # LLM occasionally answering them as chat/roleplay. Falls back to
                    # the NL planner for everything else (60s budget — an LLM call +
                    # a few short motions fits comfortably).
                    cmds = _parse_speak_compound(stripped) or await _nl_to_commands(agent, stripped)
                    if not cmds:
                        # Return a clear 'result' message — NOT the raw input under
                        # 'text', or the io-agent's reply picker (reply→result→text)
                        # echoes the user's own words back at them.
                        return {"ok": False, "error": "could not parse instruction",
                                "result": (f"I couldn't turn \"{stripped}\" into a robot action. "
                                           "Try something like \"wake\", \"say hello\", "
                                           "\"whisper hi\", or \"presenter mode\"."),
                                "_task_id": _tid, "task": _tid}
                    # If reachy is offline, skip robot commands but still run HA ones.
                    ok_link, link_reason = _is_connected(agent)
                    steps = []
                    skipped = []
                    for c in cmds:
                        if not isinstance(c, dict):
                            continue
                        c_cmd = c.get("cmd") or c.get("action")
                        if not ok_link and c_cmd not in ("ha", "list_emotions"):
                            skipped.append(c_cmd)
                            continue
                        r = await _dispatch(agent, c_cmd, c, return_result=True)
                        steps.append(r)
                    # Build a human-readable summary of what actually ran
                    summary_parts = []
                    for c in cmds:
                        if not isinstance(c, dict):
                            continue
                        cc = c.get("cmd") or c.get("action") or "?"
                        if cc == "ha":
                            summary_parts.append(f"ha:{c.get('service','?')}")
                        elif cc == "pose":
                            summary_parts.append(f"pose(y={c.get('yaw',0)},p={c.get('pitch',0)})")
                        elif cc == "antennas":
                            summary_parts.append(f"antennas(l={c.get('left','?')},r={c.get('right','?')})")
                        else:
                            summary_parts.append(cc)
                    result_msg = f"ran {len(steps)} of {len(cmds)}: [{' → '.join(summary_parts)}]"
                    if skipped:
                        result_msg += f"  (skipped {len(skipped)}: {link_reason})"
                    return {"ok": True, "cmd": "nl", "steps_run": len(steps),
                            "skipped": skipped, "plan": cmds, "result": result_msg,
                            "_task_id": _tid, "task": _tid}
    cmd = payload.get("cmd") or payload.get("action")
    # Friendly fail-fast for ROBOT commands when reachy isn't connected.
    # HA-only commands ("ha") still work — that's the whole point of the
    # "stay alive in disconnected mode" design.
    if cmd not in ("ha", "list_emotions", None):
        ok, reason = _is_connected(agent)
        if not ok:
            return {"ok": False, "error": reason, "result": reason,
                    "cmd": cmd, "_task_id": _tid, "task": _tid}
    result = await _dispatch(agent, cmd, payload, return_result=True)
    if isinstance(result, dict):
        result.setdefault("_task_id", _tid)
        result.setdefault("task", _tid)
    return result


async def cleanup(agent):
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


def _is_connected(agent):
    """Quick check that the SDK handle is alive. Returns (ok, reason)."""
    mini = agent.state.get("mini")
    if mini is None:
        return False, "reachy not connected (no SDK handle)"
    # The SDK exposes _connected / _is_connected / ws on various versions — be tolerant.
    for attr in ("_connected", "is_connected", "connected"):
        v = getattr(mini, attr, None)
        if callable(v):
            try:
                if not v():
                    return False, "reachy not connected (daemon link dropped)"
            except Exception:
                pass
        elif v is False:
            return False, "reachy not connected (daemon link dropped)"
    return True, None


# ============================================================
# Dispatcher — the single place every command flows through.
# ============================================================

async def _dispatch(agent, cmd, payload, return_result=False):
    if not cmd:
        if return_result:
            return {"ok": False, "cmd": None, "error": "missing cmd field"}
        return
    cmd = str(cmd).lower().strip()
    agent.state["last_cmd"] = cmd
    started = _time.time()

    try:
        if   cmd == "wake":          result = await _wake(agent)
        elif cmd == "sleep":         result = await _sleep(agent)
        elif cmd == "pose":          result = await _pose(agent, payload)
        elif cmd == "antennas":      result = await _antennas(agent, payload)
        elif cmd == "look_at":       result = await _look_at(agent, payload)
        elif cmd == "look_pixel":    result = await _look_pixel(agent, payload)
        elif cmd == "emotion":       result = await _emotion(agent, payload)
        elif cmd == "set_pose":      result = await _set_pose(agent, payload)
        elif cmd == "bind":          result = await _bind(agent, payload)
        elif cmd == "unbind":        result = await _unbind(agent, payload)
        elif cmd == "list_emotions": result = {"emotions": agent.state.get("emotion_names", [])}
        elif cmd == "stop":          result = await _stop(agent)
        elif cmd == "say":           result = await _say(agent, payload)
        elif cmd == "volume":        result = await _volume(agent, payload)
        elif cmd == "ha":            result = await _ha(agent, payload)
        else:
            raise ValueError(f"unknown cmd: {cmd}")

        ack = {"ok": True, "cmd": cmd, "duration_s": round(_time.time() - started, 3)}
        if isinstance(result, dict):
            ack.update(result)
        # Per-command result — correlation id if provided
        rid = payload.get("id")
        if rid:
            await agent.publish(f"custom/reachy/cmd_result/{rid}", ack)
        await agent.publish("custom/reachy/events", {"type": cmd, "ok": True, "ts": _time.time()})
        if return_result:
            return ack
    except Exception as e:
        err = {"ok": False, "cmd": cmd, "error": str(e), "duration_s": round(_time.time() - started, 3)}
        rid = payload.get("id")
        if rid:
            await agent.publish(f"custom/reachy/cmd_result/{rid}", err)
        await agent.publish("custom/reachy/events", {"type": cmd, "ok": False, "error": str(e), "ts": _time.time()})
        await agent.log(f"cmd '{cmd}' failed: {e}", level="error")
        if return_result:
            return err


# ============================================================
# Command implementations
# ============================================================

async def _wake(agent):
    mini = agent.state["mini"]
    async with agent.state["motion_lock"]:
        agent.state["busy"] = True
        try:
            await _do(mini.wake_up)
            agent.state["awake"] = True
        finally:
            agent.state["busy"] = False
    return {}


async def _sleep(agent):
    """Animated 'sleep' — head droops, antennas fall. We DO NOT call mini.goto_sleep()
    because that disables motors and often loses the daemon connection, requiring a
    full agent restart. This is purely cosmetic."""
    mini = agent.state["mini"]
    np   = agent.state["np"]
    create_head_pose = agent.state["create_head_pose"]
    async with agent.state["motion_lock"]:
        agent.state["busy"] = True
        try:
            # Head droops down slowly
            await _do(mini.goto_target,
                      head=create_head_pose(pitch=25, degrees=True),
                      antennas=np.deg2rad([-30, -30]),
                      duration=1.5)
            agent.state["awake"] = False
        finally:
            agent.state["busy"] = False
    return {"animated": True}


async def _pose(agent, payload):
    """Interpolated head pose (and optional antennas + body_yaw)."""
    mini = agent.state["mini"]
    np   = agent.state["np"]
    create_head_pose = agent.state["create_head_pose"]

    head_pose = create_head_pose(
        x     = float(payload.get("x",     0)),
        y     = float(payload.get("y",     0)),
        z     = float(payload.get("z",     0)),
        roll  = float(payload.get("roll",  0)),
        pitch = float(payload.get("pitch", 0)),
        yaw   = float(payload.get("yaw",   0)),
        mm      = bool(payload.get("mm",      True)),
        degrees = bool(payload.get("degrees", True)),
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
        kw["body_yaw"] = float(np.deg2rad(by)) if payload.get("body_yaw_degrees", True) else by
    method = payload.get("method")
    if method:
        kw["method"] = str(method)

    async with agent.state["motion_lock"]:
        agent.state["busy"] = True
        try:
            await _do(mini.goto_target, **kw)
        finally:
            agent.state["busy"] = False
    return {}


async def _antennas(agent, payload):
    """Antennas-only motion. Accepts left/right (degrees by default) or angles list."""
    mini = agent.state["mini"]
    np   = agent.state["np"]
    if "angles" in payload:
        angles = payload["angles"]
    else:
        # left/right convention — confirm against your robot orientation
        left  = float(payload.get("left",  0))
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


async def _look_pixel(agent, payload):
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
    np   = agent.state["np"]
    create_head_pose = agent.state["create_head_pose"]
    kw = {}
    if any(k in payload for k in ("x","y","z","roll","pitch","yaw")):
        kw["head"] = create_head_pose(
            x=float(payload.get("x",0)), y=float(payload.get("y",0)), z=float(payload.get("z",0)),
            roll=float(payload.get("roll",0)), pitch=float(payload.get("pitch",0)), yaw=float(payload.get("yaw",0)),
            mm=bool(payload.get("mm", True)), degrees=bool(payload.get("degrees", True)),
        )
    if "antennas" in payload:
        a = payload["antennas"]
        kw["antennas"] = np.deg2rad(a) if payload.get("antennas_degrees", True) else np.array(a, dtype=float)
    if "body_yaw" in payload:
        by = float(payload["body_yaw"])
        kw["body_yaw"] = float(np.deg2rad(by)) if payload.get("body_yaw_degrees", True) else by
    await _do(mini.set_target, **kw)
    return {}


async def _stop(agent):
    """Best-effort motion abort — not all SDK versions have a stop primitive, so we re-target current pose."""
    mini = agent.state["mini"]
    # If the SDK exposes a stop, use it.
    fn = getattr(mini, "stop", None) or getattr(mini, "cancel", None)
    if fn:
        await _do(fn)
        return {"stopped": True}
    # Fallback: short re-target to current pose with very short duration.
    create_head_pose = agent.state["create_head_pose"]
    await _do(mini.goto_target, head=create_head_pose(), duration=0.1)
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
        if 0x0370 <= cp <= 0x03FF or 0x1F00 <= cp <= 0x1FFF:   # Greek + Greek Extended
            el += 1
        elif 0x0041 <= cp <= 0x024F:                            # Latin + Latin Extended-A/B
            latin += 1
    # Greek dominates and the default isn't already a Greek voice -> use one.
    if el > 0 and el >= latin and not default_voice.lower().startswith("el-"):
        return "el-GR-AthinaNeural"
    return default_voice


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
    import os, tempfile, uuid
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
            '{"media_backend": ""} to custom/reachy/config and restart the agent.'
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

    # Voice precedence: explicit payload voice > script auto-detect > configured default.
    # Auto-detect matters because an English voice fed literal Greek text spells out
    # the Unicode letter names ("kappa alpha lambda...") instead of pronouncing the
    # word. Picking a voice that matches the text's script fixes that.
    default_voice = (agent.state.get("tts_voice")
                     or os.environ.get("TTS_VOICE", "en-US-JennyNeural"))
    voice = payload.get("voice") or _voice_for_text(text, default_voice)

    # -- Synthesize to a temp MP3 file (path-based, GStreamer playbin decodes it) --
    try:
        import edge_tts
    except ImportError:
        raise RuntimeError("edge-tts not installed — pip install edge-tts")

    raw_path = os.path.join(tempfile.gettempdir(), f"reachy_say_{uuid.uuid4().hex}.mp3")
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(raw_path)

    # -- Loudness boost (default on) --------------------------------------------
    # The boost compresses + limits the TTS file to the digital ceiling (loudest
    # the file can be). Robot speaker loudness on top of that is the persistent
    # volume (daemon gain, set via cmd=volume). per-call gain_db (<=0) trims this
    # one utterance quieter at the file level without touching the volume setting.
    loud = payload.get("loud", True)
    trim_db = min(0.0, float(payload.get("gain_db", 0)))
    play_path = raw_path
    if loud:
        boosted = await _boost_audio(agent, raw_path, trim_db)
        if boosted:
            try:
                os.unlink(raw_path)   # raw is consumed by the boost step
            except Exception:
                pass
            play_path = boosted

    # -- Play through the robot's speaker (non-blocking GStreamer playbin) --
    await _do(media.play_sound, play_path)

    # Clean up the previous utterance now that a new one is playing.
    prev = agent.state.get("_say_tmp")
    if prev and prev != play_path:
        try:
            os.unlink(prev)
        except Exception:
            pass
    agent.state["_say_tmp"] = play_path

    return {"said": text, "voice": voice, "trim_db": trim_db,
            "volume_level": agent.state.get("volume_level", 100),
            "boosted": play_path != raw_path,
            "on_robot": plays_on_robot,
            "output": "robot" if plays_on_robot else "host (set media_backend=webrtc)"}


async def _boost_audio(agent, src_path, attenuation_db=0.0):
    """Compress + limit speech to the loudest clean level via ffmpeg.

    Returns the path to a new boosted MP3, or None if ffmpeg is unavailable or
    fails (caller falls back to the raw file). Raw edge-tts is ~-22 dB mean;
    the chain brings it to ~-11 dB at the digital ceiling (roughly 3-4x
    perceived loudness) — that's the maximum. attenuation_db (<=0) dials the
    final level DOWN from there for quieter playback.
    """
    import os, shutil, subprocess, tempfile, uuid
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        await agent.log("ffmpeg not found — playing raw (quieter) TTS", level="warning")
        return None
    af = "acompressor=threshold=-20dB:ratio=9:attack=5:release=50:makeup=10,alimiter=limit=0.97"
    attenuation_db = min(0.0, float(attenuation_db))
    if attenuation_db < 0:
        af += f",volume={attenuation_db:.1f}dB"
    out_path = os.path.join(tempfile.gettempdir(), f"reachy_say_{uuid.uuid4().hex}.mp3")

    def _run():
        return subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src_path,
             "-af", af, out_path],
            capture_output=True, text=True,
        )
    try:
        proc = await _do(_run)
    except Exception as e:
        await agent.log(f"ffmpeg boost failed ({e}) — playing raw TTS", level="warning")
        return None
    if proc.returncode != 0 or not os.path.exists(out_path):
        await agent.log(f"ffmpeg boost error — playing raw TTS: {proc.stderr[:200]}", level="warning")
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
    "whisper":   70,   # soft but reliably audible (below ~65 is wasted)
    "normal":    85,   # clear conversational, one-on-one
    "louder":    93,   # room-filling / small group
    "presenter": 100,  # presentation / audience / shout — the ceiling
}


def _level_to_db(level):
    """Map a 0-100 volume level to the daemon gain range (-20..+24 dB)."""
    level = max(0.0, min(100.0, float(level)))
    return _ROBOT_GAIN_MIN_DB + level / 100.0 * (_ROBOT_GAIN_MAX_DB - _ROBOT_GAIN_MIN_DB)


def _db_to_level(db):
    """Inverse of _level_to_db: dB -> 0-100 level."""
    return round((float(db) - _ROBOT_GAIN_MIN_DB)
                 / (_ROBOT_GAIN_MAX_DB - _ROBOT_GAIN_MIN_DB) * 100)


def _daemon_url(agent):
    """Base URL of the robot daemon's HTTP API, if reachable (WebRTC backend only)."""
    mini = agent.state.get("mini")
    media = getattr(mini, "media", None) or getattr(mini, "media_manager", None)
    audio = getattr(media, "audio", None)
    return (getattr(audio, "daemon_url", None) or getattr(media, "_daemon_url", None)
            or getattr(mini, "_daemon_http_url", None))


async def _get_daemon_volume(agent):
    """GET the robot speaker volume (0-100) from the daemon, or None if unavailable."""
    import aiohttp
    url = _daemon_url(agent)
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{url.rstrip('/')}/api/volume/current",
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
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
        return False, ('no daemon URL — robot volume needs the WebRTC backend. '
                       'Set REACHY_MEDIA_BACKEND=webrtc (or publish media_backend) and restart.')
    base = url.rstrip("/")
    level = int(max(0, min(100, round(level))))
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{base}/api/volume/set", json={"volume": level},
                              timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status < 400:
                    return True, f"volume/set={level}"
                if r.status != 404:
                    body = await r.text()
                    return False, f"volume/set {r.status}: {body[:160]}"
            # Older/newer daemon without /api/volume — try the PR #1187 gain endpoint.
            async with s.post(f"{base}/api/audio/gain", json={"gain_db": _level_to_db(level)},
                              timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status < 400:
                    return True, f"audio/gain={_level_to_db(level):.1f}dB"
                if r.status == 404:
                    return False, ("daemon exposes neither /api/volume/set nor /api/audio/gain — "
                                   "use the dashboard SPEAKER slider.")
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
            agent.state["premute_level"] = cur          # remember to restore later
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
            raise ValueError(f"unknown volume preset {preset!r}; "
                             f"use one of {', '.join(_VOLUME_PRESETS)}")
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
    if mute is None:                                    # any explicit set clears mute
        agent.state["muted"] = False
        agent.persist("muted", False)
    agent.state["volume_level"] = level
    agent.persist("volume_level", level)

    ok, detail = await _apply_volume(agent, level)
    muted = bool(agent.state.get("muted"))
    # Always print volume changes to the CLI for debug/visibility.
    src = ("mute" if mute is True else "unmute" if mute is False
           else f"preset:{str(preset).lower()}" if preset is not None
           else "delta" if "delta" in payload else "level" if "level" in payload else "db")
    if ok:
        await agent.log(f"🔊 volume -> {level}/100 (muted={muted}) via {src} [{detail}]")
    else:
        await agent.log(f"🔊 volume -> {level}/100 (muted={muted}) via {src} NOT applied: {detail}",
                        level="warning")
    result = {"level": level, "muted": muted, "applied": ok}
    if not ok:
        result["reason"] = detail
    return result


async def _ha_entities_via_agent(agent):
    """Light/switch inventory obtained FROM the home-assistant-agent — no direct HA
    REST. Only needed so the planner can fill real entity_ids into reachy-side
    reactive binds ('when the living-room light turns on, wake up'). Best-effort:
    returns {'lights': [...], 'switches': [...]}, empty on any failure."""
    try:
        res = await agent.send_to("home-assistant-agent",
                                  {"text": "list all entities"}, timeout=20.0)
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
        eid  = str(r.get("entity_id", ""))
        name = r.get("name") or eid
        if eid.startswith("light."):
            lights.append({"entity_id": eid, "name": name})
        elif eid.startswith("switch."):
            switches.append({"entity_id": eid, "name": name})
    await agent.log(f"HA inventory via agent: {len(lights)} light(s), {len(switches)} switch(es)")
    return {"lights": lights, "switches": switches}


async def _ha(agent, payload):
    """Delegate any Home Assistant request to the home-assistant-agent.

    reachy NO LONGER talks to HA's REST API directly. The home-assistant-agent owns
    all HA logic — it resolves entities, controls devices, and creates/edits/lists/
    deletes automations from a natural-language request. We just forward the ask and
    relay its answer.

    Accepts (first non-empty wins):
      {"cmd":"ha","request":"turn on the living room light"}                  ← preferred
      {"cmd":"ha","request":"create an automation: when the door opens, turn on the porch light"}
      {"cmd":"ha","service":"light.turn_on","entity_id":"light.x"}            ← legacy, rendered to NL
    """
    request = (payload.get("request") or payload.get("text")
               or payload.get("message") or payload.get("query") or "").strip()
    if not request:
        # Legacy structured form -> natural language so the HA agent can act on it.
        service   = payload.get("service")
        entity_id = payload.get("entity_id")
        if service and "." in service:
            _, action = service.split(".", 1)
            verb = ("turn on"  if action.endswith("turn_on")  else
                    "turn off" if action.endswith("turn_off") else
                    action.replace("_", " "))
            request = f"{verb} {entity_id}".strip() if entity_id else f"call service {service}"
        else:
            raise ValueError("ha requires a natural-language 'request' (or legacy service+entity_id)")

    # send_to waits for the HA agent's RESULT. Cap at 45s to stay inside reachy's
    # 60s handle_task budget (automation creation can take a couple of LLM calls).
    result = await agent.send_to("home-assistant-agent", {"text": request}, timeout=45.0)
    if result is None:
        raise RuntimeError("home-assistant-agent did not respond (not running / no reply)")
    if isinstance(result, dict):
        # send_to surfaces routing failures as {"error": ...} with no 'result'.
        if result.get("error") and not result.get("result"):
            raise RuntimeError(f"home-assistant-agent error: {result['error']}")
        return {"delegated_to": "home-assistant-agent", "request": request,
                "ha_result": result.get("result"), "data": result.get("data")}
    return {"delegated_to": "home-assistant-agent", "request": request, "ha_result": str(result)}


# ============================================================
# Reactive bindings
# ============================================================

async def _bind(agent, payload):
    """
    Add a reactive binding.
    payload = {
      'topic': 'home/state/light.living_room',
      'when':  {'new_state.state': 'on'},   # equality on dotted-path fields
      'do':    {'cmd': 'emotion', 'name': 'curious1'}
    }
    Persists across restarts. Re-bindings overwrite by topic+do.
    """
    topic = payload.get("topic")
    do    = payload.get("do")
    when  = payload.get("when") or {}
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
