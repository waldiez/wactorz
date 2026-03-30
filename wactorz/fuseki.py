"""
wactorz/fuseki.py  -  Home Assistant → Apache Jena Fuseki bridge.

Subscribes to HA ``state_changed`` events and pushes RDF/Turtle to Fuseki
using the Graph Store Protocol (GSP) and SPARQL Update.

Ontology prefixes used (all inline - no external file references):
  core:   HSML Core  <https://www.spatialwebfoundation.org/ns/hsml/core#>
  saref:  SAREF Core <https://saref.etsi.org/core/>
  sosa:   SOSA       <http://www.w3.org/ns/sosa/>
  ssn:    SSN        <http://www.w3.org/ns/ssn/>
  syn:    SYNAPSE    <https://synapse.waldiez.io/ns#>
  prov:   PROV-O     <http://www.w3.org/ns/prov#>

Named graphs managed:
  urn:ha:current  - latest state per entity  (DELETE + INSERT on every change)
  urn:ha:history  - append-only observations
  urn:ha:devices  - entity catalog            (rebuilt on startup)

Usage::

    python -m wactorz.fuseki

Environment variables:
    HA_URL          Home Assistant base URL  (default: http://homeassistant.local:8123)
    HA_TOKEN        Long-lived access token  (required)
    FUSEKI_URL      Fuseki base URL          (default: http://localhost:3030)
    FUSEKI_DATASET  Fuseki dataset name      (default: wactorz)
    FUSEKI_USER     Fuseki admin user        (default: admin)
    FUSEKI_PASSWORD Fuseki admin password    (default: empty = no auth)
    HA_DOMAINS      Comma-separated domains  (default: see DEFAULT_DOMAINS)
"""

# cspell: disable
# flake8: noqa: E501
# pyright: reportAny=false,reportExplicitAny=false,reportUnusedCallResult=false
# pyright: reportUnknownVariableType=false,reportUnknownMemberType=false,reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import aiohttp

from wactorz.core.integrations.home_assistant.ha_web_socket_client import (
    HAWebSocketClient,
)

log = logging.getLogger("wactorz.fuseki")

# ── Shared Turtle prefix block ────────────────────────────────────────────────

TTL_PREFIXES = """\
@prefix rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs:  <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:   <http://www.w3.org/2001/XMLSchema#> .
@prefix owl:   <http://www.w3.org/2002/07/owl#> .
@prefix prov:  <http://www.w3.org/ns/prov#> .
@prefix sosa:  <http://www.w3.org/ns/sosa/> .
@prefix ssn:   <http://www.w3.org/ns/ssn/> .
@prefix saref: <https://saref.etsi.org/core/> .
@prefix core:  <https://www.spatialwebfoundation.org/ns/hsml/core#> .
@prefix syn:   <https://synapse.waldiez.io/ns#> .
@prefix ha:    <urn:ha:entity:> .
@prefix haobs: <urn:ha:obs:> .
@prefix haprop: <urn:ha:prop:> .
"""

# Provenance IRI for this bridge process
BRIDGE_AGENT_IRI = "<urn:ha:bridge:wactorz>"

# Named graph IRIs
GRAPH_CURRENT = "urn:ha:current"
GRAPH_HISTORY = "urn:ha:history"
GRAPH_DEVICES = "urn:ha:devices"

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


def _device_body(entity_id: str, state_obj: dict[str, Any]) -> str:
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
    """
    Current-state triples for one entity:
      - entity → syn:hasCurrentObservation / syn:state
      - the observation node
    """
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

    # Entity pointer
    lines.append(f"{iri}")
    lines.append(f"  syn:hasCurrentObservation {obs_iri} ;")
    lines.append(f"  syn:state {_literal(state_val)} .")
    lines.append("")

    # Observation
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
    """Append-only observation for the history graph (no entity pointer)."""
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
        self._base = fuseki_url.rstrip("/")  # pyright: ignore[reportUnannotatedClassAttribute]
        self._ds = dataset  # pyright: ignore[reportUnannotatedClassAttribute]
        self._session = session  # pyright: ignore[reportUnannotatedClassAttribute]
        self._auth = auth  # pyright: ignore[reportUnannotatedClassAttribute]

    # GSP endpoints
    def _gsp_url(self, graph: str) -> str:
        return f"{self._base}/{self._ds}/data?graph={graph}"

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

    async def replace_entity_in_graph(
        self, graph: str, entity_id: str, ttl: str
    ) -> None:
        """
        Atomically replace all triples about *entity_id* and its current
        observation in *graph*:
          1. SPARQL DELETE WHERE for the entity IRI and its obs nodes
          2. GSP POST to append the new triples
        """
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
        self._ha_url = ha_url.rstrip("/")  # pyright: ignore[reportUnannotatedClassAttribute]
        self._ha_token = ha_token  # pyright: ignore[reportUnannotatedClassAttribute]
        self._fuseki_url = fuseki_url  # pyright: ignore[reportUnannotatedClassAttribute]
        self._fuseki_dataset = fuseki_dataset  # pyright: ignore[reportUnannotatedClassAttribute]
        self._fuseki_auth: aiohttp.BasicAuth | None = (  # pyright: ignore[reportUnannotatedClassAttribute]
            aiohttp.BasicAuth(fuseki_user, fuseki_password)
            if fuseki_user
            else None
        )
        self._domains: frozenset[str] = domains if domains is not None else DEFAULT_DOMAINS

    def _ws_url(self) -> str:
        parsed = urlparse(self._ha_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}/api/websocket"

    def _want(self, entity_id: str) -> bool:
        return entity_id.split(".")[0] in self._domains

    async def run(self) -> None:
        connector = aiohttp.TCPConnector(ssl=False)
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
        all_states: list[dict[str, Any]] = await ha.call("get_states") or []
        wanted = [s for s in all_states if self._want(s.get("entity_id", ""))]
        log.info(
            "Seeding %d / %d entities → Fuseki …", len(wanted), len(all_states)
        )

        # ── Devices catalog (full replace) ────────────────────────────────────
        catalog_body_parts = [_bridge_agent_body()]
        for s in wanted:
            catalog_body_parts.append(_device_body(s["entity_id"], s))

        await fuseki.replace_graph(
            GRAPH_DEVICES, _ttl("\n".join(catalog_body_parts))
        )
        log.info("Devices catalog replaced (%d entities).", len(wanted))

        # ── Current-state graph (patch per entity) ────────────────────────────
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

        # Update current-state graph
        current_body = _current_obs_body(entity_id, new_state, ts_ms)
        await fuseki.replace_entity_in_graph(
            GRAPH_CURRENT, entity_id, _ttl(current_body)
        )

        # Append to history
        hist_body = _history_obs_body(entity_id, new_state, ts_ms)
        await fuseki.append_graph(GRAPH_HISTORY, _ttl(hist_body))


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def _parse_domains(raw: str | None) -> frozenset[str] | None:
    if not raw:
        return None
    return frozenset(d.strip() for d in raw.split(",") if d.strip())


async def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
    )

    ha_url = os.environ.get("HA_URL", "http://homeassistant.local:8123")
    ha_token = os.environ.get("HA_TOKEN", "")
    fuseki_url = os.environ.get("FUSEKI_URL", "http://localhost:3030")
    fuseki_dataset = os.environ.get("FUSEKI_DATASET", "wactorz")
    fuseki_user = os.environ.get("FUSEKI_USER", "admin")
    fuseki_password = os.environ.get("FUSEKI_PASSWORD", "")
    domains = _parse_domains(os.environ.get("HA_DOMAINS"))

    if not ha_token:
        raise SystemExit("HA_TOKEN environment variable is required.")

    log.info(
        "HA→Fuseki bridge  ha=%s  fuseki=%s/%s  auth=%s  domains=%s",
        ha_url,
        fuseki_url,
        fuseki_dataset,
        "yes" if fuseki_password else "none",
        ",".join(sorted(domains)) if domains else "default",
    )

    bridge = HAFusekiBridge(
        ha_url=ha_url,
        ha_token=ha_token,
        fuseki_url=fuseki_url,
        fuseki_dataset=fuseki_dataset,
        fuseki_user=fuseki_user,
        fuseki_password=fuseki_password,
        domains=domains,
    )

    while True:
        try:
            await bridge.run()
        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception as exc:
            log.error("Bridge error: %s — reconnecting in 10 s …", exc)
            await asyncio.sleep(10)


def _cli_main() -> None:
    """Sync entry point for the ``wactorz-fuseki`` console script."""
    asyncio.run(_main())


if __name__ == "__main__":
    _cli_main()
