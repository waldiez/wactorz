"""
CATALOG RECIPE — reachy-body-agent
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
    # media_backend defaults to "" (SDK default) so the audio system is
    # initialized and the say command can play through Reachy's speaker.
    # Set to "no_media" via custom/reachy/config + restart if you hit
    # GStreamer audio-device contention on Windows and don't need the speaker.
    media_backend = agent.recall("media_backend") or ""
    agent.state["media_backend"] = media_backend

    mini = None
    last_err = None
    attempts = []
    base = {"media_backend": media_backend} if media_backend else {}
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

    # ---- Discover HA entities so the LLM uses the right IDs ----
    # Without this the planner falls back to a hardcoded entity_id that may not
    # exist on the user's HA (different network = different entities).
    agent.state["ha_entities"] = await _fetch_ha_entities(agent)

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
                 "unbind", "list_emotions", "stop", "say"):
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

Home Assistant commands (use the actual entity_id from the inventory below):
  {"cmd":"ha","service":"light.turn_on","entity_id":"<light entity>"}
  {"cmd":"ha","service":"light.turn_off","entity_id":"<light entity>"}
  {"cmd":"ha","service":"switch.turn_on","entity_id":"<switch entity>"}
  {"cmd":"ha","service":"switch.turn_off","entity_id":"<switch entity>"}

Reactive bindings — for "WHEN X happens, do Y" requests:
  {"cmd":"bind",
   "topic":"homeassistant/state_changes",
   "when":{"entity_id":"<entity>","new_state.state":"on"|"off"},
   "do":{"cmd":"<wake|sleep|pose|...>"}}
  {"cmd":"unbind","topic":"homeassistant/state_changes"}    ← removes ALL rules on a topic
A bind is a STANDING rule — it survives restarts and fires every time the HA event matches.

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
- "WHEN X" / "EVERY TIME X" / "if X happens" / "whenever" / "react to" / "when ... goes on/off"
  → the user wants a STANDING RULE. Emit one or more {"cmd":"bind",...} commands,
    NOT a one-shot action. Do not also emit the action itself afterwards.
- A request without "when/whenever/every/if" is a one-shot — emit the action directly.
- If the user mentions a light, lamp, switch, plug, or any smart home thing in a
  one-shot request, you MUST emit the matching {"cmd":"ha", ...} command.
- "turn on/off the light", "open/close the lamp", "lights on", "switch on"
  → ALL mean an HA light.turn_on / light.turn_off call.
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

Reply with ONLY the JSON array, no markdown, no prose."""


async def _nl_to_commands(agent, text):
    import json as _json
    if agent.llm is None:
        return None
    # Inject the live HA entity list so the LLM uses real IDs from THIS network.
    ents = agent.state.get("ha_entities") or {}
    lines = []
    for kind, items in (("Lights", ents.get("lights", [])), ("Switches", ents.get("switches", []))):
        if not items:
            continue
        lines.append(f"\n{kind} (use the exact entity_id):")
        for it in items:
            lines.append(f"  {it['entity_id']:50s}  ({it['name']})")
    ha_section = "\n".join(lines) if lines else "\n(no HA entities discovered — ha commands will fail)"
    system_with_ents = _NL_SYSTEM + "\n\nLive HA inventory:" + ha_section
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
                low = stripped.lower()
                # Single-verb shortcuts (no LLM call needed)
                if   low in ("wake", "wake up"):           payload = {"cmd": "wake"}
                elif low in ("sleep", "go to sleep"):      payload = {"cmd": "sleep"}
                elif low in ("stop",):                     payload = {"cmd": "stop"}
                elif low in ("list emotions", "emotions"): payload = {"cmd": "list_emotions"}
                else:
                    # Natural language — ask the LLM to plan a command sequence,
                    # then execute it synchronously. handle_task has 60s budget;
                    # an LLM call + a few short motions fits comfortably.
                    cmds = await _nl_to_commands(agent, stripped)
                    if not cmds:
                        return {"ok": False, "error": "could not parse instruction", "text": stripped,
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
        elif cmd == "ha":            result = await _ha_call(agent, payload)
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
    if getattr(media, "audio", None) is None:
        raise RuntimeError(
            "reachy audio backend is not initialized — media_backend is "
            f"'{agent.state.get('media_backend') or 'default'}'. Publish "
            '{"media_backend": ""} to custom/reachy/config and restart the agent.'
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

    tmp_path = os.path.join(tempfile.gettempdir(), f"reachy_say_{uuid.uuid4().hex}.mp3")
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(tmp_path)

    # -- Play through the robot's speaker (non-blocking GStreamer playbin) --
    await _do(media.play_sound, tmp_path)

    # Clean up the previous utterance now that a new one is playing.
    prev = agent.state.get("_say_tmp")
    if prev and prev != tmp_path:
        try:
            os.unlink(prev)
        except Exception:
            pass
    agent.state["_say_tmp"] = tmp_path

    return {"said": text, "voice": voice}


async def _fetch_ha_entities(agent):
    """Query HA /api/states once at startup. Returns {'lights': [...], 'switches': [...]}.
    Used to inject the right entity IDs into the LLM system prompt — without this
    the planner uses a hardcoded entity that may not exist on this network."""
    import os, aiohttp
    ha_url   = (os.environ.get("HA_URL") or "").strip()
    ha_token = os.environ.get("HA_TOKEN")
    if not ha_url or not ha_token:
        await agent.log("HA_URL or HA_TOKEN not set — skipping entity discovery", level="warning")
        return {"lights": [], "switches": []}
    headers = {"Authorization": f"Bearer {ha_token}"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{ha_url.rstrip('/')}/api/states", headers=headers,
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status != 200:
                    await agent.log(f"HA states fetch returned {r.status}", level="warning")
                    return {"lights": [], "switches": []}
                states = await r.json()
    except Exception as e:
        await agent.log(f"HA entity discovery failed: {e}", level="warning")
        return {"lights": [], "switches": []}
    lights, switches = [], []
    for s in states:
        eid = s.get("entity_id", "")
        if s.get("state") == "unavailable":
            continue  # skip dead entities so the LLM doesn't try them
        name = (s.get("attributes") or {}).get("friendly_name", eid)
        if eid.startswith("light."):
            lights.append({"entity_id": eid, "name": name})
        elif eid.startswith("switch."):
            switches.append({"entity_id": eid, "name": name})
    await agent.log(f"HA entities discovered: {len(lights)} light(s), {len(switches)} switch(es)")
    return {"lights": lights, "switches": switches}


async def _ha_call(agent, payload):
    """Call a Home Assistant service. payload = {service: 'light.turn_on', entity_id: '...'}.
    Reads HA_URL and HA_TOKEN from process env (.env is loaded by wactorz at startup)."""
    import os, aiohttp
    service   = payload.get("service")    # e.g. "light.turn_on"
    entity_id = payload.get("entity_id")
    if not service or "." not in service:
        raise ValueError("ha requires 'service' like 'light.turn_on'")
    domain, action = service.split(".", 1)
    ha_url   = (os.environ.get("HA_URL") or "").strip()
    ha_token = os.environ.get("HA_TOKEN")
    if not ha_url or not ha_token:
        raise RuntimeError("HA_URL or HA_TOKEN not set in environment")
    url = f"{ha_url.rstrip('/')}/api/services/{domain}/{action}"
    body = {}
    if entity_id:
        body["entity_id"] = entity_id
    # Pass through any extra fields (brightness, color, etc.)
    for k, v in (payload or {}).items():
        if k not in ("cmd", "action", "service", "entity_id", "id", "_task_id", "task", "_verb"):
            body[k] = v
    headers = {"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"HA call {service} failed [{r.status}]: {text[:200]}")
    return {"service": service, "entity_id": entity_id}


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
