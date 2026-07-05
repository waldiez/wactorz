"""SWID registry: async persistence + uniqueness guard.

Stores one :class:`Record` per minted SWID and enforces that a SWID maps to
exactly one natural key. Two implementations:

* :class:`InMemoryRegistry` -- dependency-free; tests and single-process use.
* :class:`FusekiSwidRegistry` -- persists to a dedicated named graph in the UDG
  (Jena/Fuseki) via any client exposing async ``sparql_select`` /
  ``sparql_update`` (i.e. :class:`wactorz.fuseki.FusekiClient`).

The registry is storage only; idempotent onboarding lives in
:class:`wactorz.core.swid.service.SwidService`.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, final, runtime_checkable, TYPE_CHECKING

if TYPE_CHECKING:
    from wactorz.fuseki import FusekiClient

# Dedicated named graph for registry triples (kept apart from HA catalog graphs).
REGISTRY_GRAPH = "urn:wactorz:swid-registry"
SWID_NS = "https://spatialwebfoundation.org/ns/swid#"


class SwidCollisionError(Exception):
    """Two different natural keys derived the same SWID (fingerprint collision)."""


@dataclass(frozen=True, slots=True)
class Record:
    """One registered entity."""

    swid: str
    natural_key: str
    document: dict[str, Any]  # pyright: ignore[reportExplicitAny]
    metadata: dict[str, Any] = field(default_factory=dict)  # pyright: ignore[reportExplicitAny]


@runtime_checkable
class Registry(Protocol):
    """Async storage contract for SWID records."""

    async def get(self, swid: str) -> Record | None: ...
    async def find_by_natural_key(self, natural_key: str) -> Record | None: ...
    async def put(self, record: Record) -> None: ...


class InMemoryRegistry:
    """Dict-backed async registry (single-process pilot / tests)."""

    def __init__(self) -> None:
        self._by_swid: dict[str, Record] = {}
        self._by_key: dict[str, Record] = {}

    async def get(self, swid: str) -> Record | None:
        return self._by_swid.get(swid)

    async def find_by_natural_key(self, natural_key: str) -> Record | None:
        return self._by_key.get(natural_key)

    async def put(self, record: Record) -> None:
        existing = self._by_swid.get(record.swid)
        if existing is not None and existing.natural_key != record.natural_key:
            raise SwidCollisionError(
                f"{record.swid} already bound to {existing.natural_key!r}"
            )
        self._by_swid[record.swid] = record
        self._by_key[record.natural_key] = record


def _lit(value: str) -> str:
    """Escape a Python string as a SPARQL/Turtle quoted literal (JSON-escaped)."""
    return json.dumps(value)


@final
class FusekiSwidRegistry:
    """Registry backed by a Fuseki named graph via an async SPARQL client.

    ``client`` must provide::

        async def sparql_update(self, sparql: str) -> None
        async def sparql_select(self, sparql: str) -> list[dict]   # [{var: str}]
    """

    def __init__(
        self, client: "FusekiClient", *, graph: str = REGISTRY_GRAPH, ns: str = SWID_NS
    ) -> None:
        self._client = client
        self._graph = graph
        self._ns = ns

    def _select(self, where: str) -> str:
        return (
            f"PREFIX swid: <{self._ns}>\n"
            f"SELECT ?swid ?naturalKey ?document ?metadata WHERE {{\n"
            f"  GRAPH <{self._graph}> {{\n{where}\n"
            f"    OPTIONAL {{ ?swid swid:metadata ?metadata . }}\n"
            f"  }}\n}}"
        )

    @staticmethod
    def _row_to_record(row: dict[str, Any]) -> Record:  # pyright: ignore[reportExplicitAny]
        return Record(
            swid=row["swid"],  # pyright: ignore[reportAny]
            natural_key=row["naturalKey"],  # pyright: ignore[reportAny]
            document=json.loads(row["document"]),  # pyright: ignore[reportAny]
            metadata=json.loads(row["metadata"]) if row.get("metadata") else {},  # pyright: ignore[reportAny]
        )

    async def get(self, swid: str) -> Record | None:
        where = (
            f"    BIND(<{swid}> AS ?swid)\n"
            f"    ?swid swid:naturalKey ?naturalKey ; swid:document ?document ."
        )
        rows = await self._client.sparql_select(self._select(where))
        return self._row_to_record(rows[0]) if rows else None

    async def find_by_natural_key(self, natural_key: str) -> Record | None:
        where = (
            f"    ?swid swid:naturalKey {_lit(natural_key)} ; swid:document ?document .\n"
            f"    ?swid swid:naturalKey ?naturalKey ."
        )
        rows = await self._client.sparql_select(self._select(where))
        return self._row_to_record(rows[0]) if rows else None

    async def put(self, record: Record) -> None:
        existing = await self.get(record.swid)
        if existing is not None:
            if existing.natural_key != record.natural_key:
                raise SwidCollisionError(
                    f"{record.swid} already bound to {existing.natural_key!r}"
                )
            return  # idempotent: same swid + same key already stored
        insert = (
            f"PREFIX swid: <{self._ns}>\n"
            f"INSERT DATA {{ GRAPH <{self._graph}> {{\n"
            f"  <{record.swid}> swid:naturalKey {_lit(record.natural_key)} ;\n"
            f"                  swid:document   {_lit(json.dumps(record.document))} ;\n"
            f"                  swid:metadata   {_lit(json.dumps(record.metadata))} .\n"
            f"}} }}"
        )
        await self._client.sparql_update(insert)
