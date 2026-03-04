"""
FusekiAgent — SPARQL knowledge-graph interface (NATO: FERN / Foxtrot).

Connects to an Apache Jena Fuseki triple store and executes SPARQL queries.
No API key required — uses the standard SPARQL 1.1 HTTP protocol.

Environment:
  FUSEKI_URL     Fuseki base URL (default: http://fuseki:3030)
  FUSEKI_DATASET Dataset name (default: /ds)

Commands (prefix @fern-agent or @fuseki-agent stripped automatically):
  query <sparql>          execute a SELECT or CONSTRUCT query
  ask <sparql>            execute an ASK query → true/false
  prefixes                list common RDF prefix bindings
  datasets                list available Fuseki datasets
  help                    show commands
"""

from __future__ import annotations

import logging
import os
import time

from ..core.actor import Actor, Message, MessageType

logger = logging.getLogger(__name__)

_DEFAULT_URL     = os.getenv("FUSEKI_URL", "http://fuseki:3030")
_DEFAULT_DATASET = os.getenv("FUSEKI_DATASET", "/ds")
_TIMEOUT         = 20

_COMMON_PREFIXES = """\
PREFIX rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dc:    <http://purl.org/dc/elements/1.1/>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX foaf:  <http://xmlns.com/foaf/0.1/>
PREFIX schema: <https://schema.org/>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>"""

_HELP = """\
**FERN — FusekiAgent** 🌿
_SPARQL knowledge-graph interface_

```
query <sparql>       SELECT / CONSTRUCT / DESCRIBE query
ask <sparql>         ASK query → true or false
prefixes             list common RDF prefix bindings
datasets             list available Fuseki datasets
help                 this message
```

**Examples:**
```sparql
query SELECT * WHERE { ?s ?p ?o } LIMIT 5
ask ASK { <http://example.org/foo> a owl:Class }
```

Set `FUSEKI_URL` and `FUSEKI_DATASET` in your environment.
Default: `http://fuseki:3030/ds`"""


class FusekiAgent(Actor):
    """SPARQL interface to Apache Jena Fuseki."""

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "fern-agent")
        super().__init__(**kwargs)
        self.protected = False
        self._base_url  = _DEFAULT_URL.rstrip("/")
        self._dataset   = _DEFAULT_DATASET if _DEFAULT_DATASET.startswith("/") else f"/{_DEFAULT_DATASET}"

    @property
    def _sparql_endpoint(self) -> str:
        return f"{self._base_url}{self._dataset}/sparql"

    @property
    def _admin_endpoint(self) -> str:
        return f"{self._base_url}/$/datasets"

    async def on_start(self):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/spawn",
            {
                "agentId":   self.actor_id,
                "agentName": self.name,
                "agentType": "librarian",
                "timestamp": time.time(),
            },
        )
        logger.info("[%s] started — endpoint: %s", self.name, self._sparql_endpoint)

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
        # strip agent prefix
        for pfx in ("@fern-agent", "@fern_agent", "@fuseki-agent", "@fuseki_agent"):
            if text.lower().startswith(pfx):
                text = text[len(pfx):].lstrip()
                break
        reply = await self._dispatch(text)
        await self._reply(reply)
        self.metrics.tasks_completed += 1

    async def _dispatch(self, text: str) -> str:
        if not text or text.lower() == "help":
            return _HELP
        lower = text.lower()
        if lower == "prefixes":
            return f"**Common RDF Prefixes:**\n\n```sparql\n{_COMMON_PREFIXES}\n```"
        if lower == "datasets":
            return await self._cmd_datasets()
        if lower.startswith("ask "):
            return await self._cmd_ask(text[4:].strip())
        if lower.startswith("query "):
            return await self._cmd_query(text[6:].strip())
        # try as a raw SPARQL query
        upper = text.upper().lstrip()
        if any(upper.startswith(kw) for kw in ("SELECT", "CONSTRUCT", "DESCRIBE", "PREFIX")):
            return await self._cmd_query(text)
        if upper.startswith("ASK"):
            return await self._cmd_ask(text)
        return f"Unknown command. Type `help`.\n\n{_HELP}"

    async def _cmd_query(self, sparql: str) -> str:
        if not sparql:
            return "Usage: `query <sparql>`\n\nExample: `query SELECT * WHERE { ?s ?p ?o } LIMIT 5`"
        try:
            import aiohttp
        except ImportError:
            return "Error: `aiohttp` is not installed."

        params = {"query": sparql, "format": "json"}
        headers = {"Accept": "application/sparql-results+json,application/json"}
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self._sparql_endpoint, params=params, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        return f"✗ Fuseki returned HTTP {resp.status}:\n```\n{body[:500]}\n```"
                    data = await resp.json(content_type=None)
        except Exception as exc:
            return f"✗ Query failed: {exc}"

        return self._format_results(data)

    async def _cmd_ask(self, sparql: str) -> str:
        if not sparql:
            return "Usage: `ask <sparql>`\n\nExample: `ask ASK { <http://example.org/foo> a owl:Class }`"
        # ensure ASK keyword present
        if not sparql.upper().lstrip().startswith("ASK"):
            sparql = f"ASK {{ {sparql} }}"
        try:
            import aiohttp
        except ImportError:
            return "Error: `aiohttp` is not installed."

        params = {"query": sparql, "format": "json"}
        headers = {"Accept": "application/sparql-results+json,application/json"}
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self._sparql_endpoint, params=params, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        return f"✗ Fuseki returned HTTP {resp.status}:\n```\n{body[:300]}\n```"
                    data = await resp.json(content_type=None)
        except Exception as exc:
            return f"✗ ASK query failed: {exc}"

        boolean = data.get("boolean")
        icon = "✓" if boolean else "✗"
        return f"**ASK Result:** {icon} `{'true' if boolean else 'false'}`\n\nQuery: `{sparql}`"

    async def _cmd_datasets(self) -> str:
        try:
            import aiohttp
        except ImportError:
            return "Error: `aiohttp` is not installed."

        timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self._admin_endpoint) as resp:
                    if resp.status != 200:
                        return f"✗ Could not list datasets (HTTP {resp.status}).\nIs the Fuseki admin API enabled?"
                    data = await resp.json(content_type=None)
        except Exception as exc:
            return f"✗ Could not reach Fuseki at `{self._base_url}`: {exc}"

        datasets = data.get("datasets", []) or []
        if not datasets:
            return "No datasets found on this Fuseki server."
        lines = [f"**Fuseki Datasets** ({self._base_url}):\n"]
        for ds in datasets:
            name = ds.get("ds.name", "?")
            state = ds.get("ds.state", "?")
            lines.append(f"- `{name}` — {state}")
        return "\n".join(lines)

    @staticmethod
    def _format_results(data: dict) -> str:
        # Handle SELECT results
        results = data.get("results", {})
        bindings = results.get("bindings", [])
        vars_ = data.get("head", {}).get("vars", [])

        if not vars_ and not bindings:
            return "Query returned no results."

        if not bindings:
            return f"Query returned 0 rows.\n\nColumns: {', '.join(vars_)}"

        lines = [f"**Results** ({len(bindings)} rows, columns: {', '.join(vars_)}):\n"]
        for i, row in enumerate(bindings[:20]):
            parts = []
            for var in vars_:
                cell = row.get(var, {})
                val = cell.get("value", "")
                t   = cell.get("type", "")
                if t == "uri":
                    # shorten common namespaces
                    for pfx, ns in (
                        ("rdf", "http://www.w3.org/1999/02/22-rdf-syntax-ns#"),
                        ("rdfs", "http://www.w3.org/2000/01/rdf-schema#"),
                        ("owl", "http://www.w3.org/2002/07/owl#"),
                        ("xsd", "http://www.w3.org/2001/XMLSchema#"),
                    ):
                        if val.startswith(ns):
                            val = f"{pfx}:{val[len(ns):]}"
                            break
                parts.append(f"`{var}`={val!r}")
            lines.append(f"{i + 1}. {' | '.join(parts)}")

        if len(bindings) > 20:
            lines.append(f"… and {len(bindings) - 20} more rows")

        return "\n".join(lines)

    async def _reply(self, content: str):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/chat",
            {"from": self.name, "to": "user", "content": content, "timestamp": time.time()},
        )

    def _current_task_description(self) -> str:
        return f"librarian — {self._sparql_endpoint}"
