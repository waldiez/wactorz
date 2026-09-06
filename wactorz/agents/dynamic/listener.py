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

#: Consecutive callback failures before the listener stops and the actor fails.
CB_MAX_CONSECUTIVE_FAILURES = 5
#: Consecutive failures at which the LLM is asked to fix the callback's code.
CB_LLM_FIX_AT = 3
#: Seconds between repeat reports of the same failing callback to supervision.
#: Only the reporting is rate-limited; every failure counts toward the budget.
CB_ERROR_REPORT_INTERVAL = 10.0


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
    # One LLM fix per listener lifetime: the fix is staged for the restart that
    # follows, so a second attempt here would only fix the same stale code.
    fix_attempted = False
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
                        tb = traceback.format_exc()
                        last_report = actor._cb_error_last.get(topic, 0)
                        failures = actor._cb_error_count.get(topic, 0) + 1
                        actor._cb_error_count[topic] = failures
                        fatal = failures >= CB_MAX_CONSECUTIVE_FAILURES
                        fixer = getattr(actor, "_fix_subscribe_callback_with_llm", None)

                        if fixer and not fix_attempted:
                            logger.exception(
                                "[%s] subscribe callback error (failure #%s — LLM fix at %s, FAILED at %s, topic=%s)",
                                actor.name,
                                failures,
                                CB_LLM_FIX_AT,
                                CB_MAX_CONSECUTIVE_FAILURES,
                                topic,
                            )
                        else:
                            logger.exception(
                                "[%s] subscribe callback error (failure #%s/%s, topic=%s)",
                                actor.name,
                                failures,
                                CB_MAX_CONSECUTIVE_FAILURES,
                                topic,
                            )

                        # Every failure counts; only the report to supervision is
                        # rate-limited. A fatal failure is always reported.
                        if fatal or (now - last_report) >= CB_ERROR_REPORT_INTERVAL:
                            actor._cb_error_last[topic] = now
                            await actor._publish_error(
                                phase="subscribe_callback",
                                error=e,
                                traceback_str=tb,
                                fatal=fatal,
                            )

                        # ── LLM self-healing ─────────────────────────────────
                        # The callback is a closure bound to setup()'s namespace,
                        # so the fix cannot be swapped in here. Stage it and fail
                        # the actor: the Supervisor restarts it with the fixed
                        # code, on the same persisted state.
                        if not fatal and not fix_attempted and failures >= CB_LLM_FIX_AT and fixer:
                            fix_attempted = True
                            logger.warning(
                                "[%s] %s consecutive subscribe callback errors on '%s' — asking LLM to fix code.",
                                actor.name,
                                failures,
                                topic,
                            )
                            if await fixer(e, tb):
                                logger.warning(
                                    "[%s] Fixed code staged — marking FAILED so Supervisor restarts with it.",
                                    actor.name,
                                )
                                from ...core.actor import ActorState

                                actor.state = ActorState.FAILED
                                return  # exits _listener task

                        if fatal:
                            # Budget exhausted — stop looping, let Supervisor restart
                            logger.critical(
                                "[%s] subscribe callback on '%s' failed %sx in a row — marking FAILED for Supervisor.",
                                actor.name,
                                topic,
                                failures,
                            )
                            from ...core.actor import ActorState

                            actor.state = ActorState.FAILED
                            return  # exits _listener task
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("[%s] MQTT subscribe error: %s — retrying in 5s", actor.name, e)
            await asyncio.sleep(5)
