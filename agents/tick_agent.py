"""
TickAgent — scheduler / timer (NATO: CHRON / Tango).

Schedule one-shot messages or recurring reminders without any external
cron daemon. All timers live in-process; they survive restarts only if
persisted (pickle via Actor.persist/recall).

Commands (prefix @chron-agent or @tick-agent stripped automatically):
  at <HH:MM> <message>               fire once at clock time (today / tomorrow)
  in <n> <unit> <message>            fire after a delay  (unit: s|sec|m|min|h|hr|d|day)
  every <n> <unit> <message>         recurring timer     (unit: m|min|h|hr|d|day)
  list                               show all pending timers
  cancel <id>                        cancel a timer by ID
  clear                              cancel all timers
  help                               show commands
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from ..core.actor import Actor, ActorState, Message, MessageType

logger = logging.getLogger(__name__)

_HELP = """\
**CHRON — TickAgent** ⏱
_In-process scheduler — no external cron needed_

```
at <HH:MM> <message>             fire once at clock time today/tomorrow
in <n> <unit> <message>          fire after delay (s/m/h/d)
every <n> <unit> <message>       recurring (m/h/d)
list                             show pending timers
cancel <id>                      cancel timer by ID prefix
clear                            cancel all timers
help                             this message
```

**Examples:**
```
in 5 m check the oven
at 09:00 Good morning, team!
every 1 h system check
```"""


def _parse_seconds(n_str: str, unit: str) -> Optional[float]:
    """Parse a number + unit into seconds. Returns None on failure."""
    try:
        n = float(n_str)
    except ValueError:
        return None
    u = unit.lower().rstrip("s")  # normalize plural
    mapping = {
        "s": 1, "sec": 1, "second": 1,
        "m": 60, "min": 60, "minute": 60,
        "h": 3600, "hr": 3600, "hour": 3600,
        "d": 86400, "day": 86400,
    }
    factor = mapping.get(u)
    if factor is None:
        return None
    return n * factor


def _parse_hhmm(s: str) -> Optional[float]:
    """Return Unix timestamp for the next occurrence of HH:MM today or tomorrow."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s.strip())
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2))
    if not (0 <= h < 24 and 0 <= mn < 60):
        return None
    import datetime
    now = datetime.datetime.now()
    target = now.replace(hour=h, minute=mn, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return target.timestamp()


@dataclass
class Timer:
    id: str
    message: str
    fire_at: float        # Unix timestamp of next fire
    interval: float = 0.0  # 0 → one-shot; >0 → recurring every `interval` seconds
    created_at: float = field(default_factory=time.time)

    def label(self) -> str:
        """Human-readable next-fire description."""
        remaining = self.fire_at - time.time()
        if remaining <= 0:
            return "overdue"
        if remaining < 60:
            return f"in {remaining:.0f}s"
        if remaining < 3600:
            return f"in {remaining / 60:.0f}m"
        if remaining < 86400:
            return f"in {remaining / 3600:.1f}h"
        return f"in {remaining / 86400:.1f}d"

    def kind(self) -> str:
        return "every" if self.interval > 0 else "once"


class TickAgent(Actor):
    """In-process scheduler agent."""

    _TICK_INTERVAL = 5.0  # seconds between scheduler checks

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "chron-agent")
        super().__init__(**kwargs)
        self.protected = False
        self._timers: dict[str, Timer] = {}
        self._lock = asyncio.Lock()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def on_start(self):
        # Restore timers from pickle (prune stale one-shots)
        saved = self.recall("timers", {})
        now = time.time()
        for tid, t in saved.items():
            if isinstance(t, dict):
                timer = Timer(**t)
                if timer.interval > 0 or timer.fire_at > now:
                    # Advance recurring timers past current time
                    while timer.interval > 0 and timer.fire_at <= now:
                        timer.fire_at += timer.interval
                    self._timers[tid] = timer

        await self._mqtt_publish(
            f"agents/{self.actor_id}/spawn",
            {
                "agentId":   self.actor_id,
                "agentName": self.name,
                "agentType": "scheduler",
                "timestamp": time.time(),
            },
        )
        self._tasks.append(asyncio.create_task(self._scheduler_loop()))
        logger.info("[%s] started — %d timers restored", self.name, len(self._timers))

    async def on_stop(self):
        self._save()

    def _save(self):
        self.persist("timers", {tid: vars(t) for tid, t in self._timers.items()})

    # ── Scheduler loop ─────────────────────────────────────────────────────────

    async def _scheduler_loop(self):
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                await asyncio.sleep(self._TICK_INTERVAL)
                await self._fire_due()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[%s] scheduler error: %s", self.name, exc)

    async def _fire_due(self):
        now = time.time()
        to_delete = []
        async with self._lock:
            for tid, timer in list(self._timers.items()):
                if timer.fire_at <= now:
                    await self._fire(timer)
                    if timer.interval > 0:
                        # advance to next interval
                        while timer.fire_at <= now:
                            timer.fire_at += timer.interval
                    else:
                        to_delete.append(tid)
            for tid in to_delete:
                del self._timers[tid]
        if to_delete:
            self._save()

    async def _fire(self, timer: Timer):
        short = timer.id[:8]
        content = f"⏰ **Timer `{short}…`** fired!\n\n{timer.message}"
        await self._reply(content)
        logger.info("[%s] timer %s fired: %s", self.name, timer.id[:8], timer.message)

    # ── handle_message ─────────────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        if msg.type not in (MessageType.TASK, MessageType.RESULT):
            return
        payload = msg.payload or {}
        text = str(
            payload.get("text") or payload.get("content") or payload.get("task") or ""
            if isinstance(payload, dict) else payload
        ).strip()
        if not text:
            return
        for pfx in ("@chron-agent", "@chron_agent", "@tick-agent", "@tick_agent"):
            if text.lower().startswith(pfx):
                text = text[len(pfx):].lstrip()
                break
        reply = await self._dispatch(text.strip())
        if reply:
            await self._reply(reply)

    async def _dispatch(self, text: str) -> Optional[str]:
        if not text or text.lower() == "help":
            return _HELP
        parts = text.split()
        cmd = parts[0].lower()

        if cmd == "list":
            return await self._cmd_list()
        if cmd == "clear":
            return await self._cmd_clear()
        if cmd in ("cancel", "del", "delete", "rm", "remove"):
            return await self._cmd_cancel(parts[1] if len(parts) > 1 else "")
        if cmd == "at":
            if len(parts) < 3:
                return "Usage: `at <HH:MM> <message>`"
            return await self._cmd_at(parts[1], " ".join(parts[2:]))
        if cmd == "in":
            if len(parts) < 4:
                return "Usage: `in <n> <unit> <message>`  e.g. `in 5 m check the oven`"
            return await self._cmd_in(parts[1], parts[2], " ".join(parts[3:]))
        if cmd == "every":
            if len(parts) < 4:
                return "Usage: `every <n> <unit> <message>`  e.g. `every 1 h status check`"
            return await self._cmd_every(parts[1], parts[2], " ".join(parts[3:]))
        return f"Unknown command: `{cmd}`. Type `help`."

    async def _cmd_at(self, time_str: str, message: str) -> str:
        fire_at = _parse_hhmm(time_str)
        if fire_at is None:
            return f"Invalid time `{time_str}`. Use HH:MM (24-h), e.g. `14:30`."
        tid = str(uuid.uuid4())
        timer = Timer(id=tid, message=message, fire_at=fire_at)
        async with self._lock:
            self._timers[tid] = timer
        self._save()
        remaining = fire_at - time.time()
        h, rem = divmod(int(remaining), 3600)
        m, s = divmod(rem, 60)
        label = f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"
        return (
            f"✓ Timer `{tid[:8]}…` set for **{time_str}** (in {label}).\n\n"
            f"Message: _{message}_"
        )

    async def _cmd_in(self, n_str: str, unit: str, message: str) -> str:
        secs = _parse_seconds(n_str, unit)
        if secs is None or secs <= 0:
            return f"Invalid delay `{n_str} {unit}`. Use e.g. `5 m`, `2 h`, `30 s`."
        tid = str(uuid.uuid4())
        fire_at = time.time() + secs
        timer = Timer(id=tid, message=message, fire_at=fire_at)
        async with self._lock:
            self._timers[tid] = timer
        self._save()
        return (
            f"✓ Timer `{tid[:8]}…` — fires in **{n_str} {unit}**.\n\n"
            f"Message: _{message}_"
        )

    async def _cmd_every(self, n_str: str, unit: str, message: str) -> str:
        secs = _parse_seconds(n_str, unit)
        if secs is None or secs < 60:
            return f"Invalid interval `{n_str} {unit}`. Minimum is 1 minute."
        tid = str(uuid.uuid4())
        fire_at = time.time() + secs
        timer = Timer(id=tid, message=message, fire_at=fire_at, interval=secs)
        async with self._lock:
            self._timers[tid] = timer
        self._save()
        return (
            f"✓ Recurring timer `{tid[:8]}…` — every **{n_str} {unit}**.\n\n"
            f"Message: _{message}_"
        )

    async def _cmd_list(self) -> str:
        async with self._lock:
            timers = list(self._timers.values())
        if not timers:
            return "No active timers. Use `in`, `at`, or `every` to schedule one."
        timers.sort(key=lambda t: t.fire_at)
        lines = [f"**Active Timers ({len(timers)}):**\n"]
        for t in timers:
            lines.append(
                f"- `{t.id[:8]}…` [{t.kind()}] {t.label()} — _{t.message[:60]}_"
            )
        return "\n".join(lines)

    async def _cmd_cancel(self, prefix: str) -> str:
        if not prefix:
            return "Usage: `cancel <id-prefix>`  (use `list` to see IDs)"
        async with self._lock:
            matches = [tid for tid in self._timers if tid.startswith(prefix)]
            if not matches:
                return f"No timer found matching `{prefix}`."
            if len(matches) > 1:
                return f"Ambiguous prefix `{prefix}` matches {len(matches)} timers. Be more specific."
            del self._timers[matches[0]]
        self._save()
        return f"✓ Timer `{prefix}…` cancelled."

    async def _cmd_clear(self) -> str:
        async with self._lock:
            count = len(self._timers)
            self._timers.clear()
        self._save()
        return f"✓ Cleared {count} timer(s)."

    async def _reply(self, content: str):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/chat",
            {"from": self.name, "to": "user", "content": content, "timestamp": time.time()},
        )

    def _current_task_description(self) -> str:
        return f"scheduler — {len(self._timers)} active timer(s)"
