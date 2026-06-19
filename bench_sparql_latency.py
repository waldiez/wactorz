"""
bench_sparql_latency.py — measure SPARQL query latency for the
"HSML SPARQL Query Latency < 500ms" KPI.

Runs a fixed set of representative queries against BOTH the Fuseki endpoint
directly AND through the app's proxy, side by side, and reports p50/p95/p99
(not just an average) together with the dataset's triple count — because a
latency number is only meaningful next to the size of the graph it was measured
on. Writes a CSV and a JSON report and prints a pass/fail table against 500ms.

Run it from anywhere that can reach Fuseki (and, for the proxy column, the app):

    python bench_sparql_latency.py

Env vars:
    FUSEKI_URL      (default: http://localhost:3030)
    FUSEKI_DATASET  (default: wactorz)
    FUSEKI_USER     (default: admin)
    FUSEKI_PASSWORD (default: empty)
    PROXY_BASE      (default: http://localhost:8000)  base URL of the app/web-UI
                    that serves /api/fuseki/<dataset>/sparql. Set this to your
                    monitor port. Leave empty to skip the proxy column.
    BENCH_RUNS      (default: 40)   timed iterations per query
    BENCH_WARMUP    (default: 5)    untimed warmup iterations per query
    KPI_THRESHOLD_MS(default: 500)
    KPI_PERCENTILE  (default: 95)   percentile the pass/fail is judged on
"""

from __future__ import annotations

import base64
import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

FUSEKI_URL      = os.environ.get("FUSEKI_URL", "http://localhost:3030").rstrip("/")
DATASET         = os.environ.get("FUSEKI_DATASET", "wactorz")
USER            = os.environ.get("FUSEKI_USER", "admin")
PASSWORD        = os.environ.get("FUSEKI_PASSWORD", "")
PROXY_BASE      = os.environ.get("PROXY_BASE", "http://localhost:8000").rstrip("/")
RUNS            = int(os.environ.get("BENCH_RUNS", "40"))
WARMUP          = int(os.environ.get("BENCH_WARMUP", "5"))
THRESHOLD_MS    = float(os.environ.get("KPI_THRESHOLD_MS", "500"))
PERCENTILE      = float(os.environ.get("KPI_PERCENTILE", "95"))

DIRECT_ENDPOINT = f"{FUSEKI_URL}/{DATASET}/sparql"
PROXY_ENDPOINT  = f"{PROXY_BASE}/api/fuseki/{DATASET}/sparql" if PROXY_BASE else ""

_AUTH = (
    "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    if USER else None
)

PREFIXES = """\
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX syn:  <https://synapse.waldiez.io/ns#>
PREFIX af:   <https://waldiez.github.io/wactorz/ontology#>
"""


def run_query(endpoint: str, query: str) -> tuple[float, bool]:
    """POST a SPARQL query, read the full response, return (elapsed_ms, ok)."""
    data = query.encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, method="POST")
    req.add_header("Content-Type", "application/sparql-query")
    req.add_header("Accept", "application/sparql-results+json")
    if _AUTH:
        req.add_header("Authorization", _AUTH)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()  # drain body — transfer time counts toward latency
        ok = True
    except Exception:
        ok = False
    return (time.perf_counter() - t0) * 1000.0, ok


def probe(endpoint: str, timeout: float = 5.0) -> bool:
    """Quick reachability check so a wrong endpoint is skipped, not hung on."""
    req = urllib.request.Request(endpoint, data=b"ASK {}", method="POST")
    req.add_header("Content-Type", "application/sparql-query")
    req.add_header("Accept", "application/sparql-results+json")
    if _AUTH:
        req.add_header("Authorization", _AUTH)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True
    except Exception as exc:
        print(f"  ! endpoint unreachable, skipping: {endpoint}  ({exc})")
        return False


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    if not values:
        return float("nan")
    s = sorted(values)
    rank = min(max(math.ceil(p / 100.0 * len(s)), 1), len(s))
    return s[rank - 1]


def scalar_query(endpoint: str, query: str) -> str | None:
    """Run a SELECT returning one value; return it as a string (or None)."""
    data = query.encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, method="POST")
    req.add_header("Content-Type", "application/sparql-query")
    req.add_header("Accept", "application/sparql-results+json")
    if _AUTH:
        req.add_header("Authorization", _AUTH)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            doc = json.loads(resp.read())
        vars_ = doc["head"]["vars"]
        rows = doc["results"]["bindings"]
        if not rows:
            return None
        return rows[0][vars_[0]]["value"]
    except Exception as exc:
        print(f"  (setup query failed against {endpoint}: {exc})")
        return None


def benchmark(endpoint: str, name: str, query: str) -> dict:
    for _ in range(WARMUP):
        run_query(endpoint, query)
    times, errors = [], 0
    for _ in range(RUNS):
        ms, ok = run_query(endpoint, query)
        if ok:
            times.append(ms)
        else:
            errors += 1
    p_pass = percentile(times, PERCENTILE)
    return {
        "endpoint": endpoint,
        "query": name,
        "runs": len(times),
        "errors": errors,
        "p50_ms": round(percentile(times, 50), 1),
        "p95_ms": round(percentile(times, 95), 1),
        "p99_ms": round(percentile(times, 99), 1),
        "min_ms": round(min(times), 1) if times else float("nan"),
        "max_ms": round(max(times), 1) if times else float("nan"),
        "mean_ms": round(sum(times) / len(times), 1) if times else float("nan"),
        "pass": (errors == 0 and p_pass < THRESHOLD_MS),
    }


def main() -> None:
    print(f"Direct : {DIRECT_ENDPOINT}")
    print(f"Proxy  : {PROXY_ENDPOINT or '(disabled — set PROXY_BASE)'}")
    print(f"Runs   : {RUNS} timed (+{WARMUP} warmup) per query")
    print(f"KPI    : p{PERCENTILE:.0f} < {THRESHOLD_MS:.0f} ms")
    print("-" * 92)

    # ── Dataset size + a real agent IRI for the point-lookup query ───────────
    triples = scalar_query(
        DIRECT_ENDPOINT,
        "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }",
    )
    triples = int(triples) if triples is not None else -1
    agent_iri = scalar_query(
        DIRECT_ENDPOINT,
        PREFIXES + "SELECT ?a WHERE { GRAPH <urn:wactorz:agents> { ?a a syn:Agent } } LIMIT 1",
    )
    print(f"Dataset: {triples} triples (named graphs)")
    print(f"Sample agent: {agent_iri or '(none found)'}")
    print("-" * 92)

    # ── Representative query set (cheap → expensive) ─────────────────────────
    queries: list[tuple[str, str]] = []
    if agent_iri:
        queries.append((
            "point_lookup",
            PREFIXES + f"SELECT ?p ?o WHERE {{ GRAPH <urn:wactorz:agents> {{ <{agent_iri}> ?p ?o }} }}",
        ))
    queries.append((
        "catalog_scan",
        PREFIXES + """
SELECT ?agent ?label
       (GROUP_CONCAT(DISTINCT ?type;    SEPARATOR=", ") AS ?types)
       (GROUP_CONCAT(DISTINCT ?capKind; SEPARATOR=", ") AS ?capabilities)
       (SAMPLE(?st) AS ?state)
WHERE {
  GRAPH <urn:wactorz:agents> {
    ?agent a syn:Agent ; rdfs:label ?label .
    OPTIONAL { ?agent a ?type . }
    OPTIONAL { ?agent syn:state ?st . }
    OPTIONAL { ?cap syn:claimedBy ?agent ; a syn:Action ; a ?capKind .
               FILTER(?capKind != syn:Action) }
  }
} GROUP BY ?agent ?label ORDER BY ?label LIMIT 200""",
    ))
    queries.append((
        "aggregation_capability_kinds",
        PREFIXES + """
SELECT (COUNT(DISTINCT ?t) AS ?kinds) WHERE {
  GRAPH <urn:wactorz:agents> { ?n a syn:Action ; a ?t . FILTER(?t != syn:Action) }
}""",
    ))
    queries.append((
        "device_catalog",
        PREFIXES + """
SELECT ?entity ?label ?state ?area WHERE {
  GRAPH <urn:ha:devices> {
    ?entity rdfs:label ?label ;
            syn:state   ?state .
    OPTIONAL { ?entity syn:areaName ?area . }
    FILTER(!STRSTARTS(STR(?entity), "urn:ha:bridge:"))
  }
} ORDER BY ?area ?label LIMIT 500""",
    ))
    queries.append((
        "channel_topics",
        PREFIXES + """
SELECT DISTINCT ?channelId ?topic ?declaredSchema ?observedSchema ?observedExample ?publisherLabel
WHERE {
  GRAPH <urn:wactorz:channels> {
    ?channel rdf:type af:Channel ;
             af:channelTopic ?topic .
    BIND(STRAFTER(STR(?channel), "urn:wactorz:channel:") AS ?channelId)
    OPTIONAL { ?channel af:declaredSchema  ?declaredSchema  . }
    OPTIONAL { ?channel af:observedSchema  ?observedSchema  . }
    OPTIONAL { ?channel af:observedExample ?observedExample . }
    OPTIONAL {
      ?publisher af:publishesTo ?channel ;
                 rdfs:label      ?publisherLabel .
    }
  }
} ORDER BY ?topic""",
    ))

    endpoints = [("direct", DIRECT_ENDPOINT)]
    if PROXY_ENDPOINT:
        endpoints.append(("proxy", PROXY_ENDPOINT))
    # Drop unreachable endpoints up front so we don't hang through 40 timeouts.
    endpoints = [(via, ep) for via, ep in endpoints if probe(ep)]
    if not endpoints:
        print("No reachable SPARQL endpoint — aborting.")
        return

    results: list[dict] = []
    hdr = f"{'query':<30} {'via':<7} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8} {'err':>4}  KPI"
    print(hdr)
    print("-" * 92)
    for qname, q in queries:
        for via, ep in endpoints:
            r = benchmark(ep, qname, q)
            r["via"] = via
            r["triples"] = triples
            results.append(r)
            verdict = "PASS" if r["pass"] else "FAIL"
            print(
                f"{qname:<30} {via:<7} {r['p50_ms']:>8} {r['p95_ms']:>8} "
                f"{r['p99_ms']:>8} {r['max_ms']:>8} {r['errors']:>4}  {verdict}"
            )
    print("-" * 92)

    overall = all(r["pass"] for r in results)
    print(f"OVERALL KPI (p{PERCENTILE:.0f} < {THRESHOLD_MS:.0f} ms): "
          f"{'PASS' if overall else 'FAIL'}")

    # ── Reports ──────────────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stamp = datetime.now(timezone.utc).isoformat()
    csv_path = f"sparql_latency_{ts}.csv"
    json_path = f"sparql_latency_{ts}.json"

    cols = ["timestamp", "via", "query", "triples", "runs", "errors",
            "p50_ms", "p95_ms", "p99_ms", "min_ms", "max_ms", "mean_ms", "pass"]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in results:
            w.writerow({"timestamp": stamp, **{k: r.get(k) for k in cols if k != "timestamp"}})

    with open(json_path, "w") as f:
        json.dump({
            "timestamp": stamp,
            "config": {
                "direct_endpoint": DIRECT_ENDPOINT,
                "proxy_endpoint": PROXY_ENDPOINT,
                "runs": RUNS, "warmup": WARMUP,
                "threshold_ms": THRESHOLD_MS, "percentile": PERCENTILE,
            },
            "triples": triples,
            "kpi_pass": overall,
            "results": results,
        }, f, indent=2)

    print(f"\nReports written: {csv_path}  {json_path}")


if __name__ == "__main__":
    main()