"""sinergym_hsml_agent.py — natural-language Q&A over the Sinergym/HSML Fuseki store.

Place in catalogue_agents/ and register in catalog_agent.py _build_catalog()
(see REGISTER block at the bottom).

What it does
------------
Spawning `sinergym-hsml` starts one agent that answers plain-English questions
about the simulation by:
  1. discovering the live RDF schema from Fuseki (actual predicates, zones,
     agents) so it doesn't rely on hard-coded names,
  2. asking the LLM to turn the question into a SPARQL SELECT/ASK query,
  3. running that query against the read-only /sparql endpoint,
  4. asking the LLM to phrase the results as a natural-language answer.

The user never writes SPARQL. Example questions:
  "What heating/cooling setpoints did maddpg-zone-11 choose around step 20000?"
  "What was the outdoor temperature in episode 1?"
  "Which agent controls Perimeter_top_ZN_1 and what policy does it use?"
  "Was Core_mid occupied at step 18000?"

Requirements
------------
  * main must have an LLM provider configured (catalog passes it through; if
    absent, agent.llm is None and the agent reports it can't answer).
  * Fuseki reachable from the wactorz process (same URL/dataset the bridge writes
    to). Read-only: only SELECT/ASK/CONSTRUCT/DESCRIBE are ever executed.

Control (via @sinergym-hsml or main natural language)
-----------------------------------------------------
  Any plain question                       -> answered in natural language
  {"action": "refresh"}                    -> re-discover the schema
  {"action": "config", "fuseki_url": ...}  -> update connection settings
"""

AGENT_CODE = r'''
import asyncio
import json
import re

SGY_NS  = "https://waldiez.github.io/wactorz/sinergym#"
G_ZONES = "urn:sinergym:zones"
G_OBS   = "urn:sinergym:observations"
G_EPS   = "urn:sinergym:episodes"
G_HOURLY= "urn:sinergym:hourly"
G_ANOM  = "urn:sinergym:anomalies"

PREFIXES = (
    "PREFIX sgy: <" + SGY_NS + ">\n"
    "PREFIX prov: <http://www.w3.org/ns/prov#>\n"
    "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
    "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
)

DEFAULTS = {
    "fuseki_url":      "http://localhost:3030",
    "fuseki_dataset":  "sinergym",
    "fuseki_user":     "admin",
    "fuseki_password": "admin",
    "env_id":          "officeMedium-multiagent",
    "max_rows":        80,     # cap rows fed to the answering LLM
}

FORBIDDEN = ("insert", "delete", "drop", "clear", "load", "create", "update", "with ")


def _cfg(agent):
    cfg = dict(DEFAULTS)
    saved = agent.recall("config", None)
    if isinstance(saved, dict):
        cfg.update({k: v for k, v in saved.items() if v is not None})
    return cfg


# ── Fuseki access (read-only SPARQL endpoint) ───────────────────────────────────
def _sparql_query(cfg, query):
    import urllib.request, urllib.parse, base64
    url = cfg["fuseki_url"].rstrip("/") + "/" + cfg["fuseki_dataset"] + "/sparql"
    data = urllib.parse.urlencode({"query": query}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/sparql-results+json")
    user = cfg.get("fuseki_user")
    if user:
        tok = base64.b64encode(f"{user}:{cfg.get('fuseki_password','')}".encode()).decode()
        req.add_header("Authorization", "Basic " + tok)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _rows(res):
    if "boolean" in res:                       # ASK
        return [{"result": res["boolean"]}]
    out = []
    for b in res.get("results", {}).get("bindings", []):
        out.append({k: v.get("value") for k, v in b.items()})
    return out


def _pfx(iri):
    """Map a full predicate IRI to its correct CURIE so the LLM uses the right namespace."""
    PROV = "http://www.w3.org/ns/prov#"
    RDFS = "http://www.w3.org/2000/01/rdf-schema#"
    RDF  = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    if iri.startswith(SGY_NS):  return "sgy:"  + iri[len(SGY_NS):]
    if iri.startswith(PROV):    return "prov:" + iri[len(PROV):]
    if iri.startswith(RDFS):    return "rdfs:" + iri[len(RDFS):]
    if iri == RDF + "type":     return "rdf:type"
    if iri.startswith(RDF):     return "rdf:"  + iri[len(RDF):]
    return "<" + iri + ">"


async def _query(cfg, q):
    return _rows(await asyncio.to_thread(_sparql_query, cfg, q))


# ── Schema discovery: ground the LLM in what's actually stored ───────────────────
async def _discover(agent, cfg):
    ctx = {"zone_predicates": [], "obs_predicates": [], "zones": [],
           "agents": [], "step_max": None, "episodes": []}
    try:
        zp = await _query(cfg, PREFIXES +
            f"SELECT DISTINCT ?p WHERE {{ GRAPH <{G_ZONES}> {{ ?s a sgy:ZoneStep ; ?p ?o }} }}")
        ctx["zone_predicates"] = sorted({_pfx(r["p"]) for r in zp if r.get("p")})

        op = await _query(cfg, PREFIXES +
            f"SELECT DISTINCT ?p WHERE {{ GRAPH <{G_OBS}> {{ ?s ?p ?o }} }} LIMIT 60")
        ctx["obs_predicates"] = sorted({_pfx(r["p"]) for r in op if r.get("p")})

        zs = await _query(cfg, PREFIXES +
            f"SELECT DISTINCT ?z WHERE {{ GRAPH <{G_ZONES}> {{ ?s sgy:zone ?z }} }}")
        ctx["zones"] = sorted({r["z"] for r in zs if r.get("z")})

        ag = await _query(cfg, PREFIXES +
            f"SELECT ?label ?zone ?policy WHERE {{ GRAPH <{G_ZONES}> {{ "
            f"?a a prov:SoftwareAgent ; rdfs:label ?label ; sgy:controlsZone ?zone . "
            f"OPTIONAL {{ ?a sgy:policy ?policy }} }} }}")
        ctx["agents"] = ag

        mx = await _query(cfg, PREFIXES +
            f"SELECT (MAX(?st) AS ?m) (MIN(?st) AS ?n) WHERE {{ GRAPH <{G_ZONES}> {{ ?s sgy:step ?st }} }}")
        if mx and mx[0].get("m"):
            ctx["step_max"] = mx[0].get("m")
            ctx["step_min"] = mx[0].get("n")

        eps = await _query(cfg, PREFIXES +
            f"SELECT DISTINCT ?e WHERE {{ GRAPH <{G_ZONES}> {{ ?s sgy:episode ?e }} }} ORDER BY ?e")
        ctx["episodes"] = [r["e"] for r in eps if r.get("e")]

        # Anomaly alerts graph (written by sinergym-anomaly). Discover its
        # predicates + count so the LLM can answer "what anomalies fired".
        try:
            ap = await _query(cfg, PREFIXES +
                f"SELECT DISTINCT ?p WHERE {{ GRAPH <{G_ANOM}> {{ ?s a sgy:Alert ; ?p ?o }} }}")
            ctx["anomaly_predicates"] = sorted({_pfx(r["p"]) for r in ap if r.get("p")})
            ac = await _query(cfg, PREFIXES +
                f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{G_ANOM}> {{ ?s a sgy:Alert }} }}")
            ctx["anomaly_count"] = ac[0].get("n") if ac else "0"
        except Exception:
            ctx["anomaly_predicates"] = []
            ctx["anomaly_count"] = "0"
    except Exception as e:
        await agent.log(f"schema discovery failed: {e}", level="warning")
    return ctx


def _schema_text(ctx):
    agents_lines = "\n".join(
        f"    - {a.get('label')} controls zone {a.get('zone')}"
        + (f" (policy {a.get('policy')})" if a.get('policy') else "")
        for a in ctx.get("agents", [])
    ) or "    (none discovered yet)"
    return f"""\
DATASET / NAMED GRAPHS (always wrap patterns in the right GRAPH block):
  <{G_ZONES}>        per-zone, per-step records, type sgy:ZoneStep
  <{G_OBS}>          global per-step observation (whole-building / outdoor)
  <{G_EPS}>          episode metadata
  <{G_HOURLY}>       downsampled hourly averages of old episodes
  <{G_ANOM}>         detected anomaly alerts, type sgy:Alert ({ctx.get('anomaly_count', '0')} so far)

ZONE-STEP predicates actually present in <{G_ZONES}> (use these EXACTLY, with their
namespace prefix — note prov:/rdfs: are NOT sgy:):
  {', '.join(ctx.get('zone_predicates', [])) or '(unknown)'}
  Meaning: sgy:zone (string), sgy:step / sgy:episode (xsd:integer),
  sgy:airTemperature / sgy:airHumidity / sgy:peopleOccupant (zone sensors),
  sgy:htgSetpoint / sgy:clgSetpoint (observed setpoints),
  sgy:action_htg / sgy:action_clg (the heating/cooling setpoints the AGENT chose),
  sgy:reward / sgy:rewardGlobal,
  prov:wasAttributedTo -> the IRI of the agent that made the decision.

GLOBAL-OBS predicates present in <{G_OBS}> (use these EXACTLY):
  {', '.join(ctx.get('obs_predicates', [])) or '(unknown)'}
  (e.g. sgy:outdoorTemperature, sgy:outdoorHumidity, sgy:windSpeed, sgy:step, sgy:episode)

ANOMALY-ALERT predicates present in <{G_ANOM}> (type sgy:Alert):
  {', '.join(ctx.get('anomaly_predicates', [])) or '(none yet)'}
  Meaning: sgy:step / sgy:episode (xsd:integer), sgy:kindGuess (e.g. "hvac_fault",
  "sensor_drift"), sgy:severity (decimal), sgy:zoneIndex (int, optional),
  sgy:sources / sgy:message (strings), sgy:detector (string),
  prov:wasAttributedTo -> the detector agent IRI.

AGENTS — each declared in <{G_ZONES}> as:
  ?agent rdf:type prov:SoftwareAgent ; rdfs:label "<name>" ;
         sgy:controlsZone "<zone>" ; sgy:policy "<path>" .
  and every sgy:ZoneStep links to its agent via  prov:wasAttributedTo ?agent .
  Discovered agents:
{agents_lines}

ZONES available: {', '.join(ctx.get('zones', [])) or '(unknown)'}
STEP range: {ctx.get('step_min')} .. {ctx.get('step_max')}   EPISODES: {ctx.get('episodes')}

EXAMPLE QUERIES (follow these patterns for agent/provenance questions):

# What setpoints an agent chose, with the agent name:
SELECT ?step ?htg ?clg ?name WHERE {{
  GRAPH <{G_ZONES}> {{
    ?s sgy:zone "Core_mid" ; sgy:step ?step ;
       sgy:action_htg ?htg ; sgy:action_clg ?clg ;
       prov:wasAttributedTo ?agent .
    ?agent rdfs:label ?name .
  }}
}} ORDER BY ?step LIMIT 50

# All decisions made by a named agent in a step range:
SELECT ?zone ?step ?htg ?clg WHERE {{
  GRAPH <{G_ZONES}> {{
    ?agent rdfs:label "maddpg-zone-11" .
    ?s prov:wasAttributedTo ?agent ; sgy:zone ?zone ; sgy:step ?step ;
       sgy:action_htg ?htg ; sgy:action_clg ?clg .
    FILTER(?step >= 19000 && ?step <= 21000)
  }}
}} ORDER BY ?step LIMIT 100

# Which agent controls / decided for a zone — prefer the decisions path (always
# present) over the declaration path. Name via rdfs:label if present:
SELECT DISTINCT ?agent ?name WHERE {{
  GRAPH <{G_ZONES}> {{
    ?s sgy:zone "Perimeter_top_ZN_1" ; prov:wasAttributedTo ?agent .
    OPTIONAL {{ ?agent rdfs:label ?name }}
  }}
}}

# What anomalies were detected (optionally filter by kind or episode):
SELECT ?step ?episode ?kind ?severity ?zone WHERE {{
  GRAPH <{G_ANOM}> {{
    ?a a sgy:Alert ; sgy:step ?step ; sgy:episode ?episode ;
       sgy:kindGuess ?kind ; sgy:severity ?severity .
    OPTIONAL {{ ?a sgy:zoneIndex ?zone }}
  }}
}} ORDER BY ?episode ?step LIMIT 200

# Count detected anomalies by kind:
SELECT ?kind (COUNT(*) AS ?n) WHERE {{
  GRAPH <{G_ANOM}> {{ ?a a sgy:Alert ; sgy:kindGuess ?kind }}
}} GROUP BY ?kind

RULES:
  - Data is SAMPLED every 3rd step, so exact step numbers may not exist; prefer a
    range filter (FILTER(?step >= A && ?step <= B)) or ORDER BY + LIMIT over exact match.
  - sgy:step and sgy:episode are xsd:integer -> write them bare (sgy:step 18000), not quoted.
  - To find the agent for a zone, go through a ZoneStep: ?s sgy:zone "Z" ; prov:wasAttributedTo ?agent.
    This ALWAYS works. The 'a prov:SoftwareAgent ; sgy:controlsZone' declaration may be ABSENT,
    so do NOT rely on it; treat rdfs:label as OPTIONAL.
  - Outdoor / whole-building values are in <{G_OBS}>, NOT in the per-zone graph.
  - Detected anomalies / alerts / faults are in <{G_ANOM}> (type sgy:Alert), NOT the zones graph.
  - Use prov: and rdfs: prefixes for those predicates — never sgy:wasAttributedTo / sgy:label.
"""


SPARQL_SYS = """You translate a user's question about a building-energy simulation into ONE SPARQL query.
Output ONLY the query — no prose, no markdown fences, no PREFIX lines (prefixes are added automatically).
Use ONLY SELECT or ASK. Never use INSERT/DELETE/DROP/CLEAR/LOAD/CREATE.
Always wrap triple patterns in the correct GRAPH <...> block from the schema.
Always end a SELECT with a sensible LIMIT (<= 200).
Use the predicate names from the schema verbatim. Follow the RULES in the schema."""

ANSWER_SYS = """You answer the user's question about a building-energy simulation using the SPARQL results provided.
Be concise and conversational. State the actual values with sensible units (temperatures in °C,
setpoints in °C, occupancy as a count). If an agent appears only as an IRI like
'...sinergym#agent_maddpg-zone-11', report its name as the part after 'agent_' (e.g. maddpg-zone-11).
If the results are empty, say no matching data was found and suggest what to ask instead
(e.g. a different step range, zone, or episode). Do not invent numbers."""


def _strip_query(text):
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    # drop any PREFIX lines the model added; we prepend our own
    t = "\n".join(ln for ln in t.splitlines() if not ln.strip().lower().startswith("prefix"))
    return t.strip()


def _is_safe(q):
    low = q.lower()
    if not (low.lstrip().startswith("select") or low.lstrip().startswith("ask")):
        return False
    return not any(bad in low for bad in FORBIDDEN)


async def _answer_question(agent, cfg, question):
    import time as _t
    if agent.llm is None:
        return {"ok": False, "answer": "No LLM provider is configured, so I can't translate "
                                       "questions to queries. Configure one on main and respawn."}

    await agent.log(f"[hsml] Q: {question!r}")
    ctx = agent.state.get("schema")
    if not ctx:
        _t0 = _t.time()
        ctx = await _discover(agent, cfg)
        agent.state["schema"] = ctx
        await agent.log(f"[hsml] schema discovered in {_t.time()-_t0:.1f}s "
                  f"(zones={len(ctx.get('zones', []))}, "
                  f"zone_preds={len(ctx.get('zone_predicates', []))}, "
                  f"agents={len(ctx.get('agents', []))})")
    schema = _schema_text(ctx)

    # 1) question -> SPARQL
    _t0 = _t.time()
    gen = await agent.llm.chat(
        f"Schema:\n{schema}\n\nQuestion: {question}\n\nSPARQL query:",
        system=SPARQL_SYS,
    )
    query_body = _strip_query(gen)
    await agent.log(f"[hsml] SPARQL generated in {_t.time()-_t0:.1f}s:\n{query_body}")
    if not _is_safe(query_body):
        await agent.log("[hsml] generated query rejected as unsafe/invalid", level="warning")
        return {"ok": False, "answer": "I could only form an unsafe or invalid query for that. "
                                       "Try rephrasing — e.g. ask about a specific zone, agent, "
                                       "step range, or episode.",
                "sparql": query_body}
    full_query = PREFIXES + query_body

    # 2) execute (one retry, feeding the error back to the model)
    _t0 = _t.time()
    try:
        rows = await _query(cfg, full_query)
        await agent.log(f"[hsml] Fuseki returned {len(rows)} row(s) in {_t.time()-_t0:.1f}s")
    except Exception as e:
        await agent.log(f"[hsml] Fuseki query failed in {_t.time()-_t0:.1f}s: {e}; retrying once",
                  level="warning")
        try:
            fix = await agent.llm.chat(
                f"Schema:\n{schema}\n\nQuestion: {question}\n\nThe previous query failed with:\n{e}\n"
                f"Previous query:\n{query_body}\n\nReturn a corrected SPARQL query:",
                system=SPARQL_SYS,
            )
            query_body = _strip_query(fix)
            await agent.log(f"[hsml] corrected SPARQL:\n{query_body}")
            if not _is_safe(query_body):
                raise ValueError("unsafe corrected query")
            full_query = PREFIXES + query_body
            rows = await _query(cfg, full_query)
            await agent.log(f"[hsml] Fuseki (retry) returned {len(rows)} row(s)")
        except Exception as e2:
            await agent.log(f"[hsml] Fuseki query failed again: {e2}", level="error")
            return {"ok": False, "answer": f"The query against Fuseki failed: {e2}",
                    "sparql": full_query}

    max_rows = int(cfg.get("max_rows", 80))
    shown = rows[:max_rows]
    note = "" if len(rows) <= max_rows else f"\n(showing first {max_rows} of {len(rows)} rows)"

    # 3) results -> natural language
    _t0 = _t.time()
    answer = await agent.llm.chat(
        f"Question: {question}\n\nSPARQL results (JSON rows):\n"
        f"{json.dumps(shown, ensure_ascii=False)}{note}\n\nAnswer:",
        system=ANSWER_SYS,
    )
    await agent.log(f"[hsml] answer composed in {_t.time()-_t0:.1f}s")
    return {"ok": True, "answer": answer.strip(), "sparql": full_query, "rows": len(rows)}


async def setup(agent):
    agent.state["schema"] = None
    cfg = _cfg(agent)
    # Best-effort schema pre-warm so the first question doesn't pay discovery cost
    # on top of the LLM calls. If Fuseki isn't reachable yet, we discover lazily.
    try:
        import time as _t
        _t0 = _t.time()
        ctx = await _discover(agent, cfg)
        if ctx.get("zone_predicates"):
            agent.state["schema"] = ctx
            await agent.log(f"sinergym-hsml ready; schema pre-warmed in {_t.time()-_t0:.1f}s "
                      f"(zones={len(ctx.get('zones', []))}, agents={len(ctx.get('agents', []))})")
        else:
            await agent.log("sinergym-hsml ready; schema empty/unreachable now, will retry on first question",
                      level="warning")
    except Exception as e:
        await agent.log(f"sinergym-hsml ready; schema pre-warm failed ({e}), will retry lazily",
                  level="warning")


async def process(agent):
    pass   # request-driven


async def handle_task(agent, payload):
    # The "@agent {json}" path delivers {"text": "<raw json>"} WITHOUT parsing it,
    # so an {"action":"config"|"refresh"} command arrives as a text-only wrapper and
    # would otherwise fall through to the question path. Normalize it here first.
    if isinstance(payload, dict) and "action" not in payload and "text" in payload:
        raw = (payload.get("text") or "").strip()
        if raw.startswith("{") and raw.endswith("}"):
            try:
                payload = json.loads(raw)
            except Exception:
                pass

    # Accept a structured dict, a raw string, or the {"text": "..."} wrapper that
    # the direct @agent input path delivers.
    question = None
    action = None
    if isinstance(payload, dict):
        action = (payload.get("action") or "").lower().strip()
        question = payload.get("question") or payload.get("text")
    elif isinstance(payload, str):
        question = payload

    cfg = _cfg(agent)
    for k in ("fuseki_url", "fuseki_dataset", "fuseki_user", "fuseki_password", "env_id", "max_rows"):
        if isinstance(payload, dict) and payload.get(k) is not None:
            cfg[k] = payload[k]

    if action == "config":
        agent.persist("config", cfg)
        agent.state["schema"] = None
        return "Config updated; the schema will re-discover on the next question."

    if action == "refresh":
        agent.state["schema"] = await _discover(agent, cfg)
        sc = agent.state["schema"]
        await agent.log(f"[hsml] schema refreshed: zones={sc.get('zones')} "
                  f"zone_predicates={sc.get('zone_predicates')}")
        return (f"Schema refreshed: {len(sc.get('zones', []))} zones, "
                f"{len(sc.get('agents', []))} agents, "
                f"{len(sc.get('zone_predicates', []))} zone predicates discovered.")

    q = (question or "").strip()
    # ignore the framework's wrapper bookkeeping if it leaked in as the text
    if q.startswith("{") and q.endswith("}"):
        try:
            inner = json.loads(q)
            q = (inner.get("question") or inner.get("text") or "").strip()
        except Exception:
            pass
    if not q:
        return ("Ask me a question about the building, e.g. "
                "\"what setpoints did maddpg-zone-11 pick near step 20000?\" "
                "or \"what was the outdoor temperature in episode 1?\"")

    await agent.log(f"[hsml] received question, dispatching to LLM+Fuseki pipeline")
    result = await _answer_question(agent, cfg, q)
    await agent.log(f"[hsml] ok={result.get('ok')} rows={result.get('rows')}\n"
              f"[hsml] sparql:\n{result.get('sparql', '(none)')}")
    answer = result.get("answer", "(no answer)")
    sparql = result.get("sparql")
    if sparql:
        answer = f"{answer}\n\n— SPARQL used —\n{sparql}"
    return answer
'''


# ──────────────────────────────────────────────────────────────────────────────
# REGISTER — add this block inside _build_catalog() in catalog_agent.py
# ──────────────────────────────────────────────────────────────────────────────
#   code = _load_recipe("sinergym_hsml_agent.py")
#   if code:
#       catalog["sinergym-hsml"] = {
#           "name":         "sinergym-hsml",
#           "type":         "dynamic",
#           "description":  "Answers natural-language questions about the Sinergym "
#                           "simulation (agent decisions, setpoints, zone temperatures, "
#                           "occupancy, outdoor conditions) by generating SPARQL over the "
#                           "Fuseki/HSML store and replying in plain language.",
#           "capabilities": ["sinergym", "fuseki", "sparql", "question_answering",
#                            "hsml", "provenance", "building_analytics"],
#           "install":      [],   # uses stdlib urllib + main's LLM provider
#           "input_schema": {
#               "question":        "str — a plain-English question about the building",
#               "action":          "str — optional: refresh | config",
#               "fuseki_url":      "str — optional override (default host.docker.internal:3030)",
#               "fuseki_dataset":  "str — optional (default 'sinergym')",
#               "fuseki_user":     "str — optional",
#               "fuseki_password": "str — optional",
#           },
#           "output_schema": {
#               "ok":     "bool",
#               "answer": "str — natural-language answer",
#               "sparql": "str — the query that was run (for transparency)",
#               "rows":   "int — number of result rows",
#           },
#           "poll_interval": 3600,
#           "code":          code,
#       }
#       logger.info("[catalog] Loaded sinergym-hsml recipe")
