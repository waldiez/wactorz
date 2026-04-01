"""
wactorz/mcp/fuseki_server.py — MCP server for Apache Jena Fuseki.

Exposes SPARQL query, ASK, update, and dataset-listing tools via the
Model Context Protocol so any MCP-capable client (Claude Desktop, etc.)
can interact with your Fuseki triple store.

Usage::

    wactorz-mcp-fuseki

Environment variables:
    FUSEKI_URL      Fuseki base URL     (default: http://localhost:3030)
    FUSEKI_DATASET  Dataset name        (default: /ds)
    FUSEKI_USER     Admin user          (default: admin)
    FUSEKI_PASSWORD Admin password      (default: empty = no auth)
"""

from __future__ import annotations

import json
import os
from typing import Any

try:
    import aiohttp
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "MCP dependencies are not installed. "
        "Run: pip install wactorz[mcp]"
    ) from exc

# ── Config ────────────────────────────────────────────────────────────────────

def _fuseki_base() -> str:
    return os.environ.get("FUSEKI_URL", "http://localhost:3030").rstrip("/")


def _fuseki_dataset() -> str:
    ds = os.environ.get("FUSEKI_DATASET", "/ds")
    return ds if ds.startswith("/") else f"/{ds}"


def _auth() -> aiohttp.BasicAuth | None:
    user = os.environ.get("FUSEKI_USER", "")
    password = os.environ.get("FUSEKI_PASSWORD", "")
    return aiohttp.BasicAuth(user, password) if user else None


def _sparql_endpoint() -> str:
    return f"{_fuseki_base()}{_fuseki_dataset()}/sparql"


def _update_endpoint() -> str:
    return f"{_fuseki_base()}{_fuseki_dataset()}/update"


def _admin_endpoint() -> str:
    return f"{_fuseki_base()}/$/datasets"


_TIMEOUT = aiohttp.ClientTimeout(total=30)

# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "wactorz-fuseki",
    instructions=(
        "SPARQL interface to an Apache Jena Fuseki triple store. "
        "Use sparql_query for SELECT/CONSTRUCT/DESCRIBE, sparql_ask for ASK "
        "queries, sparql_update for INSERT/DELETE, and list_datasets to "
        "discover available datasets."
    ),
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_select(data: dict[str, Any]) -> str:
    """Format SPARQL SELECT results as a readable table string."""
    vars_ = data.get("head", {}).get("vars", [])
    bindings = data.get("results", {}).get("bindings", [])

    if not bindings:
        return f"Query returned 0 rows. Columns: {', '.join(vars_)}"

    rows: list[str] = [f"Rows: {len(bindings)}  |  Columns: {', '.join(vars_)}\n"]
    for i, row in enumerate(bindings[:50]):
        parts = []
        for var in vars_:
            cell = row.get(var, {})
            val = cell.get("value", "")
            if cell.get("type") == "uri":
                for pfx, ns in (
                    ("rdf", "http://www.w3.org/1999/02/22-rdf-syntax-ns#"),
                    ("rdfs", "http://www.w3.org/2000/01/rdf-schema#"),
                    ("owl", "http://www.w3.org/2002/07/owl#"),
                    ("xsd", "http://www.w3.org/2001/XMLSchema#"),
                    ("sosa", "http://www.w3.org/ns/sosa/"),
                    ("ssn", "http://www.w3.org/ns/ssn/"),
                    ("saref", "https://saref.etsi.org/core/"),
                ):
                    if val.startswith(ns):
                        val = f"{pfx}:{val[len(ns):]}"
                        break
            parts.append(f"{var}={val!r}")
        rows.append(f"{i + 1}. {' | '.join(parts)}")

    if len(bindings) > 50:
        rows.append(f"... and {len(bindings) - 50} more rows (truncated)")

    return "\n".join(rows)


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def sparql_query(query: str) -> str:
    """Execute a SPARQL SELECT, CONSTRUCT, or DESCRIBE query against Fuseki.

    Args:
        query: A valid SPARQL 1.1 query string.

    Returns:
        Formatted query results, or an error message.
    """
    endpoint = _sparql_endpoint()
    params = {"query": query, "format": "json"}
    headers = {"Accept": "application/sparql-results+json,application/ld+json,application/json"}

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(
                endpoint, params=params, headers=headers, auth=_auth()
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return f"Error HTTP {resp.status} from Fuseki:\n{body[:500]}"
                data: dict[str, Any] = await resp.json(content_type=None)
    except Exception as exc:
        return f"Query failed: {exc}"

    # CONSTRUCT / DESCRIBE return JSON-LD or Turtle — pass through as-is
    if "results" not in data and "@graph" not in data:
        return json.dumps(data, indent=2)

    return _format_select(data)


@mcp.tool()
async def sparql_ask(query: str) -> str:
    """Execute a SPARQL ASK query against Fuseki.

    Args:
        query: A SPARQL ASK query. The ASK keyword may be omitted — it will be
               added automatically if missing.

    Returns:
        'true' or 'false', or an error message.
    """
    if not query.upper().lstrip().startswith("ASK"):
        query = f"ASK {{ {query} }}"

    endpoint = _sparql_endpoint()
    params = {"query": query, "format": "json"}
    headers = {"Accept": "application/sparql-results+json,application/json"}

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(
                endpoint, params=params, headers=headers, auth=_auth()
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return f"Error HTTP {resp.status} from Fuseki:\n{body[:300]}"
                data: dict[str, Any] = await resp.json(content_type=None)
    except Exception as exc:
        return f"ASK query failed: {exc}"

    return "true" if data.get("boolean") else "false"


@mcp.tool()
async def sparql_update(update: str) -> str:
    """Execute a SPARQL Update statement (INSERT DATA, DELETE DATA, etc.).

    Requires write access to the Fuseki dataset. Set FUSEKI_USER and
    FUSEKI_PASSWORD environment variables if the dataset is protected.

    Args:
        update: A valid SPARQL 1.1 Update string.

    Returns:
        'ok' on success, or an error message.
    """
    endpoint = _update_endpoint()

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                endpoint, data={"update": update}, auth=_auth()
            ) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    return f"Error HTTP {resp.status} from Fuseki:\n{body[:300]}"
    except Exception as exc:
        return f"Update failed: {exc}"

    return "ok"


@mcp.tool()
async def list_datasets() -> str:
    """List all datasets available on the Fuseki server.

    Returns:
        A list of dataset names and their states, or an error message.
    """
    endpoint = _admin_endpoint()

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(endpoint, auth=_auth()) as resp:
                if resp.status != 200:
                    return (
                        f"Could not list datasets (HTTP {resp.status}). "
                        "Is the Fuseki admin API enabled?"
                    )
                data: dict[str, Any] = await resp.json(content_type=None)
    except Exception as exc:
        return f"Could not reach Fuseki at {_fuseki_base()}: {exc}"

    datasets = data.get("datasets") or []
    if not datasets:
        return "No datasets found on this Fuseki server."

    lines = [f"Fuseki server: {_fuseki_base()}\n"]
    for ds in datasets:
        name = ds.get("ds.name", "?")
        state = ds.get("ds.state", "?")
        lines.append(f"- {name}  [{state}]")

    return "\n".join(lines)


@mcp.tool()
async def get_named_graphs() -> str:
    """List all named graphs in the current dataset.

    Returns:
        A list of named graph IRIs, or an error message.
    """
    query = "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } } ORDER BY ?g"
    return await sparql_query(query)


# ── Entry point ───────────────────────────────────────────────────────────────

def cli_main() -> None:
    """Sync entry point for the ``wactorz-mcp-fuseki`` console script."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    cli_main()
