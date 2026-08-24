"""One MQTT subscription, held open for the lifetime of the agent.

Lifted out of `AgentAPI.subscribe` so the subscription loop is readable on its
own. The callback error budget is kept on the actor rather than in this scope
because a reconnect rebuilds the loop, and a clean `process()` run must not
clear errors that `subscribe` recorded.
"""

import asyncio
import json
import logging
import time
import traceback
from typing import Any

from ...core.mqtt import mqtt_client

logger = logging.getLogger(__name__)

#: Escalations without recovery before the listener stops and the actor fails.
CB_MAX_ESCALATIONS = 5
#: Seconds between repeat reports of the same failing callback.
CB_ERROR_REPORT_INTERVAL = 30.0


async def safe_invoke(cb: Any, payload: Any, actor: Any, warned: list[bool]) -> None:
    """Run a subscribe callback, tolerating a stray `await` on a sync call."""
    try:
        await cb(payload)
    except TypeError as e:
        if "NoneType" in str(e) and "await" in str(e):
            if not warned[0]:
                logger.warning(
                    "[%s] subscribe callback has 'await None' error (suppressed): %s",
                    actor.name,
                    e,
                )
                warned[0] = True
            # Swallow: a sync API method was awaited, harmless
        else:
            raise


async def run_subscription_listener(actor: Any, topic: str, callback: Any) -> None:
    """Hold one MQTT subscription open, invoking `callback` per message.

    Error budget lives on the actor, not in this scope: a reconnect rebuilds
    the loop, and a clean process() run must not clear a callback's count.
    """
    warned = [False]
    try:
        import aiomqtt  # noqa: F401
    except ImportError:
        logger.exception("[%s] aiomqtt not installed", actor.name)
        return
    while True:
        try:
            async with mqtt_client(actor._mqtt_broker, actor._mqtt_port) as client:
                await client.subscribe(topic)
                logger.info("[%s] Subscribed to %s", actor.name, topic)
                async for msg in client.messages:
                    try:
                        payload = json.loads(msg.payload.decode())
                    except Exception:
                        payload = {"raw": msg.payload.decode()}
                    try:
                        await safe_invoke(callback, payload, actor, warned)
                        # Successful invocation — reset this topic's error budget
                        actor._cb_error_count.pop(topic, None)
                        actor._cb_error_last.pop(topic, None)
                    except Exception as e:
                        now = time.time()
                        last = actor._cb_error_last.get(topic, 0)
                        escalations = actor._cb_error_count.get(topic, 0)

                        logger.exception(
                            "[%s] subscribe callback error (escalation #%s/%s, topic=%s)",
                            actor.name,
                            escalations + 1,
                            CB_MAX_ESCALATIONS,
                            topic,
                        )

                        # Rate-limit escalation to supervision
                        if (now - last) >= CB_ERROR_REPORT_INTERVAL:
                            escalations += 1
                            actor._cb_error_count[topic] = escalations
                            actor._cb_error_last[topic] = now

                            fatal = escalations >= CB_MAX_ESCALATIONS
                            await actor._publish_error(
                                phase="subscribe_callback",
                                error=e,
                                traceback_str=traceback.format_exc(),
                                fatal=fatal,
                            )

                            if fatal:
                                # Budget exhausted — stop looping, let Supervisor restart
                                logger.critical(
                                    "[%s] subscribe callback on '%s' failed %sx — marking FAILED for Supervisor.",
                                    actor.name,
                                    topic,
                                    escalations,
                                )
                                from ...core.actor import ActorState

                                actor.state = ActorState.FAILED
                                return  # exits _listener task
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("[%s] MQTT subscribe error: %s — retrying in 5s", actor.name, e)
            await asyncio.sleep(5)
