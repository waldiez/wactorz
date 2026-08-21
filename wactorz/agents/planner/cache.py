"""Reuse of a previous plan for a repeated task, while it still applies.

A cached plan is only valid as long as the agents it delegates to are still
around, so liveness is checked on every read rather than trusted from the time
the entry was written.
"""

import logging
import time

logger = logging.getLogger(__name__)

#: Where the cache sits in the planner's persistent state.
PLAN_CACHE_KEY = "_plan_cache"
#: How long an entry stays usable.
CACHE_TTL_S = 86400  # 24 hours


def select_cached_plan(
    raw: dict[str, dict], cache_key: str, alive: set[str], log_name: str
) -> list | None:
    """Return the cached plan for ``cache_key``, or None if it cannot be reused."""
    entry = raw.get(cache_key)
    if not entry:
        return None

    age = time.time() - entry.get("timestamp", 0)
    if age > CACHE_TTL_S:
        logger.info("[%s] Cache expired (%.1fh old)", log_name, age / 3600)
        return None

    plan = entry.get("plan", [])
    if not plan:
        return None

    for step in plan:
        agent = step.get("agent", "")
        # A step carrying a spawn_config creates its own agent, so the agent
        # being absent now is expected rather than stale.
        if agent not in alive and not step.get("spawn_config"):
            logger.info("[%s] Cache invalid — agent '%s' no longer running", log_name, agent)
            return None

    return plan


def with_plan_cached(
    raw: dict[str, dict], cache_key: str, task: str, plan: list
) -> dict[str, dict]:
    """Return ``raw`` plus this plan, with entries past the TTL dropped."""
    now = time.time()
    fresh = {k: v for k, v in raw.items() if now - v.get("timestamp", 0) < CACHE_TTL_S}
    fresh[cache_key] = {"task": task[:200], "plan": plan, "timestamp": now}
    return fresh
