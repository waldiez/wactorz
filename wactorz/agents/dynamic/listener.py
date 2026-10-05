"""The subscription hub of a generated program: one connection, repaired callbacks.

The connection, the bindings and the per-topic queues are the shared hub in
:mod:`wactorz.core.subscriptions`, which every actor gets through
``Actor.subscribe``. What a generated program adds is what happens when a
callback keeps failing: the model is asked to repair the program in place, the
way a crashing ``process()`` is, before the actor is given up on.

The callback error budget is kept on the actor rather than in this scope
because a reconnect rebuilds the loop, and a clean `process()` run must not
clear errors that `subscribe` recorded.
"""

import logging
import time
import traceback
from typing import Any

from ...core.mqtt import client_id, mqtt_client
from ...core.subscriptions import Binding, is_durable_actor, safe_invoke
from ...core.subscriptions import SubscriptionHub as SharedSubscriptionHub

logger = logging.getLogger(__name__)

__all__ = [
    "CB_ERROR_REPORT_INTERVAL",
    "CB_LLM_FIX_AT",
    "CB_MAX_CONSECUTIVE_FAILURES",
    "SubscriptionHub",
    "hub_for",
    "is_durable_actor",
    "safe_invoke",
]

#: Consecutive callback failures before the binding is dropped and the actor fails.
CB_MAX_CONSECUTIVE_FAILURES = 5
#: Consecutive failures at which the model is asked to repair the program in place.
CB_LLM_FIX_AT = 3
#: Seconds between repeat reports of the same failing callback to supervision.
#: Only the reporting is rate-limited; every failure counts toward the budget.
CB_ERROR_REPORT_INTERVAL = 10.0


class SubscriptionHub(SharedSubscriptionHub):
    """The shared hub, with a generated program's repair-or-fail budget.

    Named as the shared one is, because this module is where a generated
    program's hub has always been reached and tested.
    """

    def _connect(self) -> Any:
        # Through this module's name for the client factory, so what a test
        # stands in for it here is what the hub connects with.
        return mqtt_client(
            self._actor._mqtt_broker,
            self._actor._mqtt_port,
            identifier=client_id("agent", str(self._actor.actor_id)),
            **self._session_kwargs(),
        )

    def _record_success(self, binding: Binding) -> None:
        actor = self._actor
        actor._cb_error_count.pop(binding.topic, None)
        actor._cb_error_last.pop(binding.topic, None)

    async def _record_failure(self, binding: Binding, error: BaseException) -> None:
        """Count a failing callback, report it, and repair or fail the actor.

        Every failure counts toward the budget; only the report to supervision
        is rate-limited. At `CB_LLM_FIX_AT` straight failures the program is
        repaired in place, the same way a crashing process() is: the model
        fixes the code, the old program is torn down, and the repaired one
        starts from its own setup() with the agent's memory carried across.
        At `CB_MAX_CONSECUTIVE_FAILURES` the actor is marked FAILED for the
        Supervisor.
        """
        actor = self._actor
        topic = binding.topic
        now = time.time()
        tb = traceback.format_exc()
        failures = actor._cb_error_count.get(topic, 0) + 1
        actor._cb_error_count[topic] = failures
        self._failures[topic] = failures
        fatal = failures >= CB_MAX_CONSECUTIVE_FAILURES
        can_repair = getattr(actor, "_can_repair_in_place", lambda: False)()

        # `exc_info=error`, not `.exception()`: this runs from the caller's
        # except block, so the ambient exception is still set at runtime, but
        # nothing here says which exception is being reported. Naming it is both
        # clearer and correct if this is ever called from somewhere else.
        if can_repair and not fatal:
            logger.error(
                "[%s] subscribe callback error (failure #%s — LLM fix at %s, FAILED at %s, topic=%s)",
                actor.name,
                failures,
                CB_LLM_FIX_AT,
                CB_MAX_CONSECUTIVE_FAILURES,
                topic,
                exc_info=error,
            )
        else:
            logger.error(
                "[%s] subscribe callback error (failure #%s/%s, topic=%s)",
                actor.name,
                failures,
                CB_MAX_CONSECUTIVE_FAILURES,
                topic,
                exc_info=error,
            )

        last_report = actor._cb_error_last.get(topic, 0)
        if fatal or (now - last_report) >= CB_ERROR_REPORT_INTERVAL:
            actor._cb_error_last[topic] = now
            await actor._publish_error(
                phase="subscribe_callback",
                error=error,
                traceback_str=tb,
                fatal=fatal,
            )

        if can_repair and not fatal and failures >= CB_LLM_FIX_AT:
            repaired = await actor._repair_program_in_place(
                error, tb, phase=f"subscribe callback on '{topic}'", failures=failures
            )
            if repaired:
                # The repair cleared this binding with the rest of the old
                # program; `_drain` ends this worker when the callback returns.
                actor._cb_error_count.pop(topic, None)
                actor._cb_error_last.pop(topic, None)
                return
            # No repair was made: the plain budget carries on to FAILED.

        if not fatal:
            return

        # Budget exhausted. Drop this binding rather than the connection: the
        # actor is being marked FAILED for the Supervisor to restart, and the
        # other subscriptions must not keep firing into a program on its way out.
        self.fail_binding(binding)


def hub_for(actor: Any) -> SubscriptionHub:
    """The actor's subscription hub, created on first subscribe."""
    hub = getattr(actor, "_sub_hub", None)
    if hub is None:
        hub = SubscriptionHub(actor, durable=is_durable_actor(actor))
        actor._sub_hub = hub
    return hub
