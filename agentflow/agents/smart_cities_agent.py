"""
SmartCitiesAgent — SYNAPSE integration for smart city sensors.

Subscribes to SYNAPSE's Fuseki triple store and MQTT bus to:
    • Monitor weapon detection observations and escalate high-confidence alerts
    • Track road user density and publish spatial summaries
    • Report parking occupancy changes
    • Answer natural-language queries about city state via LLM + SPARQL

MQTT topics consumed:
    synapse/smartcities/safety/alerts        — weapon alerts from MQTT bridge
    synapse/telemetry/+                      — telemetry from any sensor
    synapse/smartcities/+/+/rdf              — raw RDF from bridge

MQTT topics published:
    synapse/smartcities/status               — periodic city state summary
    synapse/smartcities/road-density         — road user count by zone
    synapse/alerts/weapon                    — weapon alert mirror
    agents/<id>/logs                         — agent log (via standard framework)

Environment:
    FUSEKI_URL       Fuseki base URL (default: http://fuseki:3030)
    FUSEKI_DATASET   Fuseki dataset (default: synapse)
    WEAPON_THRESHOLD Confidence threshold for CRITICAL alerts (default: 0.60)
    POLL_INTERVAL    Seconds between SPARQL polls (default: 30)
"""

from __future__ import annotations

import logging
import os
import time

from ..core.actor import Actor, Message, MessageType

logger = logging.getLogger(__name__)

_FUSEKI_URL      = os.getenv("FUSEKI_URL",      "http://fuseki:3030")
_FUSEKI_DATASET  = os.getenv("FUSEKI_DATASET",  "synapse")
_WEAPON_THRESH   = float(os.getenv("WEAPON_THRESHOLD", "0.60"))
_POLL_INTERVAL   = float(os.getenv("POLL_INTERVAL",    "30"))
_TIMEOUT         = 20

# ── Common SPARQL prefixes ────────────────────────────────────────────────────

_PREFIXES = """\
PREFIX rdf:     <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX sosa:    <http://www.w3.org/ns/sosa/>
PREFIX sc:      <https://diaedge.com/ont/smart-cities#>
PREFIX xsd:     <http://www.w3.org/2001/XMLSchema#>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX geo:     <http://www.opengis.net/ont/geosparql#>
PREFIX qudt:    <https://qudt.org/schema/qudt/>
"""

# ── Queries ───────────────────────────────────────────────────────────────────

# Recent weapon detections above threshold
_WEAPON_QUERY = """\
{prefixes}
SELECT ?obs ?sensor ?confidence ?ts ?wid
FROM <urn:synapse:events>
WHERE {{
  ?obs a sosa:Observation ;
       sosa:observedProperty sc:DetectionConfidence ;
       sosa:madeBySensor ?sensor ;
       sosa:resultTime ?ts ;
       sosa:hasSimpleResult ?confidence .
  OPTIONAL {{ ?obs dcterms:identifier ?wid }}
  FILTER (?confidence >= {threshold})
  FILTER (?ts > "{since}"^^xsd:dateTime)
}}
ORDER BY DESC(?confidence)
LIMIT 20
""".format(prefixes=_PREFIXES, threshold=_WEAPON_THRESH, since="{since}")

# Road user counts by sensor (latest via urn:synapse:current)
_ROAD_DENSITY_QUERY = """\
{prefixes}
SELECT ?sensor (COUNT(DISTINCT ?user) AS ?count)
FROM <urn:synapse:current>
WHERE {{
  ?obs a sosa:Observation ;
       sosa:observedProperty sc:ObjectDetection ;
       sosa:madeBySensor ?sensor ;
       sosa:hasFeatureOfInterest ?user .
  ?user a sc:RoadUser .
}}
GROUP BY ?sensor
ORDER BY DESC(?count)
""".format(prefixes=_PREFIXES)

# Parking occupancy rate (latest state)
_PARKING_QUERY = """\
{prefixes}
SELECT ?sensor
       (SUM(IF(?occupied = "true"^^xsd:boolean, 1, 0)) AS ?occupied)
       (COUNT(?space) AS ?total)
FROM <urn:synapse:current>
WHERE {{
  ?obs a sosa:Observation ;
       sosa:observedProperty sc:Occupancy ;
       sosa:madeBySensor ?sensor ;
       sosa:hasFeatureOfInterest ?space ;
       sosa:hasSimpleResult ?occupied .
}}
GROUP BY ?sensor
""".format(prefixes=_PREFIXES)

# NL query context: full description of current city state for LLM
_STATE_QUERY = """\
{prefixes}
SELECT ?type (COUNT(*) AS ?count)
FROM <urn:synapse:events>
WHERE {{
  ?obs a sosa:Observation ;
       sosa:observedProperty ?type .
}}
GROUP BY ?type
ORDER BY DESC(?count)
""".format(prefixes=_PREFIXES)


# ── HTTP SPARQL helper ────────────────────────────────────────────────────────

async def _sparql_select(sparql: str) -> list[dict]:
    """Execute a SPARQL SELECT and return the bindings list."""
    try:
        import aiohttp
    except ImportError:
        logger.warning("aiohttp not available — SPARQL queries disabled")
        return []

    base     = _FUSEKI_URL.rstrip("/")
    dataset  = _FUSEKI_DATASET.strip("/")
    endpoint = f"{base}/{dataset}/sparql"
    params   = {"query": sparql, "format": "json"}
    headers  = {"Accept": "application/sparql-results+json"}
    timeout  = aiohttp.ClientTimeout(total=_TIMEOUT)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as ses:
            async with ses.get(endpoint, params=params, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"SPARQL returned HTTP {resp.status}: {body[:200]}")
                    return []
                data = await resp.json(content_type=None)
                return data.get("results", {}).get("bindings", [])
    except Exception as e:
        logger.debug(f"SPARQL query failed: {e}")
        return []


def _val(binding: dict, var: str) -> str:
    return binding.get(var, {}).get("value", "")


# ── Main agent class ──────────────────────────────────────────────────────────

class SmartCitiesAgent(Actor):
    """
    AgentFlow actor that bridges SYNAPSE's Fuseki triple store with the
    agent messaging bus for smart city event monitoring and NL queries.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "smart-cities-agent")
        super().__init__(**kwargs)
        self.protected       = False
        self._last_poll      = 0.0
        self._seen_weapons:  set[str] = set()   # obs URIs already alerted
        self._weapon_thresh  = _WEAPON_THRESH
        self._poll_interval  = _POLL_INTERVAL

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self):
        import asyncio
        await self._mqtt_publish(
            f"agents/{self.actor_id}/spawn",
            {
                "agentId":   self.actor_id,
                "agentName": self.name,
                "agentType": "smart-cities",
                "timestamp": time.time(),
            },
        )
        logger.info("[%s] started — poll interval: %ds, weapon threshold: %.2f",
                    self.name, int(self._poll_interval), self._weapon_thresh)
        self._tasks.append(asyncio.create_task(self._poll_loop()))

    # ── Poll loop ──────────────────────────────────────────────────────────

    async def _poll_loop(self):
        import asyncio
        from ..core.actor import ActorState
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                await self._run_checks()
            except Exception as e:
                logger.error("[%s] poll error: %s", self.name, e, exc_info=True)
            await asyncio.sleep(self._poll_interval)

    async def _run_checks(self):
        since = _iso_ago(seconds=int(self._poll_interval * 2))
        await self._check_weapons(since)
        await self._publish_road_density()
        await self._publish_parking_summary()

    # ── Weapon detection monitor ───────────────────────────────────────────

    async def _check_weapons(self, since: str):
        query = _WEAPON_QUERY.replace("{since}", since)
        rows  = await _sparql_select(query)
        for row in rows:
            obs_uri    = _val(row, "obs")
            if obs_uri in self._seen_weapons:
                continue
            self._seen_weapons.add(obs_uri)

            confidence = float(_val(row, "confidence") or "0")
            sensor_id  = _val(row, "sensor").split("/")[-1]
            ts         = _val(row, "ts")
            wid        = _val(row, "wid") or "unknown"

            severity = "critical" if confidence >= 0.85 else "warning"
            alert = {
                "type":        "weapon_detection",
                "source":      "smart-cities-agent/sparql",
                "sensor_id":   sensor_id,
                "observation": obs_uri,
                "confidence":  confidence,
                "threshold":   self._weapon_thresh,
                "wid":         wid,
                "timestamp":   ts,
                "severity":    severity,
            }

            await self._mqtt_publish("synapse/alerts/weapon", alert)
            await self._mqtt_publish("synapse/smartcities/safety/alerts", alert)
            await self._mqtt_publish(
                f"agents/{self.actor_id}/alert",
                {
                    "actor_id":  self.actor_id,
                    "name":      self.name,
                    "message":   f"Weapon detected: sensor={sensor_id} confidence={confidence:.2f}",
                    "severity":  severity,
                    "timestamp": time.time(),
                },
            )
            logger.warning(
                "[%s] WEAPON ALERT sensor=%s conf=%.2f wid=%s",
                self.name,
                sensor_id,
                confidence,
                wid,
            )
            self.metrics.messages_processed += 1

    # ── Road density summary ───────────────────────────────────────────────

    async def _publish_road_density(self):
        rows = await _sparql_select(_ROAD_DENSITY_QUERY)
        if not rows:
            return
        density = {}
        for row in rows:
            sensor_id     = _val(row, "sensor").split("/")[-1]
            count         = int(_val(row, "count") or "0")
            density[sensor_id] = count

        total = sum(density.values())
        await self._mqtt_publish(
            "synapse/smartcities/road-density",
            {
                "by_sensor": density,
                "total":     total,
                "timestamp": _iso_now(),
            },
        )

    # ── Parking summary ────────────────────────────────────────────────────

    async def _publish_parking_summary(self):
        rows = await _sparql_select(_PARKING_QUERY)
        if not rows:
            return
        lots = []
        for row in rows:
            sid      = _val(row, "sensor").split("/")[-1]
            occupied = int(_val(row, "occupied") or "0")
            total    = int(_val(row, "total")    or "0")
            lots.append({
                "sensor_id":      sid,
                "occupied":       occupied,
                "total":          total,
                "occupancy_rate": round(occupied / total, 4) if total else 0.0,
            })

        await self._mqtt_publish(
            "synapse/smartcities/status",
            {
                "parking": lots,
                "timestamp": _iso_now(),
            },
        )

    # ── Message handling (task interface) ──────────────────────────────────

    async def handle_message(self, msg: Message):
        if msg.type not in (MessageType.TASK, MessageType.RESULT):
            return
        payload = msg.payload or {}
        if isinstance(payload, str):
            payload = {"text": payload}

        text = str(
            payload.get("text") or payload.get("content") or payload.get("task") or ""
        ).strip()
        if not text:
            return

        # strip agent prefix
        for pfx in ("@smart-cities-agent", "@smartcities-agent", "@sc-agent"):
            if text.lower().startswith(pfx):
                text = text[len(pfx):].lstrip()
                break

        reply = await self._dispatch(text, payload)
        await self._reply(reply)
        self.metrics.tasks_completed += 1

    async def _dispatch(self, text: str, payload: dict) -> str:
        lower = text.lower()

        if lower in ("help", "?"):
            return _HELP

        if lower.startswith("query ") or lower.startswith("sparql "):
            sparql = text[6:].strip() if lower.startswith("query ") else text[7:].strip()
            rows   = await _sparql_select(sparql)
            return _format_rows(rows, max_rows=20)

        if "weapon" in lower or "alert" in lower or "armed" in lower:
            since = _iso_ago(3600)
            rows  = await _sparql_select(_WEAPON_QUERY.replace("{since}", since))
            if not rows:
                return "No weapon detections above threshold in the last hour."
            lines = [f"**Weapon detections** (last 1h, confidence ≥ {self._weapon_thresh}):\n"]
            for r in rows:
                conf   = float(_val(r, "confidence") or "0")
                sensor = _val(r, "sensor").split("/")[-1]
                ts     = _val(r, "ts")
                wid    = _val(r, "wid") or "—"
                sev    = "🔴 CRITICAL" if conf >= 0.85 else "🟡 WARNING"
                lines.append(f"- {sev} sensor=`{sensor}` confidence=`{conf:.2f}` @ {ts[:19]} (WID: {wid})")
            return "\n".join(lines)

        if "road" in lower or "user" in lower or "density" in lower:
            rows = await _sparql_select(_ROAD_DENSITY_QUERY)
            if not rows:
                return "No road user data in current state graph."
            lines = ["**Road user density by sensor:**\n"]
            for r in rows:
                sensor = _val(r, "sensor").split("/")[-1]
                count  = _val(r, "count")
                lines.append(f"- `{sensor}`: {count} active road users")
            return "\n".join(lines)

        if "parking" in lower or "occupancy" in lower:
            rows = await _sparql_select(_PARKING_QUERY)
            if not rows:
                return "No parking occupancy data in current state graph."
            lines = ["**Parking occupancy:**\n"]
            for r in rows:
                sid      = _val(r, "sensor").split("/")[-1]
                occupied = _val(r, "occupied")
                total    = _val(r, "total")
                pct      = round(int(occupied) / int(total) * 100) if int(total or "0") else 0
                bar      = "█" * (pct // 10) + "░" * (10 - pct // 10)
                lines.append(f"- `{sid}`: {occupied}/{total} [{bar}] {pct}%")
            return "\n".join(lines)

        if "status" in lower or "summary" in lower or "overview" in lower:
            rows = await _sparql_select(_STATE_QUERY)
            if not rows:
                return "No observation data found in Fuseki."
            lines = ["**City observation summary (all time):**\n"]
            for r in rows:
                obs_type = _val(r, "type").split("#")[-1].split("/")[-1]
                count    = _val(r, "count")
                lines.append(f"- `{obs_type}`: {count} observations")
            return "\n".join(lines)

        # Unknown command
        return f"Unknown command: `{text[:60]}`\n\nType `help` for available commands."

    async def _reply(self, content: str):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/chat",
            {"from": self.name, "to": "user", "content": content, "timestamp": time.time()},
        )

    def _current_task_description(self) -> str:
        return f"smart-cities monitor — {_FUSEKI_URL}/{_FUSEKI_DATASET}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _iso_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _iso_ago(seconds: int) -> str:
    import datetime
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _format_rows(rows: list[dict], max_rows: int = 20) -> str:
    if not rows:
        return "Query returned no results."
    vars_ = list(rows[0].keys())
    lines = [f"**Results** ({len(rows)} rows — columns: {', '.join(vars_)}):\n"]
    for i, row in enumerate(rows[:max_rows]):
        parts = [f"`{v}`={_val(row, v)!r}" for v in vars_]
        lines.append(f"{i + 1}. {' | '.join(parts)}")
    if len(rows) > max_rows:
        lines.append(f"… and {len(rows) - max_rows} more rows")
    return "\n".join(lines)


_HELP = """\
**SmartCities Agent** 🏙️
_SYNAPSE smart city sensor monitor_

```
weapons / alerts       recent weapon detections (last 1h)
road / density         road user counts by sensor
parking / occupancy    parking space occupancy
status / summary       observation counts by type
query <sparql>         run a raw SPARQL SELECT
help                   this message
```

**Integration points:**
- Polls `urn:synapse:events` in Fuseki every 30s for new weapon detections
- Publishes alerts to `synapse/alerts/weapon` and `synapse/smartcities/safety/alerts`
- Road density → `synapse/smartcities/road-density`
- Parking summary → `synapse/smartcities/status`

Set `FUSEKI_URL`, `FUSEKI_DATASET`, `WEAPON_THRESHOLD`, `POLL_INTERVAL` in environment.
"""
