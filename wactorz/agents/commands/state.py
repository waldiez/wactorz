"""Commands that change what main remembers or has agreed to do.

Grouped apart from the read-only reports because each of these has an effect:
memory is wiped, a rule stops running, a proposal is approved into agents that
then exist. Nothing here decides whether the caller is allowed to — a social
channel never reaches this package at all.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from ..helpers.main_actor_helpers import PENDING_PLANS_KEY
from .dispatch import CommandContext, command

logger = logging.getLogger(__name__)


@command(
    "/memory",
    exact=("/memory",),
    prefixes=("/memory ",),
    summary="show or clear stored user facts and summary",
)
async def manage_memory(ctx: CommandContext, argument: str) -> str:
    """Show or clear stored user facts and summary."""
    sub = argument
    if sub == "clear":
        ctx.actor.persist("_user_facts", {})
        ctx.actor.persist("history_summary", "")
        ctx.actor._history_summary = ""
        # Rebuild fresh — running agents block is preserved, facts are gone
        ctx.actor._rebuild_system_prompt()
        return "Memory cleared — user facts and conversation summary reset."
    if sub.startswith("forget "):
        key = sub[7:].strip()
        facts = ctx.actor.get_user_facts()
        if key in facts:
            del facts[key]
            ctx.actor.persist("_user_facts", facts)
            ctx.actor._inject_user_facts_into_prompt()
            return f"Forgotten: '{key}'"
        return f"No fact found with key '{key}'."
    # Default: show memory, grouped by bucket so it's easy to scan
    facts = ctx.actor.get_user_facts()
    summary = ctx.actor._history_summary
    lines = []
    if facts:
        lines.append(f"User facts ({len(facts)}):")
        buckets = [
            ("pref_", "Preferences & identity"),
            ("device_", "Devices & setup"),
            ("policy_", "Standing policies"),
        ]
        shown = set()
        for prefix, heading in buckets:
            items = [(k, v) for k, v in facts.items() if k.startswith(prefix)]
            if items:
                lines.append(f"\n  [{heading}]")
                for k, v in sorted(items):
                    lines.append(f"    {k[len(prefix) :]}: {v}")
                    shown.add(k)
        # Anything left over (legacy or unprefixed keys)
        leftover = [(k, v) for k, v in facts.items() if k not in shown]
        if leftover:
            lines.append("\n  [Other / legacy]")
            for k, v in sorted(leftover):
                lines.append(f"    {k}: {v}")
    else:
        lines.append("No user facts stored yet.")
    if summary:
        lines.append(
            f"\nConversation summary:\n  {summary[:300]}{'...' if len(summary) > 300 else ''}"
        )
    else:
        lines.append("\nNo conversation summary yet.")
    lines.append("\nCommands: /memory clear | /memory forget <key>")
    return "\n".join(lines)


@command(
    "/rules",
    exact=("/rules", "rules"),
    summary="list active pipeline rules",
)
async def show_rules(ctx: CommandContext, _argument: str) -> str:
    """List active pipeline rules."""
    rules = ctx.actor.get_pipeline_rules()
    if not rules:
        return "No pipeline rules active.\nDescribe a reactive rule to create one, e.g. 'when the door opens send me a Discord message'."
    lines = [f"Active pipeline rules ({len(rules)}):"]
    for rule_id, rule in sorted(rules.items(), key=lambda x: x[1].get("created_at", 0)):
        agents = rule.get("agents", [])
        task = rule.get("task", "")[:]
        ts = rule.get("created_at", 0)
        created = (
            datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
            if ts
            else "unknown"
        )
        # Check which agents are running
        running_agents = []
        stopped_agents = []
        for a in agents:
            if ctx.actor._registry and ctx.actor._registry.find_by_name(a):
                running_agents.append(a)
            else:
                stopped_agents.append(a)
        status = "🟢" if running_agents else "🔴"
        lines.append(f"\n{status} [{rule_id}] — {task}")
        lines.append(f"   agents  : {', '.join(agents)}")
        if stopped_agents:
            lines.append(f"   stopped : {', '.join(stopped_agents)}")
        lines.append(f"   created : {created}")
    lines.append("\nTo delete a rule: /rules delete <rule_id>")
    return "\n".join(lines)


@command(
    "/rules delete",
    prefixes=("/rules delete ",),
    summary="stop agents and remove a rule",
)
async def delete_rule(ctx: CommandContext, argument: str) -> str:
    """Stop agents and remove a rule."""
    return await ctx.actor.delete_pipeline_rule(argument)


@command(
    "/clear-plans",
    exact=("/clear-plans",),
    summary="clear the plan cache",
)
async def clear_plans(ctx: CommandContext, _argument: str) -> str:
    """Clear the plan cache."""
    try:
        ctx.actor.persist("_plan_cache", {})
    except Exception as exc:
        logger.exception("[main] /clear-plans failed")
        return f"Failed to clear plan cache: {exc}"
    return "Plan cache cleared."


@command(
    "/plans",
    exact=("/plans",),
    prefixes=("/plans ",),
    summary="list, inspect, approve or reject pipeline proposals",
)
async def manage_plans(ctx: CommandContext, argument: str) -> str:
    """List, inspect, approve or reject pipeline proposals."""
    parts = argument.split(None, 1)
    sub = parts[0] if parts else ""
    target = parts[1].strip() if len(parts) > 1 else ""

    # /plans show <id>
    if sub == "show" and target:
        pid = target
        p = ctx.actor.get_pending_plans().get(pid)
        if not p:
            return f"No plan with id `{pid}`."
        envelope = p.get("envelope", {})
        agents = envelope.get("plan", [])
        lines = [ctx.actor._format_plan_proposal(p), "", "**Full agent code:**"]
        for step in agents:
            name = step.get("name", "?")
            code = step.get("spawn_config", {}).get("code", "") or "(no code — pre-built type)"
            lines.append(f"\n--- {name} ---")
            lines.append("```python")
            lines.append(code[:2000])
            if len(code) > 2000:
                lines.append(f"... ({len(code) - 2000} more chars truncated)")
            lines.append("```")
        return "\n".join(lines)

    # /plans approve <id>
    if sub == "approve" and target:
        pid = target
        p = ctx.actor.get_pending_plans().get(pid)
        if not p:
            return f"No plan with id `{pid}`."
        if p.get("status") != "pending":
            return f"Plan `{pid}` is `{p.get('status')}`, not pending."
        return await ctx.actor._execute_pending_plan(p)

    # /plans reject <id>
    if sub == "reject" and target:
        pid = target
        p = ctx.actor.get_pending_plans().get(pid)
        if not p:
            return f"No plan with id `{pid}`."
        if p.get("status") != "pending":
            return f"Plan `{pid}` is `{p.get('status')}`, not pending."
        return ctx.actor._reject_pending_plan(p)

    # /plans clear — drop all non-pending plans (housekeeping)
    if sub == "clear":
        plans = ctx.actor.recall(PENDING_PLANS_KEY) or {}
        kept = {pid: p for pid, p in plans.items() if p.get("status") == "pending"}
        dropped = len(plans) - len(kept)
        ctx.actor.persist(PENDING_PLANS_KEY, kept)
        return f"Cleared {dropped} resolved plan(s). {len(kept)} still pending."

    # /plans (no args) — list
    plans = ctx.actor.get_pending_plans()
    pending = [p for p in plans.values() if p.get("status") == "pending"]
    resolved = [p for p in plans.values() if p.get("status") != "pending"]
    lines = []
    if pending:
        lines.append(f"**Pending plans ({len(pending)})** — awaiting your approval")
        for p in sorted(pending, key=lambda x: -x.get("created_at", 0)):
            pid = p.get("plan_id", "?")
            task = p.get("task", "?")[:60]
            n_agents = len(p.get("envelope", {}).get("plan", []))
            age_s = int(time.time() - p.get("created_at", 0))
            lines.append(f"  `{pid}` ({n_agents} agent(s), {age_s}s ago) — {task}")
        lines.append("\n  /plans show <id>      — see full plan with code")
        lines.append("  /plans approve <id>   — execute the plan")
        lines.append("  /plans reject <id>    — discard the plan")
    else:
        lines.append("No pending plans.")
    if resolved:
        lines.append(f"\n_Recent resolved plans ({len(resolved)})_:")
        for p in sorted(resolved, key=lambda x: -x.get("created_at", 0))[:5]:
            pid = p.get("plan_id", "?")
            status = p.get("status", "?")
            task = p.get("task", "?")[:50]
            icon = {
                "approved": "\u2705",
                "rejected": "\u274c",
                "expired": "\u23f0",
                "superseded": "\u21bb",
            }.get(status, "?")
            lines.append(f"  {icon} `{pid}` ({status}) — {task}")
        lines.append("\n  /plans clear          — drop resolved entries")
    return "\n".join(lines)


@command(
    "/webhook",
    exact=("/webhook",),
    prefixes=("/webhook ",),
    summary="list or store notification webhook URLs",
)
async def manage_webhooks(ctx: CommandContext, argument: str) -> str:
    """List or store notification webhook URLs."""
    parts = argument.split(None, 1)
    if not parts:
        # /webhook — show stored URLs
        urls = ctx.actor.recall("_notification_urls") or {}
        if not urls:
            return "No notification URLs stored.\nUse: /webhook discord <url>  or  /webhook telegram <url>"
        lines = ["Stored notification URLs:"]
        for svc, url in urls.items():
            lines.append(f"  {svc}: {url}")
        return "\n".join(lines)
    if len(parts) >= 2:
        # /webhook discord <url>
        service = parts[0].lower()
        url = parts[1].strip()
        urls = ctx.actor.recall("_notification_urls") or {}
        urls[service] = url
        ctx.actor.persist("_notification_urls", urls)
        return f"Saved {service} webhook URL. Pipelines will use it automatically."
    return "Usage: /webhook <service> <url>\nExample: /webhook discord https://discord.com/api/webhooks/..."
