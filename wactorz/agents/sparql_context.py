"""sparql_context.py — SPARQL-powered world model for the PlannerAgent.

PURPOSE
-------
This module adds a `_sparql_context()` method that the PlannerAgent can call
before its LLM decomposition step.  It queries the Fuseki triplestore that
wactorz already maintains and returns structured, up-to-date information about:

  1. Running agents + their published MQTT topics (from urn:wactorz:agents)
  2. Known channels with declared/observed schemas (from urn:wactorz:channels)
  3. Relevant Home Assistant device state (from urn:ha:current), only when the
     task mentions a room, device domain, or specific entity keyword.

HOW TO INTEGRATE
----------------
Drop this file next to planner_agent.py, then in PlannerAgent:

    from .sparql_context import build_sparql_context

    # In _decompose() and _decompose_pipeline(), just before building `prompt`:
    sparql_ctx = await build_sparql_context(
        task=task,
        fuseki_url=getattr(self, "_fuseki_url", None),
        timeout=3.0,
    )
    # Then append sparql_ctx to the prompt where you already have topic_schema_ctx

The method is deliberately optional: if Fuseki is unreachable, it returns ""
so the planner degrades gracefully to its existing TopicBus path.

HONEST ASSESSMENT OF VALUE vs OVERKILL — read below.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

logger = logging.getLogger(__name__)

# ── Namespace shortcuts ────────────────────────────────────────────────────────

_PREFIXES = """
PREFIX rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX ssn:   <http://www.w3.org/ns/ssn/>
PREFIX sosa:  <http://www.w3.org/ns/sosa/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX syn:   <https://synapse.waldiez.io/ns#>
PREFIX wact:  <https://waldiez.github.io/wactorz/ontology#>
"""

# ── Individual queries ─────────────────────────────────────────────────────────

_Q_AGENTS_AND_TOPICS = (
    _PREFIXES
    + """
SELECT DISTINCT ?agentLabel ?description ?mqttTopic ?direction
WHERE {
  GRAPH <urn:wactorz:agents> {
    ?agent rdfs:label ?agentLabel .
    OPTIONAL { ?agent dcterms:description ?description . }

    {
      ?agent ssn:hasProperty ?prop .
      ?prop syn:publishesTopic ?mqttTopic .
      BIND("PUBLISH" AS ?direction)
    }
    UNION
    {
      ?agent dcterms:description ?desc .
      FILTER(CONTAINS(LCASE(?desc), "subscribes to"))
      BIND(REPLACE(?desc, ".*[Ss]ubscribes to ([^ ]+).*", "$1") AS ?mqttTopic)
      BIND("SUBSCRIBE" AS ?direction)
    }
  }
}
ORDER BY ?agentLabel ?direction
"""
)

_Q_CHANNELS = (
    _PREFIXES
    + """
SELECT DISTINCT ?channelId ?topic ?declaredSchema ?observedSchema ?observedExample ?publisherLabel
WHERE {
  GRAPH <urn:wactorz:channels> {
    ?channel rdf:type wact:Channel ;
             wact:channelTopic ?topic .
    BIND(STRAFTER(STR(?channel), "urn:wactorz:channel:") AS ?channelId)
    OPTIONAL { ?channel wact:declaredSchema  ?declaredSchema  . }
    OPTIONAL { ?channel wact:observedSchema  ?observedSchema  . }
    OPTIONAL { ?channel wact:observedExample ?observedExample . }
    OPTIONAL {
      ?publisher wact:publishesTo ?channel ;
                 rdfs:label       ?publisherLabel .
    }
  }
}
ORDER BY ?topic
"""
)

# HA state query — parameterised: {keywords} is a SPARQL REGEX pattern
_Q_HA_STATE_TEMPLATE = (
    _PREFIXES
    + """
SELECT DISTINCT ?entityId ?state
WHERE {{
  GRAPH <urn:ha:current> {{
    ?entity syn:state ?state .
    BIND(STRAFTER(STR(?entity), "urn:ha:entity:") AS ?entityId)
    FILTER(REGEX(LCASE(?entityId), "{keywords}", "i"))
  }}
}}
LIMIT 20
"""
)

# ── HTTP helper ────────────────────────────────────────────────────────────────


async def _sparql_query(
    url: str, query: str, timeout: float = 3.0, _label: str = "query", _auth: tuple | None = None
) -> list[dict]:
    """POST a SPARQL SELECT query to Fuseki, return rows as list of dicts.
    Each dict maps variable name → string value (or "" if unbound).
    Returns [] on any error so callers can degrade gracefully.
    """
    try:
        import httpx
    except ImportError:
        logger.debug("sparql_context: httpx not installed — skipping Fuseki query")
        return []

    try:
        # ── SPARQL LATENCY ─────────────────────────────────────────────────
        import time as _time

        _t0 = _time.perf_counter()
        # ── SPARQL LATENCY ─────────────────────────────────────────────────

        kwargs: dict = {
            "data": {"query": query},
            "headers": {"Accept": "application/sparql-results+json"},
        }
        if _auth:
            kwargs["auth"] = _auth  # httpx accepts (user, password) tuple

        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, **kwargs)
            r.raise_for_status()
            body = r.json()

        # ── SPARQL LATENCY ─────────────────────────────────────────────────
        _elapsed_ms = (_time.perf_counter() - _t0) * 1000
        logger.info(f"[sparql_latency] {_label}: {_elapsed_ms:.1f} ms")
        print(f"[sparql_latency] {_label}: {_elapsed_ms:.1f} ms", flush=True)
        # ── SPARQL LATENCY ─────────────────────────────────────────────────

        vars_ = body["head"]["vars"]
        rows = []
        for binding in body["results"]["bindings"]:
            row = {v: binding.get(v, {}).get("value", "") for v in vars_}
            rows.append(row)
        return rows

    except Exception as e:
        logger.debug(f"sparql_context: query failed — {e}")
        return []


# ── HA keyword extraction ──────────────────────────────────────────────────────

_HA_KEYWORDS = {
    # domains
    "light",
    "lights",
    "switch",
    "climate",
    "cover",
    "sensor",
    "binary_sensor",
    "lock",
    "media_player",
    "fan",
    "camera",
    # rooms from your data
    "bedroom",
    "kitchen",
    "living_room",
    "workspace",
    # common device words
    "lamp",
    "door",
    "window",
    "motion",
    "temperature",
    "humidity",
    "occupancy",
    "presence",
    "power",
    "energy",
}


def _extract_ha_keywords(task: str) -> str | None:
    """Return a SPARQL REGEX pattern if the task mentions any HA-relevant keyword,
    else None (so we skip the HA query when it's irrelevant).
    """
    words = set(re.findall(r"\b\w+\b", task.lower()))
    matched = words & _HA_KEYWORDS
    if not matched:
        return None
    # Build a pipe-separated alternation: "bedroom|lamp|door"
    return "|".join(re.escape(w) for w in matched)


# ── Public API ─────────────────────────────────────────────────────────────────


async def build_sparql_context(
    task: str,
    fuseki_url: str | None = None,
    timeout: float = 3.0,
) -> str:
    """Run up to 3 targeted SPARQL queries against Fuseki and return a formatted
    context block ready to be injected into the planner's LLM prompt.

    Parameters
    ----------
    task        : the user's task string (used to decide whether to run HA query)
    fuseki_url  : full URL of the Fuseki SPARQL endpoint, e.g.
                  "http://localhost:3030/wactorz/sparql"
                  Falls back to CONFIG.fuseki_url if not supplied.
    timeout     : per-query HTTP timeout in seconds (default 3.0)

    Returns:
    -------
    A non-empty string to append to the planner prompt, or "" if Fuseki is
    unreachable or returned no useful data.
    """
    if not fuseki_url:
        try:
            from ..config import CONFIG

            fuseki_url = getattr(CONFIG, "fuseki_url", None) or getattr(
                CONFIG, "fuseki_endpoint", None
            )
        except Exception:
            pass

    if not fuseki_url:
        # Build from env vars the same way fuseki.py does:
        # FUSEKI_URL (base) + FUSEKI_DATASET → {base}/{dataset}/sparql
        import os

        _base = os.environ.get("FUSEKI_URL", "http://localhost:3030").rstrip("/")
        _dataset = os.environ.get("FUSEKI_DATASET", "wactorz")
        fuseki_url = f"{_base}/{_dataset}/sparql"
    elif not any(seg in fuseki_url for seg in ("/sparql", "/query")):
        # Caller passed the base URL only (e.g. "http://localhost:3030") — append dataset/sparql
        import os

        _dataset = os.environ.get("FUSEKI_DATASET", "wactorz")
        fuseki_url = f"{fuseki_url.rstrip('/')}/{_dataset}/sparql"

    # Resolve basic auth from env (mirrors FUSEKI_USER / FUSEKI_PASSWORD in fuseki.py)
    import os as _os

    _user = _os.environ.get("FUSEKI_USER", "")
    _password = _os.environ.get("FUSEKI_PASSWORD", "")
    _auth: tuple | None = (_user, _password) if _user else None

    # ── Run queries concurrently ───────────────────────────────────────────
    ha_keywords = _extract_ha_keywords(task)

    # ── SPARQL LATENCY ─────────────────────────────────────────────────────
    import time as _time

    _t_total = _time.perf_counter()
    # ── SPARQL LATENCY ─────────────────────────────────────────────────────

    tasks = [
        _sparql_query(
            fuseki_url, _Q_AGENTS_AND_TOPICS, timeout, _label="agents_and_topics", _auth=_auth
        ),
        _sparql_query(fuseki_url, _Q_CHANNELS, timeout, _label="channels", _auth=_auth),
    ]
    if ha_keywords:
        tasks.append(
            _sparql_query(
                fuseki_url,
                _Q_HA_STATE_TEMPLATE.format(keywords=ha_keywords),
                timeout,
                _label="ha_state",
                _auth=_auth,
            )
        )
    else:

        async def _empty():
            return []

        tasks.append(_empty())

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # ── SPARQL LATENCY ─────────────────────────────────────────────────────
    _total_ms = (_time.perf_counter() - _t_total) * 1000
    _n_queries = 2 + (1 if ha_keywords else 0)
    logger.info(
        f"[sparql_latency] total wall time ({_n_queries} queries concurrent): {_total_ms:.1f} ms"
    )
    print(
        f"[sparql_latency] total wall time ({_n_queries} queries concurrent): {_total_ms:.1f} ms",
        flush=True,
    )
    # ── SPARQL LATENCY ─────────────────────────────────────────────────────

    agent_rows = results[0] if not isinstance(results[0], Exception) else []
    channel_rows = results[1] if not isinstance(results[1], Exception) else []
    ha_rows = results[2] if not isinstance(results[2], Exception) else []

    # ── SPARQL LATENCY ─────────────────────────────────────────────────────
    logger.info(
        f"[sparql_context] results: agents={len(agent_rows)} rows, "
        f"channels={len(channel_rows)} rows, "
        f"ha_state={len(ha_rows)} rows"
        + (" (no HA keywords in task — skipped)" if not ha_keywords else "")
    )
    print(
        f"[sparql_context] results: agents={len(agent_rows)} rows, "
        f"channels={len(channel_rows)} rows, "
        f"ha_state={len(ha_rows)} rows"
        + (" (no HA keywords in task — skipped)" if not ha_keywords else ""),
        flush=True,
    )
    # ── SPARQL LATENCY ─────────────────────────────────────────────────────

    if not agent_rows and not channel_rows:
        # ── SPARQL LATENCY ─────────────────────────────────────────────────
        logger.info("[sparql_context] all queries empty — no context injected into prompt")
        print("[sparql_context] all queries empty — no context injected into prompt", flush=True)
        # ── SPARQL LATENCY ─────────────────────────────────────────────────
        return ""  # Fuseki offline or empty — planner continues without us

    sections: list[str] = []

    # ── Section 1: Agent → topic map ──────────────────────────────────────
    if agent_rows:
        # Group by agent
        by_agent: dict[str, dict] = {}
        for row in agent_rows:
            name = row.get("agentLabel", "")
            if not name:
                continue
            if name not in by_agent:
                by_agent[name] = {
                    "description": row.get("description", ""),
                    "publishes": [],
                    "subscribes": [],
                }
            topic = row.get("mqttTopic", "")
            direction = row.get("direction", "")
            if topic:
                if direction == "PUBLISH":
                    by_agent[name]["publishes"].append(topic)
                elif direction == "SUBSCRIBE":
                    by_agent[name]["subscribes"].append(topic)

        lines = ["SPARQL — KNOWN AGENTS & MQTT TOPICS (from Fuseki):"]
        for name, info in sorted(by_agent.items()):
            desc = info["description"]
            desc_short = (desc[:100] + "…") if len(desc) > 100 else desc
            line = f"  {name}"
            if desc_short:
                line += f": {desc_short}"
            if info["publishes"]:
                line += f"\n    publishes:  {', '.join(info['publishes'])}"
            if info["subscribes"]:
                line += f"\n    subscribes: {', '.join(info['subscribes'])}"
            lines.append(line)
        sections.append("\n".join(lines))

        # ── SPARQL LATENCY ─────────────────────────────────────────────────
        _pub_topics = [t for i in by_agent.values() for t in i["publishes"]]
        _sub_topics = [t for i in by_agent.values() for t in i["subscribes"]]
        logger.info(
            f"[sparql_context] agents section: {len(by_agent)} agent(s), "
            f"{len(_pub_topics)} publish topic(s), {len(_sub_topics)} subscribe topic(s) — "
            f"agents: {list(by_agent.keys())}"
        )
        print(
            f"[sparql_context] agents section: {len(by_agent)} agent(s) — "
            f"agents: {list(by_agent.keys())}",
            flush=True,
        )
        # ── SPARQL LATENCY ─────────────────────────────────────────────────

    # ── Section 2: Channel schemas ─────────────────────────────────────────
    if channel_rows:
        lines = ["SPARQL — CHANNEL SCHEMAS (use exact field names in generated code):"]
        for row in channel_rows:
            topic = row.get("topic", "")
            pub = row.get("publisherLabel", "")
            ds = row.get("declaredSchema", "")
            os_ = row.get("observedSchema", "")
            example = row.get("observedExample", "")

            if not topic:
                continue

            schema_str = ds or os_ or ""
            example_str = example or ""

            line = f"  {topic}"
            if pub:
                line += f"  (published by {pub})"
            if schema_str:
                # Try to make it compact
                try:
                    parsed = json.loads(schema_str)
                    line += f"\n    fields: {json.dumps(parsed)}"
                except Exception:
                    line += f"\n    fields: {schema_str}"
            if example_str:
                line += f"\n    example: {example_str}"
            lines.append(line)
        sections.append("\n".join(lines))

        # ── SPARQL LATENCY ─────────────────────────────────────────────────
        _ch_topics = [r.get("topic", "") for r in channel_rows if r.get("topic")]
        _ch_with_schema = [
            r.get("topic", "")
            for r in channel_rows
            if r.get("declaredSchema") or r.get("observedSchema")
        ]
        _ch_with_example = [r.get("topic", "") for r in channel_rows if r.get("observedExample")]
        logger.info(
            f"[sparql_context] channels section: {len(_ch_topics)} topic(s), "
            f"{len(_ch_with_schema)} with schema, {len(_ch_with_example)} with example — "
            f"topics: {_ch_topics}"
        )
        print(
            f"[sparql_context] channels section: {len(_ch_topics)} topic(s) "
            f"({len(_ch_with_schema)} with schema, {len(_ch_with_example)} with example) — "
            f"topics: {_ch_topics}",
            flush=True,
        )
        # ── SPARQL LATENCY ─────────────────────────────────────────────────

    # ── Section 3: Relevant HA device states ──────────────────────────────
    if ha_rows:
        lines = ["SPARQL — RELEVANT HOME ASSISTANT DEVICE STATES:"]
        for row in ha_rows:
            eid = row.get("entityId", "")
            state = row.get("state", "")
            if eid:
                lines.append(f"  {eid}: {state}")
        if len(lines) > 1:
            sections.append("\n".join(lines))
            # ── SPARQL LATENCY ─────────────────────────────────────────────
            _ha_entities = [r.get("entityId", "") for r in ha_rows if r.get("entityId")]
            logger.info(
                f"[sparql_context] ha_state section: {len(_ha_entities)} entity/ies — "
                f"entities: {_ha_entities}"
            )
            print(
                f"[sparql_context] ha_state section: {len(_ha_entities)} entity/ies — "
                f"{_ha_entities}",
                flush=True,
            )
            # ── SPARQL LATENCY ─────────────────────────────────────────────
        else:
            # ── SPARQL LATENCY ─────────────────────────────────────────────
            logger.info(
                "[sparql_context] ha_state: query returned rows but no entityId values — section skipped"
            )
            print(
                "[sparql_context] ha_state: rows returned but no entityId values — section skipped",
                flush=True,
            )
            # ── SPARQL LATENCY ─────────────────────────────────────────────

    if not sections:
        # ── SPARQL LATENCY ─────────────────────────────────────────────────
        logger.info("[sparql_context] all sections empty after processing — no context injected")
        print(
            "[sparql_context] all sections empty after processing — no context injected", flush=True
        )
        # ── SPARQL LATENCY ─────────────────────────────────────────────────
        return ""

    header = "\n\n" + "=" * 60 + "\n"
    footer = "\n" + "=" * 60
    ctx = header + ("\n\n".join(sections)) + footer

    # ── SPARQL LATENCY ─────────────────────────────────────────────────────
    logger.info(
        f"[sparql_context] injecting {len(ctx)} chars into prompt "
        f"({len(sections)} section(s): "
        + ", ".join(
            [
                "agents" if "AGENTS" in s else "channels" if "CHANNEL" in s else "ha_state"
                for s in sections
            ]
        )
        + ")"
    )
    print(
        f"[sparql_context] injecting {len(ctx)} chars into prompt ({len(sections)} section(s))",
        flush=True,
    )
    # ── SPARQL LATENCY ─────────────────────────────────────────────────────

    return ctx


# ── Utility: stale agent detection ────────────────────────────────────────────


async def find_stale_agents(
    fuseki_url: str,
    stale_threshold_seconds: float = 60.0,
    timeout: float = 3.0,
) -> list[str]:
    """Return agent labels whose metricsUpdatedAt has not been seen in the last
    stale_threshold_seconds — indicating the agent may have crashed silently.

    This is a bonus utility; the planner can call it as a health check and
    include the result in its context or alert the user.

    The query finds the latest metricsUpdatedAt per agent (each agent has exactly
    one value — upserted atomically) and flags agents where that timestamp is
    older than the threshold.
    """
    q = (
        _PREFIXES
        + f"""
SELECT ?agentLabel (MAX(?ts) AS ?latestUpdate)
WHERE {{
  GRAPH <urn:wactorz:agents> {{
    ?agent rdfs:label ?agentLabel ;
           wact:metricsUpdatedAt ?rawTs .
    BIND(xsd:dateTime(?rawTs) AS ?ts)
  }}
}}
GROUP BY ?agentLabel
HAVING (
  (NOW() - MAX(?ts)) > "PT{int(stale_threshold_seconds)}S"^^xsd:duration
)
ORDER BY ?agentLabel
"""
    )
    rows = await _sparql_query(fuseki_url, q, timeout)
    return [r.get("agentLabel", "") for r in rows if r.get("agentLabel")]


# ── Wiring opportunity query ───────────────────────────────────────────────────


async def find_unwired_channels(
    fuseki_url: str,
    timeout: float = 3.0,
) -> list[dict]:
    """Find channels that have a publisher but no subscriber declared in Fuseki.
    Useful for the planner to proactively suggest wiring opportunities.

    Returns a list of dicts: {"topic": ..., "publisher": ...}
    """
    q = (
        _PREFIXES
        + """
SELECT DISTINCT ?topic ?publisherLabel
WHERE {
  GRAPH <urn:wactorz:channels> {
    ?channel rdf:type wact:Channel ;
             wact:channelTopic ?topic .
    ?publisher wact:publishesTo ?channel ;
               rdfs:label       ?publisherLabel .
    FILTER NOT EXISTS {
      GRAPH <urn:wactorz:agents> {
        ?subscriber dcterms:description ?desc .
        FILTER(CONTAINS(LCASE(?desc), LCASE(?topic)))
      }
    }
  }
}
ORDER BY ?topic
"""
    )
    rows = await _sparql_query(fuseki_url, q, timeout)
    return [
        {"topic": r.get("topic", ""), "publisher": r.get("publisherLabel", "")}
        for r in rows
        if r.get("topic")
    ]
