"""
WifAgent — Finance expert (WIF = Waldiez In Finance).

In-memory finance tracker with calculations. No external API key required.
All state persisted via Actor pickle mechanism.

Commands (prefix @wif-agent stripped automatically):
  add <amount> [category] [note]          log an expense
  budget <amount>                         set monthly budget
  report                                  breakdown by category
  balance                                 budget vs total spent
  compound <principal> <rate%> <years>    compound interest (monthly)
  loan <principal> <rate%> <months>       monthly loan payment
  roi <investment> <return>               ROI percentage
  tax <income> [bracket%]                 simple tax estimate (default 25%)
  tip <bill> [percent]                    tip calculator (default 15%)
  help                                    list all commands
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..core.actor import Actor, Message, MessageType

logger = logging.getLogger(__name__)

_HELP = """\
**WIF — Finance Expert** 💹
_Waldiez In Finance_

```
add <amount> [category] [note]          log an expense
budget <amount>                         set monthly budget limit
report                                  spending breakdown by category
balance                                 budget vs total spent
compound <principal> <rate%> <years>    compound interest (monthly)
loan <principal> <rate%> <months>       monthly loan payment
roi <investment> <return>               return on investment %
tax <income> [bracket%]                 simple tax estimate (default 25%)
tip <bill> [percent]                    tip calculator (default 15%)
help                                    this message
```"""


class WifAgent(Actor):
    """Finance-expert actor — no API key, all calculations local."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "wif-agent")
        super().__init__(**kwargs)
        self.protected  = False
        self._expenses: list[dict] = []
        self._budget: float        = 0.0

    async def on_start(self) -> None:
        self._expenses = self.recall("expenses", [])
        self._budget   = float(self.recall("budget", 0.0))
        await self._mqtt_publish(
            f"agents/{self.actor_id}/spawn",
            {"agentId": self.actor_id, "agentName": self.name, "agentType": "financier", "timestamp": time.time()},
        )
        logger.info("[%s] started — %d expenses loaded", self.name, len(self._expenses))

    async def on_stop(self) -> None:
        self._save()

    def _save(self) -> None:
        self.persist("expenses", self._expenses)
        self.persist("budget", self._budget)

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
        self._save()
        await self._reply(reply)

    async def _reply(self, content: str) -> None:
        await self._mqtt_publish(
            f"agents/{self.actor_id}/chat",
            {"from": self.name, "to": "user", "content": content, "timestamp": time.time()},
        )

    def _dispatch(self, raw: str) -> str:
        text = raw
        for pfx in ("@wif-agent", "@wif_agent"):
            if text.lower().startswith(pfx):
                text = text[len(pfx):].lstrip()
                break
        parts = text.split()
        if not parts:
            return _HELP
        cmd, args = parts[0].lower(), parts[1:]
        return {
            "add":      lambda: self._cmd_add(args),
            "budget":   lambda: self._cmd_budget(args),
            "report":   lambda: self._cmd_report(),
            "balance":  lambda: self._cmd_balance(),
            "compound": lambda: self._cmd_compound(args),
            "loan":     lambda: self._cmd_loan(args),
            "roi":      lambda: self._cmd_roi(args),
            "tax":      lambda: self._cmd_tax(args),
            "tip":      lambda: self._cmd_tip(args),
        }.get(cmd, lambda: _HELP if cmd in ("help", "") else f"Unknown command: `{cmd}`. Type `help`.")()

    # ── add ────────────────────────────────────────────────────────────────
    def _cmd_add(self, args: list[str]) -> str:
        if not args:
            return "Usage: `add <amount> [category] [note]`"
        try:
            amount = float(args[0].lstrip("$€£¥+"))
            if amount <= 0:
                return "Amount must be positive."
        except ValueError:
            return f"Invalid amount: `{args[0]}`"
        category = args[1].lower() if len(args) > 1 else "misc"
        note     = " ".join(args[2:]) if len(args) > 2 else ""
        self._expenses.append({"amount": amount, "category": category, "note": note, "timestamp": time.time()})
        header = f"Logged **${amount:.2f}** → `{category}`" + (f" — _{note}_" if note else "")
        if self._budget > 0:
            spent = sum(e["amount"] for e in self._expenses)
            pct   = min(spent / self._budget * 100, 999)
            icon  = "🚨" if pct >= 100 else ("⚠" if pct >= 80 else "✓")
            return f"{header}\n{icon} Budget: ${spent:.2f} / ${self._budget:.2f} ({pct:.0f}%)"
        return header

    # ── budget ─────────────────────────────────────────────────────────────
    def _cmd_budget(self, args: list[str]) -> str:
        if not args:
            return "Usage: `budget <amount>`"
        try:
            amount = float(args[0].lstrip("$€£¥"))
        except ValueError:
            return f"Invalid amount: `{args[0]}`"
        self._budget = amount
        spent = sum(e["amount"] for e in self._expenses)
        pct   = min(spent / amount * 100, 999) if amount > 0 else 0
        return f"Budget set: **${amount:.2f}** | Spent so far: ${spent:.2f} ({pct:.0f}%)"

    # ── report ─────────────────────────────────────────────────────────────
    def _cmd_report(self) -> str:
        if not self._expenses:
            return "No expenses yet. Try: `add 25 food coffee`"
        by_cat: dict[str, float] = {}
        for e in self._expenses:
            by_cat[e["category"]] = by_cat.get(e["category"], 0) + e["amount"]
        total = sum(by_cat.values())
        rows  = [f"  **{cat}**: ${amt:.2f} ({amt/total*100:.0f}%)" for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1])]
        return f"**Expense Report** ({len(self._expenses)} transactions)\n\n" + "\n".join(rows) + f"\n\n**Total: ${total:.2f}**"

    # ── balance ────────────────────────────────────────────────────────────
    def _cmd_balance(self) -> str:
        spent = sum(e["amount"] for e in self._expenses)
        if self._budget <= 0:
            return f"**Balance**\n\nTotal spent: **${spent:.2f}** ({len(self._expenses)} transactions)\nBudget: _not set_"
        rem  = self._budget - spent
        pct  = min(spent / self._budget * 100, 999)
        icon = "🚨" if pct >= 100 else ("⚠" if pct >= 80 else "✓")
        return (
            f"**Monthly Budget Balance**\n\n"
            f"{icon} ${spent:.2f} / ${self._budget:.2f} ({pct:.0f}%)\n"
            f"{'${:.2f} left'.format(rem) if rem >= 0 else '${:.2f} over budget'.format(-rem)}"
        )

    # ── compound ───────────────────────────────────────────────────────────
    def _cmd_compound(self, args: list[str]) -> str:
        if len(args) < 3:
            return "Usage: `compound <principal> <rate%> <years>`  e.g. `compound 10000 7 20`"
        try:
            p, r, t = float(args[0].lstrip("$")), float(args[1].rstrip("%")) / 100, float(args[2])
        except ValueError:
            return "Invalid numbers."
        n  = 12.0
        fv = p * (1 + r / n) ** (n * t)
        return f"**Compound Interest**\n\nPrincipal: ${p:,.2f} | Rate: {r*100:.2f}% | Term: {t:.0f}y\n→ **Future Value: ${fv:,.2f}** (interest: ${fv-p:,.2f})"

    # ── loan ───────────────────────────────────────────────────────────────
    def _cmd_loan(self, args: list[str]) -> str:
        if len(args) < 3:
            return "Usage: `loan <principal> <rate%> <months>`  e.g. `loan 20000 5.5 60`"
        try:
            p, annual, n = float(args[0].lstrip("$")), float(args[1].rstrip("%")), float(args[2])
        except ValueError:
            return "Invalid numbers."
        r = annual / 100 / 12
        monthly = (p * r * (1 + r) ** n / ((1 + r) ** n - 1)) if r else p / n
        total   = monthly * n
        return f"**Loan Calculator**\n\nPrincipal: ${p:,.2f} | Rate: {annual:.2f}% | Term: {n:.0f}mo\n→ **Monthly: ${monthly:,.2f}** | Total: ${total:,.2f} | Interest: ${total-p:,.2f}"

    # ── roi ────────────────────────────────────────────────────────────────
    def _cmd_roi(self, args: list[str]) -> str:
        if len(args) < 2:
            return "Usage: `roi <investment> <return>`  e.g. `roi 5000 7500`"
        try:
            inv, ret = float(args[0].lstrip("$")), float(args[1].lstrip("$"))
        except ValueError:
            return "Invalid numbers."
        if inv == 0:
            return "Investment cannot be zero."
        gain = ret - inv
        roi  = gain / inv * 100
        return f"**ROI**\n\nInvestment: ${inv:,.2f} | Return: ${ret:,.2f}\n→ Gain: ${gain:+,.2f} | **ROI: {roi:+.2f}%**"

    # ── tax ────────────────────────────────────────────────────────────────
    def _cmd_tax(self, args: list[str]) -> str:
        if not args:
            return "Usage: `tax <income> [bracket%]`  e.g. `tax 75000 25`"
        try:
            income = float(args[0].lstrip("$"))
            rate   = float(args[1].rstrip("%")) if len(args) > 1 else 25.0
        except ValueError:
            return "Invalid numbers."
        tax = income * rate / 100
        return f"**Tax Estimate**\n\nIncome: ${income:,.2f} | Rate: {rate:.1f}%\n→ Tax: **${tax:,.2f}** | Net: **${income-tax:,.2f}**\n_Simplified estimate — consult a professional._"

    # ── tip ────────────────────────────────────────────────────────────────
    def _cmd_tip(self, args: list[str]) -> str:
        if not args:
            return "Usage: `tip <bill> [percent]`  e.g. `tip 45 20`"
        try:
            bill = float(args[0].lstrip("$"))
            pct  = float(args[1].rstrip("%")) if len(args) > 1 else 15.0
        except ValueError:
            return "Invalid numbers."
        tip   = bill * pct / 100
        total = bill + tip
        splits = "\n".join(f"  {n} people: ${total/n:.2f}/person" for n in (1, 2, 3, 4, 5))
        return f"**Tip Calculator**\n\nBill: ${bill:.2f} | Tip ({pct:.0f}%): ${tip:.2f} | **Total: ${total:.2f}**\n\n{splits}"

    def _current_task_description(self) -> str:
        return f"financier — {len(self._expenses)} expenses"
