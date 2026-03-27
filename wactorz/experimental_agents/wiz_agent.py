"""
WizAgent — WaldiezCoin in-game economist (WIZ = Waldiez In-game Zeal).

Tracks the WaldiezCoin (Ƿ) economy: earns on agent activity, deducts on
QA flags and alerts. Publishes `system/coin` on every balance change.

Economy rules (via background MQTT subscription):
  agents/*/spawn       +10 Ƿ
  agents/*/heartbeat    +2 Ƿ
  system/health         +5 Ƿ  (all agents healthy)
  system/qa-flag        -5 Ƿ
  system/alert          -3 Ƿ

Commands (prefix @wiz-agent stripped):
  balance              current Ƿ balance
  history [n]          last n transactions (default 10)
  earn <n> [reason]    credit coins manually
  debit <n> [reason]   debit coins manually
  help                 show commands
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..core.actor import Actor, ActorState, Message, MessageType

logger = logging.getLogger(__name__)

_MAX_HISTORY = 200

_HELP = """\
**WIZ — WaldiezCoin Economist** Ƿ
_In-game economy for the Wactorz swarm_

```
balance              current Ƿ balance
history [n]          last n transactions (default 10)
earn <n> [reason]    credit n coins manually
debit <n> [reason]   debit n coins manually
help                 this message
```

**Auto-economy:**
+10 Ƿ agent spawn  ·  +2 Ƿ heartbeat  ·  +5 Ƿ healthy system
 −5 Ƿ QA flag  ·  −3 Ƿ stale alert"""


class WizAgent(Actor):
    """WaldiezCoin economist actor."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "wiz-agent")
        super().__init__(**kwargs)
        self.protected  = False
        self._balance: int          = 0
        self._history: list[dict]   = []

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self) -> None:
        self._balance = int(self.recall("balance", 0))
        self._history = list(self.recall("history", []))
        await self._mqtt_publish(
            f"agents/{self.actor_id}/spawn",
            {"agentId": self.actor_id, "agentName": self.name, "agentType": "coin", "timestamp": time.time()},
        )
        self._tasks.append(asyncio.create_task(self._economy_listener()))
        self._tasks.append(asyncio.create_task(self._coin_heartbeat()))
        logger.info("[%s] WizAgent started — balance: Ƿ %d", self.name, self._balance)

    async def on_stop(self) -> None:
        self._save()

    def _save(self) -> None:
        self.persist("balance", self._balance)
        self.persist("history", self._history)

    # ── Economy listener ───────────────────────────────────────────────────

    async def _economy_listener(self) -> None:
        try:
            import aiomqtt
        except ImportError:
            logger.warning("[%s] aiomqtt not available — economy listener disabled", self.name)
            return

        topics = ["agents/+/spawn", "agents/+/heartbeat", "system/health", "system/qa-flag", "system/alert"]

        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                async with aiomqtt.Client(self._mqtt_broker, self._mqtt_port) as client:
                    for t in topics:
                        await client.subscribe(t)
                    async for message in client.messages:
                        if self.state in (ActorState.STOPPED, ActorState.FAILED):
                            break
                        topic = str(message.topic)
                        try:
                            payload = json.loads(message.payload.decode())
                        except Exception:
                            payload = {}
                        self._handle_event(topic, payload)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self.state not in (ActorState.STOPPED, ActorState.FAILED):
                    await asyncio.sleep(5)

    def _handle_event(self, topic: str, payload: Any) -> None:
        parts = topic.split("/")
        if len(parts) == 3 and parts[0] == "agents" and parts[2] == "spawn":
            if isinstance(payload, dict) and payload.get("agentId") == self.actor_id:
                return  # skip own spawn
            name = payload.get("agentName", "agent") if isinstance(payload, dict) else "agent"
            self._apply(10, f"Agent spawned: {name}")
        elif len(parts) == 3 and parts[0] == "agents" and parts[2] == "heartbeat":
            if isinstance(payload, dict) and payload.get("actor_id") == self.actor_id:
                return
            self._apply(2, "Heartbeat received")
        elif topic == "system/health":
            if isinstance(payload, dict) and payload.get("failed", 0) == 0 and payload.get("stopped", 0) == 0:
                self._apply(5, "System health OK")
        elif topic == "system/qa-flag":
            self._apply(-5, "QA flag raised")
        elif topic == "system/alert":
            self._apply(-3, "Alert received")

    def _apply(self, delta: int, reason: str) -> None:
        self._balance += delta
        self._history.append({"delta": delta, "reason": reason, "balance": self._balance, "timestamp": time.time()})
        if len(self._history) > _MAX_HISTORY:
            self._history = self._history[-_MAX_HISTORY:]
        self._save()
        asyncio.create_task(self._publish_coin(delta, reason))

    async def _publish_coin(self, delta: int, reason: str) -> None:
        await self._mqtt_publish(
            "system/coin",
            {
                "balance":   self._balance,
                "event":     "earn" if delta >= 0 else "debit",
                "amount":    abs(delta),
                "reason":    reason,
                "timestamp": time.time(),
            },
        )

    async def _coin_heartbeat(self, interval: float = 10.0) -> None:
        await asyncio.sleep(1.5)
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                await self._publish_coin(0, "heartbeat")
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    # ── handle_message ─────────────────────────────────────────────────────

    async def handle_message(self, msg: Message) -> None:
        if msg.type not in (MessageType.TASK, MessageType.RESULT):
            return
        payload = msg.payload or {}
        text = str(
            payload.get("text") or payload.get("content") or payload.get("task") or ""
            if isinstance(payload, dict) else payload
        ).strip()
        if not text:
            return
        reply = self._dispatch(text)
        if reply:
            await self._reply(reply)

    async def _reply(self, content: str) -> None:
        await self._mqtt_publish(
            f"agents/{self.actor_id}/chat",
            {"from": self.name, "to": "user", "content": content, "timestamp": time.time()},
        )

    def _dispatch(self, raw: str) -> str | None:
        text = raw
        for pfx in ("@wiz-agent", "@wiz_agent"):
            if text.lower().startswith(pfx):
                text = text[len(pfx):].lstrip()
                break
        parts = text.split()
        if not parts:
            return _HELP
        cmd, args = parts[0].lower(), parts[1:]
        if cmd == "balance":
            return self._cmd_balance()
        if cmd == "history":
            n = int(args[0]) if args and args[0].isdigit() else 10
            return self._cmd_history(n)
        if cmd == "earn":
            return self._cmd_earn(args)
        if cmd == "debit":
            return self._cmd_debit(args)
        if cmd in ("help", ""):
            return _HELP
        return f"Unknown command: `{cmd}`. Type `help`."

    def _cmd_balance(self) -> str:
        sign = "+" if self._balance >= 0 else ""
        return (
            f"**Ƿ WaldiezCoin Balance**\n\nCurrent: **{sign}Ƿ {self._balance}**\n\n"
            f"_Earn: spawn +10 · heartbeat +2 · healthy +5_\n"
            f"_Lose: QA flag −5 · alert −3_"
        )

    def _cmd_history(self, n: int) -> str:
        if not self._history:
            return "📭 No coin history yet."
        n    = max(1, min(n, 50, len(self._history)))
        rows = []
        for e in reversed(self._history[-n:]):
            sign = "+" if e["delta"] >= 0 else ""
            ts   = e["timestamp"]
            t    = f"{int(ts//3600)%24:02d}:{int(ts//60)%60:02d}:{int(ts)%60:02d}"
            rows.append(f"  `{t}` {sign}{e['delta']} Ƿ — {e['reason']} (bal: {e['balance']})")
        return f"**Ƿ Coin History** (last {n})\n\n" + "\n".join(rows) + f"\n\n**Balance: Ƿ {self._balance}**"

    def _cmd_earn(self, args: list[str]) -> str:
        if not args:
            return "Usage: `earn <amount> [reason]`"
        try:
            amount = int(args[0])
            if amount <= 0:
                return "Amount must be positive."
        except ValueError:
            return f"Invalid: `{args[0]}`"
        reason = " ".join(args[1:]) or "manual earn"
        self._apply(amount, reason)
        return f"✅ Earned **Ƿ {amount}** — {reason}\n\n**New balance: Ƿ {self._balance}**"

    def _cmd_debit(self, args: list[str]) -> str:
        if not args:
            return "Usage: `debit <amount> [reason]`"
        try:
            amount = int(args[0])
            if amount <= 0:
                return "Amount must be positive."
        except ValueError:
            return f"Invalid: `{args[0]}`"
        reason = " ".join(args[1:]) or "manual debit"
        self._apply(-amount, reason)
        return f"📉 Debited **Ƿ {amount}** — {reason}\n\n**New balance: Ƿ {self._balance}**"

    def _current_task_description(self) -> str:
        return f"coin economist — Ƿ {self._balance}"
