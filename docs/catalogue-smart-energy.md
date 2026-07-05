# Smart Energy agent

`smart-energy` — an LLM-powered energy helper for smart plugs via Home Assistant
(brand-agnostic). It monitors live wattage, tracks per-plug kWh and cost (today/week/month),
and can power plugs down through **explicit, guarded rules**.

Requires a configured [Home Assistant](../guide/agents.html) connection.

## Spawn

```text
@catalog spawn smart-energy
```

## Usage

Plain English is the primary interface:

```text
import my plugs            # scans HA, shows what it found, asks which to monitor
how much did the AC cost today?
status                     # overview: plugs monitored, live watts
turn off the heater if it's idle for 10 minutes
```

`import my plugs` starts a conversational onboarding — no JSON needed.

## Safety

Plugs marked **locked** (e.g. AC, servers, AI rigs) are **never** turned off — this is
hard-guarded in code, not just prompt guidance. Power-down only happens via rules you add
explicitly.

## Configuration

| Env | Meaning |
|-----|---------|
| `ENERGY_RATE` | default €/kWh used for cost (default `0.138`; also settable at runtime with "set rate to 0.25") |
| `ENERGY_CURRENCY` | display currency (default `EUR`) |

## Structured operations

For power users, `action` accepts: `status`, `cost`, `report`, `add_plug`, `list_plugs`,
`remove_plug`, `add_rule`, `list_rules`, `remove_rule`, `set_rate` (with `plug`, `rule`, or
`rate` payloads). Returns `plugs_monitored`, `active_rules`, and `total_watts`.
