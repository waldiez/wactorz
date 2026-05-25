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
    # media_backend defaults to "no_media": we drive motion only, so opening the
    # WebRTC/GStreamer media manager just burns CPU and causes audio-device
    # contention on Windows (see GStreamer "send failed because receiver is gone"
    # errors). Override by publishing {"media_backend": "auto"} to
    # custom/reachy/config + restart if you ever need camera/mic from this agent.
    media_backend = agent.recall("media_backend") or "no_media"
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

    for kwargs in attempts:
        try:
            mini = ReachyMini(**kwargs).__enter__()
            await agent.log(f"Connected to Reachy via {kwargs or 'autodetect'}")
            break
        except TypeError as e:
            # Older SDK without connection_mode/host kwargs — keep trying simpler forms.
            last_err = e
            continue
        except Exception as e:
            last_err = e
            continue

    if mini is None:
        await agent.alert(
            f"Could not open ReachyMini(). Robot powered on and on the same WiFi? "
            f"Try `ping reachy-mini.local`. Apps must be stopped. Last error: {last_err}",
            severity="critical",
        )
        raise RuntimeError(f"reachy connection failed: {last_err}")

    agent.state["mini"] = mini
    agent.state["np"] = np
    agent.state["create_head_pose"] = create_head_pose

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

    # ---- Wake up so the robot is ready for commands ----
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
                 "unbind", "list_emotions", "stop"):
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
    await agent.publish("custom/reachy/state", {
        "awake":   agent.state.get("awake", False),
        "busy":    agent.state.get("busy", False),
        "last_cmd": agent.state.get("last_cmd"),
        "emotions": agent.state.get("emotion_names", []),
        "bindings": list(agent.state.get("bindings", {}).keys()),
        "robot_host":    agent.state.get("robot_host") or "(autodetect)",
        "media_backend": agent.state.get("media_backend") or "default",
        "ts":            _time.time(),
    })


async def handle_task(agent, payload):
    # Direct send_to(reachy-body, {...}) goes here — same dispatch as MQTT.
    # Peel IOAgent envelope when the user types: @reachy-body {"cmd": "...", ...}
    # IOAgent wraps as: {"text": "<your-json-string>", "from": ..., "reply_to": ...}
    # We try to unwrap, fall back to natural-language parsing of simple verbs.
    payload = payload or {}
    if "cmd" not in payload and "action" not in payload:
        text = payload.get("text") or payload.get("content") or payload.get("message")
        if isinstance(text, str):
            stripped = text.strip()
            # Try JSON first
            if stripped.startswith("{") and stripped.endswith("}"):
                import json as _json
                try:
                    parsed = _json.loads(stripped)
                    if isinstance(parsed, dict):
                        payload = parsed
                except Exception:
                    pass
            # Fall back: very simple natural-language verbs
            elif stripped:
                low = stripped.lower()
                if   low in ("wake", "wake up"):           payload = {"cmd": "wake"}
                elif low in ("sleep", "go to sleep"):      payload = {"cmd": "sleep"}
                elif low in ("stop",):                     payload = {"cmd": "stop"}
                elif low in ("list emotions", "emotions"): payload = {"cmd": "list_emotions"}
    cmd = payload.get("cmd") or payload.get("action")
    return await _dispatch(agent, cmd, payload, return_result=True)


async def cleanup(agent):
    mini = agent.state.get("mini")
    if not mini:
        return
    try:
        await _do(mini.goto_sleep)
    except Exception as e:
        await agent.log(f"goto_sleep on cleanup failed: {e}", level="warning")
    try:
        mini.__exit__(None, None, None)
    except Exception as e:
        await agent.log(f"ReachyMini __exit__ failed: {e}", level="warning")
    # Persist bindings on graceful shutdown
    agent.persist("bindings", agent.state.get("bindings", {}))


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
    mini = agent.state["mini"]
    async with agent.state["motion_lock"]:
        agent.state["busy"] = True
        try:
            await _do(mini.goto_sleep)
            agent.state["awake"] = False
        finally:
            agent.state["busy"] = False
    return {}


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