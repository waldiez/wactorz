"""ScheduledAgent — first-class scheduled trigger primitive.

Fires an MQTT event at a configured schedule. Designed to be paired with a
downstream consumer (ha_actuator, dynamic agent, etc.) that subscribes to
the fire topic and performs the actual action.

This exists because LLM-generated dynamic agents kept producing broken
time-based patterns:

  # broken — checks once at startup
  async def setup(agent):
      if datetime.now().hour == 17:
          await trigger()

  # broken — busy-loop, hits framework's 120s timeout
  async def process(agent):
      while True:
          if datetime.now().hour == 17:
              await trigger()
          await asyncio.sleep(60)

  # mostly works but wasteful — polls clock 60+ times per fire
  async def process(agent):
      if datetime.now().hour == 17 and datetime.now().minute == 0:
          await trigger()

ScheduledAgent replaces all of these with a single, correct primitive that
sleeps until the next fire time, fires once, recomputes, and repeats.

Schedule spec (canonical form is a dict — easier for the LLM to emit
correctly than a cron string and easier for users to read in /rules):

  Daily:    {"type": "daily",    "at": "17:00"}
  Weekly:   {"type": "weekly",   "at": "17:00", "days": ["mon", "wed", "fri"]}
  Interval: {"type": "interval", "seconds": 1800}
  Once:     {"type": "once",     "at": "2026-05-02T17:00:00"}    # ISO-8601 local time
  Cron:     {"type": "cron",     "expr": "0 17 * * *"}            # escape hatch

Day names accepted: mon|tue|wed|thu|fri|sat|sun (lowercase, three letters).

When the agent fires, it publishes to:
    schedule/<agent-name>/fired

with payload:
    {"fired_at": "<ISO-8601 UTC>", "schedule_type": "<type>", "agent": "<name>"}

Timezone resolution:
    1. Schedule spec's optional "tz" field (e.g. "Europe/Athens")
    2. User fact "pref_timezone" if available (read from a 'timezone' kwarg
       passed at spawn time — main injects this)
    3. System local timezone

Catch-up behavior on restart:
    - "once" schedules: fire if missed within the last 5 minutes; otherwise
      the agent self-deletes silently (the moment passed long ago).
    - Recurring schedules: never catch up. Resume from the next fire time.

The agent runs a single background task that sleeps until the next fire,
fires, then loops. Every 5 minutes the sleep is also bounded so that DST
transitions, system clock jumps, and laptop sleep don't strand the agent.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 — should not happen in this codebase
    ZoneInfo = None  # type: ignore[assignment]

from ..core.actor import Actor, Message, MessageType
from .lookup import find_main_actor

logger = logging.getLogger(__name__)


# ── Day-of-week parsing ──────────────────────────────────────────────────────
# Python's datetime.weekday(): Monday=0, Sunday=6
_DAY_NAMES = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
    # Tolerate full names too — LLMs sometimes spell them out
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


# ── Re-bound for safety: never sleep more than this between wake-ups ────────
# Bounding the sleep guarantees we re-evaluate the next fire time at least
# this often, which catches DST transitions, system clock changes, and
# laptops waking from sleep. 5 minutes is short enough to be responsive,
# long enough to keep CPU wakes negligible.
_MAX_SLEEP_S = 300.0

# ── Catch-up window for "once" schedules ────────────────────────────────────
# If main was down at the scheduled time and comes back up within this
# window, the schedule still fires. Beyond it, the moment is considered
# lost and the agent self-deletes. Recurring schedules NEVER catch up.
_ONESHOT_CATCHUP_S = 300.0


class ScheduleError(ValueError):
    """A schedule that cannot be run, and why.

    A `ValueError`, because a spawner reports a ValueError as rejected input
    rather than as a failure to start.
    """

    @classmethod
    def not_a_dict(cls, schedule: Any) -> "ScheduleError":
        return cls(f"ScheduledAgent requires a 'schedule' dict, got {type(schedule).__name__}")

    @classmethod
    def invalid(cls, name: str, exc: Exception) -> "ScheduleError":
        return cls(f"Invalid schedule for {name}: {exc}")

    @classmethod
    def unknown_type(cls, stype: Any) -> "ScheduleError":
        return cls(
            f"Unknown schedule type: {stype!r}. Use one of: daily, weekly, interval, once, cron."
        )

    @classmethod
    def daily_without_at(cls) -> "ScheduleError":
        return cls("daily schedule requires 'at' (e.g. '17:00')")

    @classmethod
    def weekly_without_at(cls) -> "ScheduleError":
        return cls("weekly schedule requires 'at'")

    @classmethod
    def weekly_without_days(cls) -> "ScheduleError":
        return cls("weekly schedule requires non-empty 'days' list")

    @classmethod
    def once_without_at(cls) -> "ScheduleError":
        return cls("once schedule requires 'at' (ISO-8601 datetime)")

    @classmethod
    def cron_without_expr(cls) -> "ScheduleError":
        return cls("cron schedule requires 'expr' (e.g. '0 17 * * *')")

    @classmethod
    def not_hhmm(cls, at: str) -> "ScheduleError":
        return cls(f"Time string must be HH:MM, got {at!r}")

    @classmethod
    def time_out_of_range(cls, at: str) -> "ScheduleError":
        return cls(f"Time out of range: {at!r}")

    @classmethod
    def unknown_day(cls, day: str) -> "ScheduleError":
        return cls(f"Unknown day: {day!r} (use mon|tue|wed|thu|fri|sat|sun)")

    @classmethod
    def interval_not_positive(cls, seconds: int) -> "ScheduleError":
        return cls(f"interval seconds must be positive, got {seconds}")

    @classmethod
    def interval_not_a_number(cls, seconds: Any) -> "ScheduleError":
        return cls(f"interval schedule requires 'seconds' (int), got {seconds!r}")

    @classmethod
    def once_not_iso(cls, at: str) -> "ScheduleError":
        return cls(f"once schedule 'at' must be ISO-8601 (YYYY-MM-DDTHH:MM:SS), got {at!r}")

    @classmethod
    def no_croniter(cls) -> "ScheduleError":
        return cls(
            "Cron schedules require the 'croniter' package. Install with "
            "`pip install 'wactorz[cron]'`, OR use a structured schedule "
            "({'type': 'daily', 'at': '17:00'}) instead — the structured "
            "form covers most cases and is preferred."
        )

    @classmethod
    def bad_cron(cls, expr: str, exc: Exception) -> "ScheduleError":
        return cls(f"Invalid cron expression {expr!r}: {exc}")


def _resolve_timezone(spec_tz: str | None, user_tz: str | None) -> Any:
    """Resolve the schedule's effective timezone.

    Order: explicit spec_tz > user_tz fact > system local.
    Returns a tzinfo. Falls back to UTC if nothing else works (which is
    the wrong answer for users in DST-affected zones, but at least makes
    the agent runnable).
    """
    candidates = [spec_tz, user_tz]
    for cand in candidates:
        if cand and ZoneInfo is not None:
            try:
                return ZoneInfo(cand)
            except Exception:
                logger.warning("[scheduled] Unknown timezone '%s' — trying next fallback", cand)
    # System local — datetime.astimezone() with no arg returns local time
    try:
        local = datetime.now().astimezone().tzinfo
        if local is not None:
            return local
    except Exception:  # noqa: S110  # falls through to the next timezone source
        pass
    return timezone.utc


def _parse_hhmm(at: str) -> tuple[int, int]:
    """Parse 'HH:MM' (or 'HH:MM:SS' — seconds dropped). Raises ValueError."""
    parts = at.strip().split(":")
    if len(parts) < 2:
        raise ScheduleError.not_hhmm(at)
    h = int(parts[0])
    m = int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ScheduleError.time_out_of_range(at)
    return h, m


def _next_fire_daily(now_local: datetime, at: str) -> datetime:
    """Next fire time for a daily schedule, in the same tz as now_local."""
    h, m = _parse_hhmm(at)
    candidate = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate


def _next_fire_weekly(now_local: datetime, at: str, days: list[str]) -> datetime:
    """Next fire time for a weekly schedule. days is a list of day-name strings.
    Picks the soonest matching weekday at or after `at` time.
    """
    if not days:
        raise ScheduleError.weekly_without_days()
    h, m = _parse_hhmm(at)
    day_nums = set()
    for d in days:
        key = d.strip().lower()
        if key not in _DAY_NAMES:
            raise ScheduleError.unknown_day(d)
        day_nums.add(_DAY_NAMES[key])

    # Each named day's next time after now; the soonest wins.
    return min(_next_on_weekday(now_local, day, h, m) for day in day_nums)


def _next_on_weekday(now_local: datetime, weekday: int, h: int, m: int) -> datetime:
    """The next ``h:m`` on ``weekday`` after ``now_local``: this week's, or next week's."""
    ahead = (weekday - now_local.weekday()) % 7
    candidate = (now_local + timedelta(days=ahead)).replace(
        hour=h, minute=m, second=0, microsecond=0
    )
    if candidate <= now_local:
        candidate += timedelta(days=7)
    return candidate


def _next_fire_interval(now_local: datetime, seconds: int, last_fire: datetime | None) -> datetime:
    """Next fire time for an interval schedule.
    If we've fired before, fire `seconds` after the last fire.
    Otherwise, fire `seconds` from now.
    """
    if seconds <= 0:
        raise ScheduleError.interval_not_positive(seconds)
    if last_fire is not None:
        cand = last_fire + timedelta(seconds=seconds)
        if cand > now_local:
            return cand
    return now_local + timedelta(seconds=seconds)


def _next_fire_once(at: str, tzinfo: Any) -> datetime:
    """Parse an ISO-8601 datetime as the one-shot fire time.
    The string is interpreted in `tzinfo` if it has no zone info itself.
    """
    # datetime.fromisoformat accepts "2026-05-02T17:00:00" or with offset
    try:
        dt = datetime.fromisoformat(at.strip())
    except ValueError as e:
        raise ScheduleError.once_not_iso(at) from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tzinfo)
    return dt


def _next_fire_cron(now_local: datetime, expr: str) -> datetime:
    """Cron expression — supported only if `croniter` is installed.
    Falls back to ValueError if not, with a hint to use structured form.
    """
    try:
        from croniter import croniter
    except ImportError as e:
        raise ScheduleError.no_croniter() from e
    try:
        it = croniter(expr, now_local)
        return it.get_next(datetime)
    except Exception as e:
        raise ScheduleError.bad_cron(expr, e) from e


@dataclass
class _ScheduleState:
    """Mutable per-agent schedule state, persisted across restarts."""

    last_fire_iso: str | None = None  # ISO-8601 UTC
    fire_count: int = 0


class ScheduledAgent(Actor):
    """First-class scheduled trigger agent. See module docstring for spec format.

    Spawn-time kwargs:
        name:        actor name (e.g. "evening-lights-trigger")
        schedule:    dict — one of the schedule specs documented above
        timezone:    optional str — overrides system tz; main injects user's
                     pref_timezone here
        publish_topic: optional str — defaults to "schedule/<name>/fired"
        description: optional str — for /rules display
    """

    def __init__(
        self,
        schedule: dict | None = None,
        timezone: str | None = None,
        publish_topic: str | None = None,
        description: str = "",
        **kwargs,
    ):
        kwargs.setdefault("name", "scheduled")
        super().__init__(**kwargs)
        if not isinstance(schedule, dict):
            raise ScheduleError.not_a_dict(schedule)
        self._schedule: dict = dict(schedule)  # defensive copy
        self._user_tz: str | None = timezone
        self._tz = _resolve_timezone(self._schedule.get("tz"), timezone)
        self._publish_topic: str = publish_topic or f"schedule/{self.name}/fired"
        self.description: str = (
            description or f"Scheduled trigger ({self._schedule.get('type', '?')})"
        )

        # Validate the schedule eagerly so a malformed spec fails at spawn,
        # not at first fire. Computing the next fire time is the cheapest
        # full validation we can do.
        try:
            now = datetime.now(self._tz)
            self._compute_next_fire(now, last_fire=None)
        except Exception as e:
            raise ScheduleError.invalid(self.name, e) from e

        # Background loop handle
        self._loop_task: asyncio.Task | None = None
        # Used by handle_message for manual trigger/inspect commands
        self._manual_trigger_event: asyncio.Event = asyncio.Event()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self):
        # Restore last-fire state if persisted
        state = self.recall("_schedule_state") or {}
        if isinstance(state, dict):
            self._state = _ScheduleState(
                **{k: v for k, v in state.items() if k in ("last_fire_iso", "fire_count")}
            )
        else:
            self._state = _ScheduleState()

        # Catch-up logic for "once" schedules: fire if the moment was within
        # the last 5 minutes. Otherwise, we missed it for good — self-delete.
        if self._schedule.get("type") == "once":
            try:
                fire_at = _next_fire_once(self._schedule["at"], self._tz)
                now = datetime.now(self._tz)
                if self._state.fire_count > 0:
                    # Already fired in a previous run — nothing to do
                    logger.info("[%s] One-shot already fired previously, exiting", self.name)
                    self.run_detached(self._self_delete(), name="self-delete")
                    return
                if fire_at < now:
                    delta = (now - fire_at).total_seconds()
                    if delta <= _ONESHOT_CATCHUP_S:
                        logger.info(
                            "[%s] One-shot fire missed by %.0fs (within catchup window) — firing now",
                            self.name,
                            delta,
                        )
                        await self._fire(now_utc=datetime.now(timezone.utc))
                    else:
                        logger.info(
                            "[%s] One-shot fire missed by %.0fs (beyond %.0fs catchup) — exiting",
                            self.name,
                            delta,
                            _ONESHOT_CATCHUP_S,
                        )
                    # Either way, a once-schedule that's past is done
                    self.run_detached(self._self_delete(), name="self-delete")
                    return
            except Exception:
                logger.exception("[%s] Once-schedule on_start error", self.name)

        await self._log(
            f"Scheduled agent ready. type={self._schedule.get('type')} "
            f"tz={self._tz} publishes={self._publish_topic}"
        )
        self._loop_task = asyncio.create_task(self._run_loop())

    async def on_stop(self):
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            # gather rather than a bare await: the scheduling loop's own
            # CancelledError comes back as a value, so ignoring it cannot also
            # swallow a cancellation aimed at whoever called stop(). `except
            # (CancelledError, Exception)` could not tell the two apart, and
            # eating the caller's is what left a supervisor's watch loop
            # believing it had never been cancelled — a shutdown that hangs.
            (outcome,) = await asyncio.gather(self._loop_task, return_exceptions=True)
            # Reported, not dropped: gather *retrieves* the exception, so a loop
            # that died of something real would otherwise vanish at shutdown.
            # CancelledError is a BaseException, so this is a real crash only.
            if isinstance(outcome, Exception):
                logger.error("[%s] scheduling loop ended in error: %s", self.name, outcome)

    # ── Scheduling loop ────────────────────────────────────────────────────

    async def _run_loop(self):
        """Sleep until the next fire time, fire, recompute, repeat.

        Wake bounded to _MAX_SLEEP_S so DST transitions / clock jumps /
        laptop sleep don't leave us stranded. On wake we always recompute
        from current time, never trust an accumulated deadline.
        """
        while self.state.value not in ("stopped", "failed"):
            try:
                now_local = datetime.now(self._tz)
                last_fire = self._last_fire_local(now_local.tzinfo)
                next_fire = self._compute_next_fire(now_local, last_fire)
                wait_s = (next_fire - now_local).total_seconds()
                if wait_s <= 0:
                    # Edge case: clock jumped forward past the fire. Fire now.
                    wait_s = 0
                # Bound the sleep so we re-evaluate periodically
                sleep_for = min(wait_s, _MAX_SLEEP_S)
                logger.debug(
                    "[%s] next fire at %s (sleeping %.1fs of %.1fs remaining)",
                    self.name,
                    next_fire.isoformat(),
                    sleep_for,
                    wait_s,
                )

                # Wait either for the deadline OR a manual-trigger signal,
                # whichever comes first. Manual trigger fires now without
                # advancing the schedule.
                self._manual_trigger_event.clear()
                try:
                    await asyncio.wait_for(
                        self._manual_trigger_event.wait(),
                        timeout=sleep_for if sleep_for > 0 else 0.001,
                    )
                    manual = True
                except asyncio.TimeoutError:
                    manual = False

                # If we woke early via the bounded re-check (not at the deadline,
                # not manual), just loop and recompute.
                if not manual:
                    now_local = datetime.now(self._tz)
                    if now_local < next_fire:
                        continue  # not time yet, recompute and sleep again

                # Time to fire (or manually triggered)
                await self._fire(
                    now_utc=datetime.now(timezone.utc),
                    manual=manual,
                )

                # If this was a "once" schedule, we're done
                if self._schedule.get("type") == "once":
                    logger.info("[%s] One-shot fired — self-deleting", self.name)
                    self.run_detached(self._self_delete(), name="self-delete")
                    return

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[%s] Loop error — backing off 30s", self.name)
                await asyncio.sleep(30)

    def _last_fire_local(self, tzinfo: Any) -> datetime | None:
        """Convert persisted UTC ISO timestamp to a tz-aware local datetime."""
        if not self._state.last_fire_iso:
            return None
        try:
            dt = datetime.fromisoformat(self._state.last_fire_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(tzinfo)
        except Exception:
            return None

    def _compute_next_fire(self, now_local: datetime, last_fire: datetime | None) -> datetime:
        """Dispatch to the right next-fire calculator based on schedule type."""
        spec = self._schedule
        stype = (spec.get("type") or "").strip().lower()

        if stype == "daily":
            at = spec.get("at")
            if not at:
                raise ScheduleError.daily_without_at()
            return _next_fire_daily(now_local, at)

        if stype == "weekly":
            at = spec.get("at")
            days = spec.get("days") or []
            if not at:
                raise ScheduleError.weekly_without_at()
            return _next_fire_weekly(now_local, at, days)

        if stype == "interval":
            secs = spec.get("seconds") or spec.get("every_seconds")
            if secs is None:
                # Allow {"every": "30m"} as a convenience
                every = spec.get("every", "")
                secs = _parse_duration(every) if every else None
            if not isinstance(secs, (int, float)):
                raise ScheduleError.interval_not_a_number(secs)
            return _next_fire_interval(now_local, int(secs), last_fire)

        if stype == "once":
            at = spec.get("at")
            if not at:
                raise ScheduleError.once_without_at()
            return _next_fire_once(at, self._tz)

        if stype == "cron":
            expr = spec.get("expr") or spec.get("expression")
            if not expr:
                raise ScheduleError.cron_without_expr()
            return _next_fire_cron(now_local, expr)

        raise ScheduleError.unknown_type(stype)

    # ── Firing ─────────────────────────────────────────────────────────────

    async def _fire(self, now_utc: datetime, manual: bool = False):
        """Publish the fire event and update persisted state."""
        payload = {
            "fired_at": now_utc.isoformat(),
            "schedule_type": self._schedule.get("type", "?"),
            "agent": self.name,
            "manual": manual,
        }
        try:
            await self._mqtt_publish(self._publish_topic, payload)
            self._state.last_fire_iso = now_utc.isoformat()
            self._state.fire_count += 1
            self.persist(
                "_schedule_state",
                {
                    "last_fire_iso": self._state.last_fire_iso,
                    "fire_count": self._state.fire_count,
                },
            )
            await self._log(
                f"Fired{' (manual)' if manual else ''} → {self._publish_topic} "
                f"[count={self._state.fire_count}]"
            )
        except Exception:
            logger.exception("[%s] Fire failed", self.name)

    async def _self_delete(self):
        """Remove from registry and stop. Used for completed once-schedules."""
        await asyncio.sleep(0.5)
        if self._registry:
            try:
                await self._registry.unregister(self.actor_id)
            except Exception:  # noqa: S110  # teardown; the agent is going away regardless
                pass
        # Best-effort: ask main to drop us from the spawn registry too
        if self._registry:
            main = find_main_actor(self._registry)
            if main:
                try:
                    main._remove_from_spawn_registry(self.name, actor_id=self.actor_id)
                except Exception:  # noqa: S110  # teardown; the agent is going away regardless
                    pass
        await self.stop()
        # After stop(), so the final status it publishes cannot be mistaken for
        # a schedule that is still here. This is what tells the dashboard the
        # card is gone; without it the entry outlives the agent.
        await self.withdraw_manifest()

    # ── Message handling ───────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        """Supports two task types for inspection / testing:

        {"action": "trigger"} — fire NOW without advancing the schedule
        {"action": "info"}    — return current schedule + next fire info
        """
        if msg.type != MessageType.TASK:
            return
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        action = (payload.get("action") or payload.get("text") or "").strip().lower()
        reply_to = msg.reply_to or msg.sender_id
        task_id = payload.get("_task_id")

        async def _reply(body: dict):
            if not reply_to:
                return
            r = dict(body)
            if task_id:
                r["_task_id"] = task_id
            await self.send(reply_to, MessageType.RESULT, r)

        if action in ("trigger", "fire", "fire now"):
            self._manual_trigger_event.set()
            await _reply({"result": f"Manual trigger queued for {self.name}"})
            return

        if action in ("info", "next", "status"):
            try:
                now_local = datetime.now(self._tz)
                last = self._last_fire_local(now_local.tzinfo)
                next_fire = self._compute_next_fire(now_local, last)
                info = {
                    "name": self.name,
                    "schedule": self._schedule,
                    "tz": str(self._tz),
                    "publish_topic": self._publish_topic,
                    "next_fire": next_fire.isoformat(),
                    "last_fire": self._state.last_fire_iso,
                    "fire_count": self._state.fire_count,
                }
                await _reply({"result": info, "info": info})
            except Exception as e:
                await _reply({"error": str(e)})
            return

        await _reply({"error": f"Unknown action: {action!r}"})

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _log(self, msg: str):
        logger.info("[%s] %s", self.name, msg)
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": msg, "timestamp": time.time()},
        )


# ── Public helpers ──────────────────────────────────────────────────────────


def _parse_duration(s: str) -> int | None:
    """Parse '30s', '5m', '2h', '1d' to seconds. Returns None on failure.
    Used as a convenience input format for interval schedules.
    """
    if not s:
        return None
    s = s.strip().lower()
    try:
        if s.endswith("s"):
            return int(float(s[:-1]))
        if s.endswith("m"):
            return int(float(s[:-1]) * 60)
        if s.endswith("h"):
            return int(float(s[:-1]) * 3600)
        if s.endswith("d"):
            return int(float(s[:-1]) * 86400)
        return int(float(s))
    except ValueError:
        return None


def describe_schedule(schedule: dict, tz_name: str | None = None) -> str:
    """Render a schedule dict as a short human-readable string for /rules
    and the /plans dry-run preview. Pure function — no I/O.
    """
    stype = (schedule.get("type") or "?").lower()
    tz_suffix = f" ({tz_name})" if tz_name else ""
    if stype == "daily":
        return f"every day at {schedule.get('at', '?')}{tz_suffix}"
    if stype == "weekly":
        days = schedule.get("days", [])
        days_str = ", ".join(days) if isinstance(days, list) else str(days)
        return f"every {days_str} at {schedule.get('at', '?')}{tz_suffix}"
    if stype == "interval":
        secs = schedule.get("seconds") or schedule.get("every_seconds") or "?"
        return f"every {secs} seconds"
    if stype == "once":
        return f"once at {schedule.get('at', '?')}{tz_suffix}"
    if stype == "cron":
        return f"cron {schedule.get('expr', '?')}{tz_suffix}"
    return f"<unknown schedule: {schedule!r}>"
