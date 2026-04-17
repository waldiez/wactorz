"""
wactorz/fuseki.py  -  Home Assistant + Agent Manifest → Apache Jena Fuseki bridge.

Two bridges run concurrently:

  HAFusekiBridge        — subscribes to HA ``state_changed`` events and pushes
                          RDF/Turtle to Fuseki via the Graph Store Protocol (GSP)
                          and SPARQL Update.

  AgentManifestBridge   — subscribes to the MQTT ``agents/+/manifest`` topic and
                          maps each wactorz agent manifest to HSML/SOSA/syn RDF,
                          storing it in the ``urn:wactorz:agents`` named graph so
                          that any HSML-compatible consumer can discover agents and
                          their input/output schemas via SPARQL.

Ontology prefixes used (all inline - no external file references):
  core:     HSML Core  <https://www.spatialwebfoundation.org/ns/hsml/core#>
  saref:    SAREF Core <https://saref.etsi.org/core/>
  sosa:     SOSA       <http://www.w3.org/ns/sosa/>
  ssn:      SSN        <http://www.w3.org/ns/ssn/>
  syn:      SYNAPSE    <https://synapse.waldiez.io/ns#>
  prov:     PROV-O     <http://www.w3.org/ns/prov#>
  dcterms:  DC Terms   <http://purl.org/dc/terms/>
  bot:    BOT        <https://w3id.org/bot#>  (Building Topology Ontology)
  

Named graphs managed:
  urn:ha:current  - latest state per entity  (DELETE + INSERT on every change)
  urn:ha:history  - append-only observations
  urn:ha:devices  - entity catalog            (rebuilt on startup)
  urn:ha:areas    - area/room topology        (rebuilt on startup)
  urn:wactorz:agents   - wactorz agent service catalog (upserted on every manifest)

Usage::

    python -m wactorz.fuseki

Environment variables:
    HA_URL          Home Assistant base URL  (default: http://homeassistant.local:8123)
    HA_TOKEN        Long-lived access token  (required for HAFusekiBridge)
    FUSEKI_URL      Fuseki base URL          (default: http://localhost:3030)
    FUSEKI_DATASET  Fuseki dataset name      (default: wactorz)
    FUSEKI_USER     Fuseki admin user        (default: admin)
    FUSEKI_PASSWORD Fuseki admin password    (default: empty = no auth)
    HA_DOMAINS      Comma-separated domains  (default: see DEFAULT_DOMAINS)
    MQTT_BROKER     MQTT broker host         (default: localhost)
    MQTT_PORT       MQTT broker port         (default: 1883)
"""

# cspell: disable
# flake8: noqa: E501
# pyright: reportAny=false,reportExplicitAny=false,reportUnusedCallResult=false
# pyright: reportUnknownVariableType=false,reportUnknownMemberType=false,reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, quote

import aiohttp
import aiomqtt

from wactorz.core.integrations.home_assistant.ha_web_socket_client import (
    HAWebSocketClient,
)

log = logging.getLogger("wactorz.fuseki")

# ── Shared Turtle prefix block ────────────────────────────────────────────────

TTL_PREFIXES = """\
@prefix rdf:     <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs:    <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:     <http://www.w3.org/2001/XMLSchema#> .
@prefix owl:     <http://www.w3.org/2002/07/owl#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix prov:    <http://www.w3.org/ns/prov#> .
@prefix sosa:    <http://www.w3.org/ns/sosa/> .
@prefix ssn:     <http://www.w3.org/ns/ssn/> .
@prefix saref:   <https://saref.etsi.org/core/> .
@prefix core:    <https://www.spatialwebfoundation.org/ns/hsml/core#> .
@prefix syn:     <https://synapse.waldiez.io/ns#> .
@prefix bot:   <https://w3id.org/bot#> .
@prefix ha:      <urn:ha:entity:> .
@prefix haobs:   <urn:ha:obs:> .
@prefix haprop:  <urn:ha:prop:> .
@prefix wact:    <urn:wactorz:agent:> .
@prefix wactprop: <urn:wactorz:prop:> .
@prefix haarea: <urn:ha:area:> .
"""

# Provenance IRIs
BRIDGE_AGENT_IRI = "<urn:ha:bridge:wactorz>"
MANIFEST_BRIDGE_IRI = "<urn:wactorz:bridge:agent-manifest>"

# Named graph IRIs
GRAPH_CURRENT = "urn:ha:current"
GRAPH_HISTORY = "urn:ha:history"
GRAPH_DEVICES = "urn:ha:devices"
GRAPH_AGENTS  = "urn:wactorz:agents"
GRAPH_AREAS   = "urn:ha:areas"

# ── Domain → RDF type mapping ─────────────────────────────────────────────────

DEFAULT_DOMAINS: frozenset[str] = frozenset(
    {
        "sensor",
        "binary_sensor",
        "light",
        "switch",
        "climate",
        "cover",
        "device_tracker",
        "input_boolean",
        "input_number",
        "input_select",
        "automation",
        "script",
        "weather",
        "sun",
    }
)

# domain → (rdf:type list, is_actuator)
_DOMAIN_TYPES: dict[str, tuple[list[str], bool]] = {
    "sensor":         (["sosa:Sensor", "saref:Sensor", "core:Thing"], False),
    "binary_sensor":  (["sosa:Sensor", "saref:Sensor", "core:Thing"], False),
    "light":          (["sosa:Actuator", "saref:LightingDevice", "core:Thing"], True),
    "switch":         (["sosa:Actuator", "saref:Switch", "core:Thing"], True),
    "cover":          (["sosa:Actuator", "saref:Device", "core:Thing"], True),
    "climate":        (["sosa:Actuator", "saref:HVAC", "core:Thing"], True),
    "device_tracker": (["syn:Person", "sosa:FeatureOfInterest", "core:Thing"], False),
    "input_boolean":  (["sosa:Actuator", "saref:Switch", "core:Thing"], True),
    "input_number":   (["sosa:Sensor", "core:Thing"], False),
    "input_select":   (["saref:Device", "core:Thing"], False),
    "automation":     (["saref:Device", "core:Thing"], False),
    "script":         (["saref:Device", "core:Thing"], False),
    "weather":        (["sosa:Sensor", "saref:Sensor", "core:Thing"], False),
    "sun":            (["core:Thing"], False),
}
_DEFAULT_TYPES: tuple[list[str], bool] = (["saref:Device", "core:Thing"], False)


# ── Small helpers ─────────────────────────────────────────────────────────────

def _safe(s: str) -> str:
    """Replace non-IRI-safe chars with underscores."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def _iri(entity_id: str) -> str:
    return f"ha:{_safe(entity_id)}"


def _obs_iri(entity_id: str, ts_ms: int) -> str:
    return f"haobs:{_safe(entity_id)}_{ts_ms}"


def _prop_iri(entity_id: str) -> str:
    return f"haprop:{_safe(entity_id)}"


def _area_iri(area_id: str) -> str:
    return f"haarea:{_safe(area_id)}"


def _esc(s: str) -> str:
    """Escape a string for Turtle double-quoted literals."""
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _literal(value: Any) -> str:
    """Convert a value to a typed Turtle literal."""
    if isinstance(value, bool):
        return f'"{str(value).lower()}"^^xsd:boolean'
    if isinstance(value, int):
        return f'"{value}"^^xsd:integer'
    if isinstance(value, float):
        return f'"{value}"^^xsd:decimal'
    s = str(value)
    try:
        if "." in s:
            float(s)
            return f'"{s}"^^xsd:decimal'
        int(s)
        return f'"{s}"^^xsd:integer'
    except ValueError:
        pass
    return f'"{_esc(s)}"'


def _dt_from_ha(ts: str | None) -> str:
    """Parse a HA ISO timestamp to UTC xsd:dateTime string, or fall back to now."""
    if not ts:
        return _dt_now()
    try:
        dt = datetime.fromisoformat(ts)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return _dt_now()


def _dt_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Turtle body builders (no prefix declarations) ─────────────────────────────

def _bridge_agent_body() -> str:
    return (
        f"{BRIDGE_AGENT_IRI}\n"
        f"  a syn:Agent, prov:SoftwareAgent ;\n"
        f'  rdfs:label "wactorz HA-Fuseki bridge" .\n'
    )


def _area_body(area: dict[str, Any]) -> str:
    """RDF triples for one HA area (room) using BOT ontology."""
    area_id = area.get("area_id", "")
    name = area.get("name", area_id)
    aliases = area.get("aliases", [])
    icon = area.get("icon") or ""

    iri = _area_iri(area_id)
    lines: list[str] = []
    lines.append(f"{iri}")
    lines.append(f"  a bot:Space, syn:Area ;")
    lines.append(f'  rdfs:label "{_esc(name)}" ;')
    lines.append(f'  syn:areaId "{_esc(area_id)}" ;')
    if icon:
        lines.append(f'  syn:icon "{_esc(icon)}" ;')
    for alias in aliases:
        lines.append(f'  syn:alias "{_esc(str(alias))}" ;')
    lines.append(f"  prov:wasAttributedTo {BRIDGE_AGENT_IRI} .")
    lines.append("")
    return "\n".join(lines)


def _device_body(
    entity_id: str,
    state_obj: dict[str, Any],
    area_id: str | None = None,
    area_name: str | None = None,
) -> str:
    """Catalog entry for one HA entity (no prefix block)."""
    domain = entity_id.split(".")[0]
    attrs = state_obj.get("attributes") or {}
    friendly = attrs.get("friendly_name") or entity_id
    state_val = str(state_obj.get("state", ""))
    types, is_actuator = _DOMAIN_TYPES.get(domain, _DEFAULT_TYPES)
    iri = _iri(entity_id)
    prop_iri = _prop_iri(entity_id)

    lines: list[str] = []

    # Entity — list all types with comma-separated rdf:type
    type_str = ", ".join(types)
    lines.append(f"{iri}")
    lines.append(f"  a {type_str} ;")
    lines.append(f'  rdfs:label "{_esc(friendly)}" ;')
    lines.append(f"  syn:state {_literal(state_val)} ;")
    if is_actuator:
        lines.append(f"  ssn:hasProperty {prop_iri} ;")

    # Area / room assignment
    if area_id:
        lines.append(f"  syn:hasArea {_area_iri(area_id)} ;")
    if area_name:
        lines.append(f'  syn:areaName "{_esc(area_name)}" ;')

    lines.append(f"  prov:wasAttributedTo {BRIDGE_AGENT_IRI} .")
    lines.append("")

    # Observable/actuatable property
    prop_type = "sosa:ActuatableProperty" if is_actuator else "sosa:ObservableProperty"
    lines.append(f"{prop_iri}")
    lines.append(f"  a {prop_type} ;")
    lines.append(f'  rdfs:label "{_esc(domain)}" .')
    lines.append("")

    return "\n".join(lines)


def _current_obs_body(entity_id: str, state_obj: dict[str, Any], ts_ms: int) -> str:
    attrs = state_obj.get("attributes") or {}
    state_val = str(state_obj.get("state", ""))
    last_changed = (
        state_obj.get("last_changed") or state_obj.get("last_updated")
    )
    ts_dt = _dt_from_ha(last_changed)
    unit = attrs.get("unit_of_measurement") or attrs.get("unit")

    iri = _iri(entity_id)
    obs_iri = _obs_iri(entity_id, ts_ms)
    prop_iri = _prop_iri(entity_id)

    lines: list[str] = []

    lines.append(f"{iri}")
    lines.append(f"  syn:hasCurrentObservation {obs_iri} ;")
    lines.append(f"  syn:state {_literal(state_val)} .")
    lines.append("")

    lines.append(f"{obs_iri}")
    lines.append("  a sosa:Observation ;")
    lines.append(f"  sosa:madeBySensor {iri} ;")
    lines.append(f"  sosa:hasFeatureOfInterest {iri} ;")
    lines.append(f"  sosa:observedProperty {prop_iri} ;")
    lines.append(f"  sosa:hasSimpleResult {_literal(state_val)} ;")
    if unit:
        lines.append(f"  syn:unit {_literal(str(unit))} ;")
    lines.append(f'  sosa:resultTime "{ts_dt}"^^xsd:dateTime ;')
    lines.append(f'  prov:generatedAtTime "{ts_dt}"^^xsd:dateTime ;')
    lines.append(f"  prov:wasAttributedTo {BRIDGE_AGENT_IRI} .")
    lines.append("")

    return "\n".join(lines)


def _history_obs_body(entity_id: str, state_obj: dict[str, Any], ts_ms: int) -> str:
    attrs = state_obj.get("attributes") or {}
    state_val = str(state_obj.get("state", ""))
    last_changed = (
        state_obj.get("last_changed") or state_obj.get("last_updated")
    )
    ts_dt = _dt_from_ha(last_changed)
    unit = attrs.get("unit_of_measurement") or attrs.get("unit")

    iri = _iri(entity_id)
    obs_iri = _obs_iri(entity_id, ts_ms)
    prop_iri = _prop_iri(entity_id)

    lines: list[str] = []
    lines.append(f"{obs_iri}")
    lines.append("  a sosa:Observation ;")
    lines.append(f"  sosa:madeBySensor {iri} ;")
    lines.append(f"  sosa:hasFeatureOfInterest {iri} ;")
    lines.append(f"  sosa:observedProperty {prop_iri} ;")
    lines.append(f"  sosa:hasSimpleResult {_literal(state_val)} ;")
    if unit:
        lines.append(f"  syn:unit {_literal(str(unit))} ;")
    lines.append(f'  sosa:resultTime "{ts_dt}"^^xsd:dateTime ;')
    lines.append(f'  prov:generatedAtTime "{ts_dt}"^^xsd:dateTime ;')
    lines.append(f"  prov:wasAttributedTo {BRIDGE_AGENT_IRI} .")
    lines.append("")

    return "\n".join(lines)


def _ttl(body: str) -> str:
    """Prepend prefix block to a body string."""
    return TTL_PREFIXES + "\n" + body


# ── Agent manifest → RDF helpers ─────────────────────────────────────────────

def _agent_iri(actor_id: str) -> str:
    return f"wact:{_safe(actor_id)}"


def _aprop_iri(actor_id: str, direction: str, field: str) -> str:
    return f"wactprop:{_safe(actor_id)}_{direction}_{_safe(field)}"


def _parse_schema_desc(spec: Any) -> str:
    """Extract human description from schema values like 'str — some description'."""
    s = str(spec)
    for sep in (" \u2014 ", " — ", " - "):
        if sep in s:
            return s.split(sep, 1)[1].strip()
    return ""


def _agent_manifest_body(manifest: dict[str, Any]) -> str:
    """Convert a wactorz agent manifest dict to a Turtle body (no prefix block)."""
    actor_id: str = str(manifest.get("actor_id") or manifest.get("name") or "unknown")
    name: str = str(manifest.get("name") or actor_id)
    description: str = str(manifest.get("description") or "")
    input_schema: dict[str, Any] = manifest.get("input_schema") or {}
    output_schema: dict[str, Any] = manifest.get("output_schema") or {}
    capabilities: list[Any] = manifest.get("capabilities") or []
    publishes: list[Any] = manifest.get("publishes") or []
    timestamp: float = float(manifest.get("timestamp") or time.time())

    safe_id = _safe(actor_id)
    agent_iri = f"wact:{safe_id}"
    ts_dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    in_props  = [_aprop_iri(actor_id, "in",  f) for f in input_schema]
    out_props = [_aprop_iri(actor_id, "out", f) for f in output_schema]
    cap_iris  = [_aprop_iri(actor_id, "cap", c) for c in capabilities if isinstance(c, str)]
    pub_iris  = [_aprop_iri(actor_id, "pub", t) for t in publishes   if isinstance(t, str)]
    all_props = in_props + out_props + cap_iris + pub_iris

    lines: list[str] = []

    # ── Agent node ────────────────────────────────────────────────────────────
    lines.append(f"{agent_iri}")
    lines.append(f"  a syn:Agent, prov:SoftwareAgent, core:Thing ;")
    lines.append(f'  rdfs:label "{_esc(name)}" ;')
    if description:
        lines.append(f'  dcterms:description "{_esc(description)}" ;')
    lines.append(f'  syn:actorId "{_esc(actor_id)}" ;')
    for p in all_props:
        lines.append(f"  ssn:hasProperty {p} ;")
    lines.append(f'  prov:generatedAtTime "{ts_dt}"^^xsd:dateTime ;')
    lines.append(f"  prov:wasAttributedTo {MANIFEST_BRIDGE_IRI} .")
    lines.append("")

    # ── Input schema → ActuatableProperty ────────────────────────────────────
    for field, spec in input_schema.items():
        prop_iri = _aprop_iri(actor_id, "in", field)
        desc = _parse_schema_desc(spec)
        lines.append(f"{prop_iri}")
        lines.append(f"  a sosa:ActuatableProperty ;")
        lines.append(f'  rdfs:label "{_esc(field)}" ;')
        if desc:
            lines.append(f'  rdfs:comment "{_esc(desc)}" ;')
        lines.append(f"  ssn:isPropertyOf {agent_iri} ;")
        lines.append(f"  syn:claimedBy {agent_iri} .")
        lines.append("")

    # ── Output schema → ObservableProperty ───────────────────────────────────
    for field, spec in output_schema.items():
        prop_iri = _aprop_iri(actor_id, "out", field)
        desc = _parse_schema_desc(spec)
        lines.append(f"{prop_iri}")
        lines.append(f"  a sosa:ObservableProperty ;")
        lines.append(f'  rdfs:label "{_esc(field)}" ;')
        if desc:
            lines.append(f'  rdfs:comment "{_esc(desc)}" ;')
        lines.append(f"  ssn:isPropertyOf {agent_iri} ;")
        lines.append(f"  syn:claimedBy {agent_iri} .")
        lines.append("")

    # ── Capabilities → syn:Action ─────────────────────────────────────────────
    for cap in capabilities:
        if not isinstance(cap, str):
            continue
        cap_iri = _aprop_iri(actor_id, "cap", cap)
        lines.append(f"{cap_iri}")
        lines.append(f"  a syn:Action ;")
        lines.append(f'  rdfs:label "{_esc(cap)}" ;')
        lines.append(f"  syn:claimedBy {agent_iri} .")
        lines.append("")

    # ── Published MQTT topics → ObservableProperty ───────────────────────────
    for topic in publishes:
        if not isinstance(topic, str):
            continue
        pub_iri = _aprop_iri(actor_id, "pub", topic)
        lines.append(f"{pub_iri}")
        lines.append(f"  a sosa:ObservableProperty ;")
        lines.append(f'  rdfs:label "{_esc(topic)}" ;')
        lines.append(f'  syn:publishesTopic "{_esc(topic)}" ;')
        lines.append(f"  syn:claimedBy {agent_iri} .")
        lines.append("")

    return "\n".join(lines)


# ── Fuseki GSP / SPARQL Update client ────────────────────────────────────────

class FusekiClient:
    """Thin async wrapper around Fuseki GSP and SPARQL Update endpoints."""

    def __init__(
        self,
        fuseki_url: str,
        dataset: str,
        session: aiohttp.ClientSession,
        auth: aiohttp.BasicAuth | None = None,
    ) -> None:
        self._base = fuseki_url.rstrip("/")
        self._ds = dataset
        self._session = session
        self._auth = auth

    def _gsp_url(self, graph: str) -> str:
        return f"{self._base}/{self._ds}/data?graph={quote(graph, safe='')}"

    def _update_url(self) -> str:
        return f"{self._base}/{self._ds}/update"

    async def replace_graph(self, graph: str, ttl: str) -> None:
        """PUT — replace the whole named graph."""
        url = self._gsp_url(graph)
        async with self._session.put(
            url,
            data=ttl.encode(),
            headers={"Content-Type": "text/turtle"},
            auth=self._auth,
        ) as resp:
            if resp.status not in (200, 201, 204):
                body = await resp.text()
                log.error("Fuseki PUT %s → %s: %s", graph, resp.status, body[:300])

    async def append_graph(self, graph: str, ttl: str) -> None:
        """POST — append triples to a named graph."""
        url = self._gsp_url(graph)
        async with self._session.post(
            url,
            data=ttl.encode(),
            headers={"Content-Type": "text/turtle"},
            auth=self._auth,
        ) as resp:
            if resp.status not in (200, 201, 204):
                body = await resp.text()
                log.error("Fuseki POST %s → %s: %s", graph, resp.status, body[:300])

    async def sparql_update(self, query: str) -> None:
        """Execute a SPARQL Update statement."""
        async with self._session.post(
            self._update_url(),
            data={"update": query},
            auth=self._auth,
        ) as resp:
            if resp.status not in (200, 204):
                body = await resp.text()
                log.warning("SPARQL Update → %s: %s", resp.status, body[:300])

    async def replace_agent_in_graph(
        self, graph: str, actor_id: str, ttl: str
    ) -> None:
        """
        Atomically replace all triples for *actor_id* and its properties in *graph*:
          1. SPARQL DELETE WHERE for the agent IRI and all properties it owns
          2. GSP POST to append the new triples
        """
        full_iri = f"urn:wactorz:agent:{_safe(actor_id)}"

        delete_q = f"""
PREFIX ssn:  <http://www.w3.org/ns/ssn/>

DELETE {{
  GRAPH <{graph}> {{
    ?prop ?pp ?po .
    <{full_iri}> ?ap ?ao .
  }}
}}
WHERE {{
  GRAPH <{graph}> {{
    OPTIONAL {{
      <{full_iri}> ssn:hasProperty ?prop .
      ?prop ?pp ?po .
    }}
    OPTIONAL {{ <{full_iri}> ?ap ?ao . }}
  }}
}}
"""
        await self.sparql_update(delete_q)
        await self.append_graph(graph, ttl)

    async def replace_entity_in_graph(
        self, graph: str, entity_id: str, ttl: str
    ) -> None:
        full_iri = f"urn:ha:entity:{_safe(entity_id)}"

        delete_q = f"""
PREFIX syn:  <https://synapse.waldiez.io/ns#>
PREFIX sosa: <http://www.w3.org/ns/sosa/>

DELETE {{
  GRAPH <{graph}> {{
    ?obs ?op ?oo .
    <{full_iri}> ?ep ?eo .
  }}
}}
WHERE {{
  GRAPH <{graph}> {{
    OPTIONAL {{
      <{full_iri}> syn:hasCurrentObservation ?obs .
      ?obs ?op ?oo .
    }}
    OPTIONAL {{ <{full_iri}> ?ep ?eo . }}
  }}
}}
"""
        await self.sparql_update(delete_q)
        await self.append_graph(graph, ttl)


# ── Bridge ────────────────────────────────────────────────────────────────────

class HAFusekiBridge:
    """Subscribe to Home Assistant events and push RDF to Fuseki."""

    def __init__(
        self,
        ha_url: str,
        ha_token: str,
        fuseki_url: str,
        fuseki_dataset: str,
        fuseki_user: str = "",
        fuseki_password: str = "",
        domains: frozenset[str] | None = None,
    ) -> None:
        self._ha_url = ha_url.rstrip("/")
        self._ha_token = ha_token
        self._fuseki_url = fuseki_url
        self._fuseki_dataset = fuseki_dataset
        self._fuseki_auth: aiohttp.BasicAuth | None = (
            aiohttp.BasicAuth(fuseki_user, fuseki_password)
            if fuseki_user
            else None
        )
        self._domains: frozenset[str] = domains if domains is not None else DEFAULT_DOMAINS
        # area_id → area name lookup built during seed
        self._area_names: dict[str, str] = {}

    def _ws_url(self) -> str:
        parsed = urlparse(self._ha_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}/api/websocket"

    def _want(self, entity_id: str) -> bool:
        return entity_id.split(".")[0] in self._domains

    async def run(self) -> None:
        connector = aiohttp.TCPConnector(ssl=False, force_close=True)
        async with aiohttp.ClientSession(connector=connector) as http:
            fuseki = FusekiClient(
                self._fuseki_url, self._fuseki_dataset, http, self._fuseki_auth
            )
            ws_url = self._ws_url()
            log.info("Connecting to HA: %s", ws_url)

            async with HAWebSocketClient(ws_url, self._ha_token) as ha:
                log.info("Authenticated. Loading initial states …")
                await self._seed(ha, fuseki)

                sub_id = await ha.subscribe_events("state_changed")
                log.info("Subscribed (id=%d). Listening for state_changed …", sub_id)

                while True:
                    try:
                        msg = await ha.receive_event(sub_id)
                        await self._on_event(msg, fuseki)
                    except Exception as exc:
                        log.exception("Event handling error: %s", exc)

    # ── Seed on startup ───────────────────────────────────────────────────────

    async def _seed(self, ha: HAWebSocketClient, fuseki: FusekiClient) -> None:
        # ── 1. Fetch areas, entity registry, and device registry ─────────────
        areas: list[dict[str, Any]] = []
        entity_area_map: dict[str, str] = {}  # entity_id → area_id

        try:
            areas = await ha.call("config/area_registry/list") or []
            log.info("Fetched %d areas from HA.", len(areas))
        except Exception as exc:
            log.warning("Could not fetch area registry: %s", exc)

        # device_id → area_id (fallback when entity has no direct area)
        device_area_map: dict[str, str] = {}
        try:
            device_registry: list[dict[str, Any]] = (
                await ha.call("config/device_registry/list") or []
            )
            for dev in device_registry:
                did = dev.get("id", "")
                aid = dev.get("area_id") or ""
                if did and aid:
                    device_area_map[did] = aid
            log.info(
                "Device registry: %d devices, %d with area assignments.",
                len(device_registry),
                len(device_area_map),
            )
        except Exception as exc:
            log.warning("Could not fetch device registry: %s", exc)

        try:
            entity_registry: list[dict[str, Any]] = (
                await ha.call("config/entity_registry/list") or []
            )
            for entry in entity_registry:
                eid = entry.get("entity_id", "")
                # Prefer entity-level area, fall back to device-level area
                aid = (
                    entry.get("area_id")
                    or device_area_map.get(entry.get("device_id", ""), "")
                )
                if eid and aid:
                    entity_area_map[eid] = aid
            log.info(
                "Entity registry: %d entries, %d with area assignments (incl. device fallback).",
                len(entity_registry),
                len(entity_area_map),
            )
        except Exception as exc:
            log.warning("Could not fetch entity registry: %s", exc)

        # Build area_id → name lookup
        self._area_names = {
            a["area_id"]: a.get("name", a["area_id"])
            for a in areas
            if "area_id" in a
        }

        # ── 2. Push areas graph ───────────────────────────────────────────────
        if areas:
            area_body_parts = [_bridge_agent_body()]
            for area in areas:
                area_body_parts.append(_area_body(area))
            await fuseki.replace_graph(
                GRAPH_AREAS, _ttl("\n".join(area_body_parts))
            )
            log.info("Areas graph replaced (%d areas).", len(areas))
        else:
            log.info("No areas found — skipping areas graph.")

        # ── 3. Fetch states ───────────────────────────────────────────────────
        all_states: list[dict[str, Any]] = await ha.call("get_states") or []
        wanted = [s for s in all_states if self._want(s.get("entity_id", ""))]
        log.info(
            "Seeding %d / %d entities → Fuseki …", len(wanted), len(all_states)
        )

        # ── 4. Devices catalog (full replace, with area info) ─────────────────
        catalog_body_parts = [_bridge_agent_body()]
        for s in wanted:
            eid = s["entity_id"]
            area_id = entity_area_map.get(eid)
            area_name = self._area_names.get(area_id, "") if area_id else None
            catalog_body_parts.append(
                _device_body(eid, s, area_id=area_id, area_name=area_name)
            )

        await fuseki.replace_graph(
            GRAPH_DEVICES, _ttl("\n".join(catalog_body_parts))
        )
        log.info("Devices catalog replaced (%d entities).", len(wanted))

        # ── 5. Current-state graph (patch per entity) ─────────────────────────
        ts_ms = int(time.time() * 1000)
        for s in wanted:
            eid = s["entity_id"]
            body = _current_obs_body(eid, s, ts_ms)
            await fuseki.replace_entity_in_graph(GRAPH_CURRENT, eid, _ttl(body))

        log.info("Current-state graph seeded.")

    # ── Live events ───────────────────────────────────────────────────────────

    async def _on_event(
        self, msg: dict[str, Any], fuseki: FusekiClient
    ) -> None:
        event = msg.get("event") or {}
        data = event.get("data") or {}
        entity_id: str = data.get("entity_id", "")
        new_state: dict[str, Any] | None = data.get("new_state")

        if not entity_id or not new_state or not self._want(entity_id):
            return

        ts_ms = int(time.time() * 1000)
        log.debug(
            "state_changed  %s → %s", entity_id, new_state.get("state", "?")
        )

        current_body = _current_obs_body(entity_id, new_state, ts_ms)
        await fuseki.replace_entity_in_graph(
            GRAPH_CURRENT, entity_id, _ttl(current_body)
        )

        hist_body = _history_obs_body(entity_id, new_state, ts_ms)
        await fuseki.append_graph(GRAPH_HISTORY, _ttl(hist_body))


# ── Agent registry seed ──────────────────────────────────────────────────────

async def seed_agent_registry(
    actors: list[Any],
    fuseki_url: str,
    fuseki_dataset: str,
    fuseki_user: str = "",
    fuseki_password: str = "",
) -> None:
    """One-shot: write all currently registered actors to urn:wactorz:agents graph."""
    if not fuseki_url or not fuseki_dataset:
        return
    auth = aiohttp.BasicAuth(fuseki_user, fuseki_password) if fuseki_user else None
    lines: list[str] = [TTL_PREFIXES]
    lines.append(
        "<urn:wactorz:bridge:agent-registry>\n"
        '  rdfs:label "wactorz agent registry bridge" .\n'
    )
    for actor in actors:
        name = getattr(actor, "name", "unknown")
        actor_id = getattr(actor, "actor_id", name)
        state = str(getattr(actor, "state", "running"))
        protected = getattr(actor, "protected", False)
        iri = f"<urn:wactorz:agent:{_safe(name)}>"
        lines.append(
            f"{iri}\n"
            f"  rdfs:label {_literal(name)} ;\n"
            f"  syn:actorId {_literal(actor_id)} ;\n"
            f"  syn:state {_literal(state)} ;\n"
            f'  syn:protected "{str(protected).lower()}"^^xsd:boolean .\n'
        )
    ttl = "\n".join(lines)
    connector = aiohttp.TCPConnector(ssl=False, force_close=True)
    try:
        async with aiohttp.ClientSession(connector=connector) as http:
            fuseki = FusekiClient(fuseki_url, fuseki_dataset, http, auth)
            await fuseki.replace_graph(GRAPH_AGENTS, ttl)
            log.info("Agent registry seeded: %d actors → Fuseki", len(actors))
    except Exception as exc:
        log.warning("Agent registry seed failed (Fuseki not ready?): %s", exc)


# ── Agent manifest bridge ────────────────────────────────────────────────────

class AgentManifestBridge:
    """Subscribe to agents/+/manifest MQTT and push RDF to Fuseki."""

    def __init__(
        self,
        mqtt_broker: str,
        mqtt_port: int,
        fuseki_url: str,
        fuseki_dataset: str,
        fuseki_user: str = "",
        fuseki_password: str = "",
    ) -> None:
        self._mqtt_broker = mqtt_broker  # pyright: ignore[reportUnannotatedClassAttribute]
        self._mqtt_port = mqtt_port  # pyright: ignore[reportUnannotatedClassAttribute]
        self._fuseki_url = fuseki_url  # pyright: ignore[reportUnannotatedClassAttribute]
        self._fuseki_dataset = fuseki_dataset  # pyright: ignore[reportUnannotatedClassAttribute]
        self._fuseki_auth: aiohttp.BasicAuth | None = (  # pyright: ignore[reportUnannotatedClassAttribute]
            aiohttp.BasicAuth(fuseki_user, fuseki_password) if fuseki_user else None
        )

    async def run(self) -> None:
        connector = aiohttp.TCPConnector(ssl=False, force_close=True)
        async with aiohttp.ClientSession(connector=connector) as http:
            fuseki = FusekiClient(
                self._fuseki_url, self._fuseki_dataset, http, self._fuseki_auth
            )
            # Register the bridge itself (best-effort — Fuseki may not be ready yet)
            bridge_body = (
                f"{MANIFEST_BRIDGE_IRI}\n"
                f"  a syn:Agent, prov:SoftwareAgent ;\n"
                f'  rdfs:label "wactorz agent-manifest bridge" .\n'
            )
            try:
                await fuseki.append_graph(GRAPH_AGENTS, _ttl(bridge_body))
            except Exception as exc:
                log.warning("AgentManifestBridge: Fuseki not ready yet (%s) — will retry on first manifest", exc)

            async with aiomqtt.Client(self._mqtt_broker, self._mqtt_port) as client:
                await client.subscribe("agents/+/manifest")
                log.info(
                    "AgentManifestBridge listening on %s:%d agents/+/manifest",
                    self._mqtt_broker, self._mqtt_port,
                )
                async for message in client.messages:
                    try:
                        manifest: dict[str, Any] = json.loads(bytes(message.payload))
                        await self._on_manifest(manifest, fuseki)
                    except Exception as exc:
                        log.exception("Manifest handling error: %s", exc)

    async def _on_manifest(
        self, manifest: dict[str, Any], fuseki: FusekiClient
    ) -> None:
        actor_id = str(manifest.get("actor_id") or manifest.get("name") or "")
        if not actor_id:
            log.warning("Manifest missing actor_id/name — skipping")
            return
        name = manifest.get("name", actor_id)
        log.debug("agent manifest: %s (%s)", name, actor_id)
        body = _agent_manifest_body(manifest)
        await fuseki.replace_agent_in_graph(GRAPH_AGENTS, actor_id, _ttl(body))
        log.info("Upserted agent manifest: %s → %s", name, GRAPH_AGENTS)


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def _parse_domains(raw: str | None) -> frozenset[str] | None:
    if not raw:
        return None
    return frozenset(d.strip() for d in raw.split(",") if d.strip())


async def _run_with_retry(coro_factory: Any, label: str) -> None:
    """Run a bridge coroutine, reconnecting on error."""
    while True:
        try:
            await coro_factory()
        except KeyboardInterrupt:
            log.info("%s shutting down.", label)
            break
        except Exception as exc:
            log.error("%s error: %s — reconnecting in 10 s …", label, exc)
            await asyncio.sleep(10)


async def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
    )

    ha_url        = os.environ.get("HA_URL", "http://homeassistant.local:8123")
    ha_token      = os.environ.get("HA_TOKEN", "")
    fuseki_url    = os.environ.get("FUSEKI_URL", "http://localhost:3030")
    fuseki_dataset = os.environ.get("FUSEKI_DATASET", "wactorz")
    fuseki_user   = os.environ.get("FUSEKI_USER", "admin")
    fuseki_password = os.environ.get("FUSEKI_PASSWORD", "")
    domains       = _parse_domains(os.environ.get("HA_DOMAINS"))
    mqtt_broker   = os.environ.get("MQTT_BROKER", "localhost")
    mqtt_port     = int(os.environ.get("MQTT_PORT", "1883"))

    log.info(
        "Bridges starting  fuseki=%s/%s  auth=%s",
        fuseki_url, fuseki_dataset, "yes" if fuseki_password else "none",
    )

    tasks: list[Any] = []

    # ── HA bridge (optional — skipped if no HA_TOKEN) ────────────────────────
    if ha_token:
        log.info("HA→Fuseki bridge  ha=%s  domains=%s",
                 ha_url, ",".join(sorted(domains)) if domains else "default")
        ha_bridge = HAFusekiBridge(
            ha_url=ha_url,
            ha_token=ha_token,
            fuseki_url=fuseki_url,
            fuseki_dataset=fuseki_dataset,
            fuseki_user=fuseki_user,
            fuseki_password=fuseki_password,
            domains=domains,
        )
        tasks.append(_run_with_retry(ha_bridge.run, "HAFusekiBridge"))
    else:
        log.info("HA_TOKEN not set — HAFusekiBridge disabled.")

    # ── Agent manifest bridge ─────────────────────────────────────────────────
    log.info("AgentManifestBridge  mqtt=%s:%d", mqtt_broker, mqtt_port)
    agent_bridge = AgentManifestBridge(
        mqtt_broker=mqtt_broker,
        mqtt_port=mqtt_port,
        fuseki_url=fuseki_url,
        fuseki_dataset=fuseki_dataset,
        fuseki_user=fuseki_user,
        fuseki_password=fuseki_password,
    )
    tasks.append(_run_with_retry(agent_bridge.run, "AgentManifestBridge"))

    await asyncio.gather(*tasks)


def _cli_main() -> None:
    """Sync entry point for the ``wactorz-fuseki`` console script."""
    asyncio.run(_main())


if __name__ == "__main__":
    _cli_main()
