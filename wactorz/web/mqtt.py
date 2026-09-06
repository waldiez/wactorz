"""Broker link: subscribe, dispatch, and report connection state.

Owns the long-lived MQTT listener that feeds every inbound broker message
through ``events.parse_topic`` into the live state, plus the startup
reachability probe. The connection flag itself lives in ``runtime`` — ``ws``
reports it to browsers, so it cannot live here (mqtt already depends on ws).
"""

import asyncio
import json
import logging
import time
from typing import Any

from ..core.mqtt import client_id, install_id, mqtt_client
from ..monitoring.log_redaction import redact
from . import events, relay, runtime, ws

logger = logging.getLogger(__name__)


async def broadcast_mqtt_msg(topic: str, payload: str) -> None:
    """Broadcast a received mqtt message."""
    parsed: Any = payload
    try:
        parsed = json.loads(payload)
    except Exception:
        # non-JSON: pass the string through
        pass
    await ws.broadcast({"type": "server_event", "topic": topic, "payload": parsed})


# Server-broker connection state, mirrored to browsers so the dashboard's "live"
# Server-broker connection state, mirrored to browsers so the dashboard's "live"
async def set_mqtt_status(connected: bool) -> None:
    """Broadcast a change in the server↔broker connection state to all browsers."""
    if runtime.mqtt_connected == connected:
        return
    runtime.mqtt_connected = connected
    await ws.broadcast({"type": "mqtt_status", "connected": connected})


async def handle_message(topic: str, payload: str) -> None:
    """Dispatch one broker message: update state, then tell the browsers.

    Extracted from the listener loop so it can be exercised without a broker —
    the loop around it is connection management, this is the part with rules.
    """
    event: dict[str, Any] | None = events.parse_topic(topic, payload)
    # State first, and for every message: the filter below decides only whether a
    # browser is shown the raw frame, not whether the server takes it in.
    if relay.is_relayed(topic):
        await broadcast_mqtt_msg(topic, payload)
    if not event or runtime.hard_resetting:
        return

    metric = event.get("metric", "")
    log_event = None if metric == "heartbeat" else event
    # Totals are the only part of a snapshot that queries the database, so they
    # are not rebuilt for every broker message. `chat` is the exception: it is
    # the frame that follows an agent spending money, it is driven by user
    # activity rather than a timer, and it is what someone watching the cost is
    # waiting to see. `heartbeat` and `metrics` both fire on the heartbeat loop,
    # so triggering on those would scale the query with agent count — which is
    # what taking totals off this path was for.
    await ws.broadcast(
        {
            "type": "patch",
            "event": log_event,
            "state": events.snapshot(include_totals=metric == "chat"),
        }
    )

    # Agent-originated user-facing message. The browser already renders it from
    # agents/{id}/chat (the broadcast above), so no second frame is sent here —
    # this only persists it so it survives a reload like any other turn.
    push = event.get("_push_chat")
    if push:
        try:
            if runtime.db is not None and push.get("content"):
                # A voice turn arrives as the user's own words (from="user"), so
                # the role and the agent it belongs to come from opposite ends of
                # the envelope; persisting it as "assistant" would replay the
                # user's speech back as the agent's reply on reload.
                role = "user" if push.get("from") == "user" else "assistant"
                agent_name = (
                    push.get("to", "agent") if role == "user" else push.get("from", "agent")
                )
                runtime.db.write_chat_log(
                    ts=push.get("timestamp", time.time()),
                    agent_name=agent_name,
                    role=role,
                    # Same treatment as the WS path: an agent can quote back
                    # something a user typed, and this row outlives the turn.
                    content=redact(push["content"]),
                )
        except Exception as exc:
            logger.debug("[chat-bridge] persist failed: %s", exc)


async def mqtt_listener() -> None:
    """Subscribe to topics and handle mqtt messages."""
    logger.info("Connecting to MQTT %s:%s...", runtime.MQTT_BROKER, runtime.MQTT_PORT)
    try:
        while True:
            try:
                async with mqtt_client(
                    runtime.MQTT_BROKER,
                    runtime.MQTT_PORT,
                    identifier=client_id("mon", install_id()),
                    clean_session=False,
                ) as client:
                    runtime.mqtt_client_ref = client
                    logger.info("MQTT connected.")

                    if runtime.registry is not None:
                        await client.publish(
                            f"agents/{runtime.IO_GATEWAY_ID}/spawn",
                            json.dumps(
                                {
                                    "agentId": runtime.IO_GATEWAY_ID,
                                    "agentName": runtime.IO_GATEWAY_ID,
                                    "agentType": "gateway",
                                    "timestamp": time.time(),
                                }
                            ),
                        )

                    for topic in runtime.MQTT_TOPICS:
                        await client.subscribe(topic, qos=1)

                    await set_mqtt_status(True)

                    async for message in client.messages:
                        await handle_message(
                            str(message.topic), message.payload.decode(errors="replace")
                        )

            except Exception as e:
                runtime.mqtt_client_ref = None
                await set_mqtt_status(False)
                logger.warning("MQTT error: %s. Reconnecting in 5s...", e)
                await asyncio.sleep(5)
    finally:
        # Drop ref and force GC while loop is still open so paho's __del__
        # doesn't fire after the event loop closes (avoids RuntimeError noise).
        import gc

        runtime.mqtt_client_ref = None
        gc.collect()


# ── Startup checks ─────────────────────────────────────────────────────────


async def check_mqtt(attempts: int = 5, delay: float = 0.5) -> bool:
    """Return True if MQTT broker is reachable.

    Retries briefly so a transient blip (or a broker mid-restart) does not fatally
    abort startup: the aiomqtt client itself reconnects, so this pre-flight probe
    must be at least as tolerant, or it aborts a server whose MQTT is actually fine.
    """
    last = ""
    for i in range(attempts):
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(runtime.MQTT_BROKER, runtime.MQTT_PORT), timeout=3
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception as exc:
            last = repr(exc)
            if i < attempts - 1:
                await asyncio.sleep(delay)
    logger.error(
        "[startup] MQTT broker %s:%s unreachable after %s tries — %s",
        runtime.MQTT_BROKER,
        runtime.MQTT_PORT,
        attempts,
        last,
    )
    return False
