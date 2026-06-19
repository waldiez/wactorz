/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Knowledge Graph ("Graph" tab) — a read-mostly Apache Jena Fuseki SPARQL
 * console. Preset queries on the left, an editor + results table on the right.
 *
 * Queries always go through the backend `/api/fuseki/<dataset>/sparql` proxy,
 * which connects to the server-configured Fuseki endpoint (the `FUSEKI_URL`
 * env var). The browser never talks to Fuseki directly and holds no Fuseki
 * credentials — only the dataset name (used in the proxy path) is client-side.
 */

interface SparqlJson {
    head: { vars: string[] };
    results?: { bindings: Record<string, { value: string; type: string }>[] };
    boolean?: boolean;
}

const KEYS = {
    /** Server's Fuseki endpoint, seeded from /api/config — shown read-only. */
    url: "wactorz-fuseki-url",
    /** Dataset name used in the proxy path; the only client-side setting. */
    dataset: "wactorz-fuseki-dataset",
} as const;

const SPARQL_PREFIXES = `PREFIX syn:    <https://synapse.waldiez.io/ns#>
PREFIX sosa:   <http://www.w3.org/ns/sosa/>
PREFIX ssn:    <http://www.w3.org/ns/ssn/>
PREFIX saref:  <https://saref.etsi.org/core/>
PREFIX rdfs:   <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rdf:    <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX xsd:    <http://www.w3.org/2001/XMLSchema#>
PREFIX prov:   <http://www.w3.org/ns/prov#>
`;

const PRESETS: { label: string; icon: string; sparql: string }[] = [
    {
        label: "All graphs",
        icon: "◌",
        sparql: `SELECT ?g (COUNT(*) AS ?triples) WHERE {
  GRAPH ?g { ?s ?p ?o }
} GROUP BY ?g ORDER BY ?g`,
    },
    {
        label: "Top predicates",
        icon: "≡",
        sparql: `SELECT ?p (COUNT(*) AS ?count) WHERE {
  GRAPH ?g { ?s ?p ?o }
} GROUP BY ?p ORDER BY DESC(?count) LIMIT 50`,
    },
    {
        label: "Sample triples",
        icon: "⋯",
        sparql: `SELECT ?g ?s ?p ?o WHERE {
  GRAPH ?g { ?s ?p ?o }
} LIMIT 50`,
    },
    {
        label: "Current states",
        icon: "◉",
        sparql: `SELECT ?g ?entity ?state ?unit WHERE {
  VALUES ?g { <urn:ha:current> <urn:wactorz:current> }
  GRAPH ?g {
    ?entity ?statePred ?state .
    FILTER(?statePred IN (syn:state, saref:hasState))
    OPTIONAL {
      ?entity ?unitPred ?unit .
      FILTER(?unitPred IN (syn:unit, saref:hasUnitOfMeasure))
    }
  }
} ORDER BY ?entity LIMIT 200`,
    },
    {
        label: "Recent observations",
        icon: "⏱",
        sparql: `SELECT ?g ?obs ?entity ?result ?ts WHERE {
  VALUES ?g { <urn:ha:history> <urn:wactorz:history> }
  GRAPH ?g {
    ?obs a sosa:Observation .
    OPTIONAL { ?obs sosa:madeBySensor ?entity . }
    OPTIONAL { ?obs sosa:hasSimpleResult ?result . }
    OPTIONAL { ?obs sosa:resultTime ?ts . }
  }
} ORDER BY DESC(?ts) LIMIT 100`,
    },
    {
        label: "Device catalog",
        icon: "⊡",
        sparql: `SELECT ?entity ?label ?state ?area WHERE {
  GRAPH <urn:ha:devices> {
    ?entity rdfs:label ?label ;
            syn:state   ?state .
    OPTIONAL { ?entity syn:areaName ?area . }
    FILTER(!STRSTARTS(STR(?entity), "urn:ha:bridge:"))
  }
} ORDER BY ?area ?label LIMIT 500`,
    },
    {
        label: "Agents",
        icon: "⚙",
        sparql: `SELECT ?entity ?label ?state ?protected ?actorId WHERE {
  GRAPH <urn:wactorz:agents> {
    ?entity rdfs:label ?label .
    OPTIONAL { ?entity syn:state ?state . }
    OPTIONAL { ?entity syn:protected ?protected . }
    OPTIONAL { ?entity syn:actorId ?actorId . }
  }
} ORDER BY ?label LIMIT 200`,
    },
    {
        label: "Sensors with units",
        icon: "📡",
        sparql: `SELECT ?g ?entity ?state ?unit WHERE {
  VALUES ?g { <urn:ha:current> <urn:wactorz:current> }
  GRAPH ?g {
    ?entity a sosa:Sensor ;
            ?statePred ?state .
    FILTER(?statePred IN (syn:state, saref:hasState))
    OPTIONAL {
      ?entity ?unitPred ?unit .
      FILTER(?unitPred IN (syn:unit, saref:hasUnitOfMeasure))
    }
  }
} ORDER BY ?entity LIMIT 200`,
    },
    {
        label: "Graph sizes",
        icon: "∑",
        sparql: `SELECT ?g (COUNT(*) AS ?triples) WHERE {
  VALUES ?g { <urn:ha:current> <urn:ha:history> <urn:ha:devices> <urn:wactorz:agents> }
  GRAPH ?g { ?s ?p ?o }
} GROUP BY ?g ORDER BY ?g`,
    },
];

function readDataset(): string {
    return localStorage.getItem(KEYS.dataset) || "wactorz";
}

/** The server's configured Fuseki endpoint (seeded from /api/config), if known. */
function serverUrl(): string | null {
    return localStorage.getItem(KEYS.url) || null;
}

function ingressPath(): string {
    return (window as unknown as { __WACTORZ_INGRESS_PATH?: string }).__WACTORZ_INGRESS_PATH ?? "";
}

/** Prepend only the standard prefixes the query has not already declared. */
function withPrefixes(q: string): string {
    const declared = new Set([...q.matchAll(/^\s*PREFIX\s+(\w*:)/gim)].map(m => m[1]));
    const needed = SPARQL_PREFIXES.split("\n")
        .filter(line => {
            const m = line.match(/^PREFIX\s+(\w*:)/);
            return m && !declared.has(m[1]);
        })
        .join("\n");
    return needed ? needed + "\n" + q : q;
}

function buildHeaderBar(dataset: string, onDataset: (ds: string) => void): HTMLElement {
    const hdr = document.createElement("div");
    hdr.style.cssText = "display:flex;align-items:center;gap:10px;flex-shrink:0;flex-wrap:wrap;";
    const url = serverUrl();
    const endpoint = url
        ? `<a href="${url}" target="_blank" rel="noopener" class="af-fuseki-endpoint">${url} ↗</a>`
        : `<span class="af-fuseki-endpoint af-fuseki-muted" title="Set FUSEKI_URL in the server environment">server endpoint not set</span>`;
    hdr.innerHTML = `
      <span style="font-size:20px;line-height:1;">⬡</span>
      <span style="font-weight:700;font-size:14px;color:rgba(255,255,255,0.92);">Knowledge Graph</span>
      <label class="af-fuseki-ds-field" title="Fuseki dataset (used in the query path)">
        dataset
        <input id="fsk-ds" class="af-fuseki-ds-input" value="${dataset}" spellcheck="false" autocomplete="off">
      </label>
      <div style="flex:1;"></div>
      ${endpoint}
    `;
    const input = hdr.querySelector<HTMLInputElement>("#fsk-ds");
    input?.addEventListener("change", () => onDataset(input.value.trim() || "wactorz"));
    return hdr;
}

function buildHint(): HTMLElement {
    const hint = document.createElement("div");
    hint.style.cssText =
        "font-size:12px;line-height:1.45;color:rgba(255,255,255,0.6);padding:10px 12px;border:1px solid rgba(255,255,255,0.08);border-radius:10px;background:rgba(255,255,255,0.03);";
    hint.textContent =
        "Read-only view of data already stored in Fuseki. If the dataset is empty, presets return 0 rows even when the endpoint is reachable.";
    return hint;
}

function buildSidebar(onPick: (sparql: string) => void): HTMLElement {
    const sidebar = document.createElement("div");
    sidebar.className = "af-fuseki-sidebar";
    PRESETS.forEach(p => {
        const btn = document.createElement("button");
        btn.className = "af-fuseki-preset-btn";
        btn.innerHTML = `<span class="af-fuseki-preset-icon">${p.icon}</span><span>${p.label}</span>`;
        btn.addEventListener("click", () => onPick(p.sparql));
        sidebar.appendChild(btn);
    });
    return sidebar;
}

interface EditorPanel {
    panel: HTMLElement;
    editor: HTMLTextAreaElement;
    runBtn: HTMLButtonElement;
    status: HTMLElement;
    results: HTMLElement;
}

function buildEditorPanel(): EditorPanel {
    const panel = document.createElement("div");
    panel.style.cssText = "flex:1;display:flex;flex-direction:column;gap:10px;min-width:0;overflow:hidden;";

    const editorRow = document.createElement("div");
    editorRow.style.cssText = "display:flex;gap:8px;align-items:flex-start;flex-shrink:0;";

    const editor = document.createElement("textarea");
    Object.assign(editor, {
        className: "af-fuseki-editor",
        spellcheck: false,
        placeholder: "SELECT * WHERE { ?s ?p ?o } LIMIT 10",
        rows: 6,
        value: PRESETS[0]?.sparql ?? "",
    });

    const runBtn = document.createElement("button");
    runBtn.className = "af-mini-btn af-fuseki-run-btn";
    runBtn.innerHTML = "▶ Run";
    editorRow.append(editor, runBtn);

    const status = document.createElement("div");
    status.className = "af-fuseki-status";
    status.textContent = "Ready.";

    const results = document.createElement("div");
    results.className = "af-fuseki-results";

    panel.append(editorRow, status, results);
    return { panel, editor, runBtn, status, results };
}

function renderTable(results: HTMLElement, vars: string[], bindings: SparqlJson["results"]): void {
    const rows = bindings?.bindings ?? [];
    const table = document.createElement("table");
    table.className = "af-fuseki-table";

    const hrow = table.createTHead().insertRow();
    vars.forEach(v => {
        const th = document.createElement("th");
        th.textContent = v;
        hrow.appendChild(th);
    });

    const tbody = table.createTBody();
    rows.forEach(row => {
        const tr = tbody.insertRow();
        vars.forEach(v => {
            const td = tr.insertCell();
            const cell = row[v];
            if (!cell) {
                td.textContent = "";
                return;
            }
            const val = cell.value;
            const display = val.length > 60 ? `<span title="${val}">${val.slice(0, 58)}…</span>` : val;
            td.innerHTML = cell.type === "uri" ? `<span class="af-fuseki-uri">${display}</span>` : display;
        });
    });
    results.appendChild(table);
}

/** Render a successful SELECT/ASK JSON response into the results panel. */
function renderSelect(parts: EditorPanel, json: SparqlJson, trimmed: string): void {
    const { status, results } = parts;
    if (typeof json.boolean === "boolean") {
        status.textContent = `Result: ${json.boolean}`;
        status.style.color = json.boolean ? "#34d399" : "#fbbf24";
        return;
    }
    const vars = json.head?.vars ?? [];
    const count = json.results?.bindings.length ?? 0;
    status.textContent = `${count} row${count !== 1 ? "s" : ""}`;
    status.style.color = "rgba(255,255,255,0.45)";
    if (count === 0) {
        const looksHa = /urn:ha:(current|history|devices)/i.test(trimmed);
        results.innerHTML = looksHa
            ? `<div class="af-fuseki-empty">No results.<br><span style="opacity:0.65">Fuseki is reachable, but the expected HA graphs appear empty.</span></div>`
            : `<div class="af-fuseki-empty">No results.</div>`;
        return;
    }
    renderTable(results, vars, json.results);
}

/** Friendly message when the server itself has no Fuseki endpoint configured. */
function renderNotConfigured(results: HTMLElement): void {
    results.innerHTML = `<div class="af-fuseki-empty">Fuseki isn't configured on the server.<br>
        <span style="opacity:0.7">Set <code>FUSEKI_URL</code> (and <code>FUSEKI_USER</code>/<code>FUSEKI_PASSWORD</code>
        if it requires auth) in the server environment, then restart.</span></div>`;
}

/** POST a query (or update) to the dataset's proxy endpoint. */
function postSparql(
    isUpdate: boolean,
    sparqlUrl: string,
    updateUrl: string,
    full: string,
): Promise<Response> {
    if (isUpdate) {
        return fetch(updateUrl, {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: `update=${encodeURIComponent(full)}`,
        });
    }
    return fetch(sparqlUrl, {
        method: "POST",
        headers: {
            Accept: "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body: `query=${encodeURIComponent(full)}`,
    });
}

/** Turn a proxy response into status text + a rendered result. */
async function handleResponse(
    parts: EditorPanel,
    resp: Response,
    isUpdate: boolean,
    trimmed: string,
): Promise<void> {
    const { status, results } = parts;
    if (!resp.ok) {
        const text = await resp.text();
        status.textContent = `Error ${resp.status}`;
        status.style.color = "#f87171";
        if (resp.status === 503 && /not configured/i.test(text)) {
            renderNotConfigured(results);
        } else {
            results.innerHTML = `<pre class="af-fuseki-error">${text.slice(0, 600)}</pre>`;
        }
        return;
    }
    if (isUpdate) {
        status.textContent = "Update OK";
        status.style.color = "#34d399";
        return;
    }
    renderSelect(parts, (await resp.json()) as SparqlJson, trimmed);
}

/** A query runner bound to one dataset's SPARQL/update proxy endpoints. */
function makeRunner(parts: EditorPanel, ds: string): (q: string) => Promise<void> {
    const dsPath = encodeURIComponent(ds);
    const sparqlUrl = `${ingressPath()}/api/fuseki/${dsPath}/sparql`;
    const updateUrl = `${ingressPath()}/api/fuseki/${dsPath}/update`;
    const { status, results } = parts;

    return async (q: string): Promise<void> => {
        const trimmed = q.trim();
        if (!trimmed) {
            return;
        }
        status.textContent = "Running…";
        status.style.color = "rgba(255,255,255,0.4)";
        results.innerHTML = "";
        const isUpdate = /^\s*(INSERT|DELETE|DROP|CREATE|LOAD|CLEAR|ADD|MOVE|COPY)/i.test(trimmed);
        const full = withPrefixes(trimmed);
        try {
            await handleResponse(
                parts,
                await postSparql(isUpdate, sparqlUrl, updateUrl, full),
                isUpdate,
                trimmed,
            );
        } catch (err) {
            status.textContent = "Network error";
            status.style.color = "#f87171";
            results.innerHTML = `<pre class="af-fuseki-error">${String(err)}</pre>`;
        }
    };
}

/** The SPARQL console: header + dataset selector + preset sidebar + editor + results. */
export function buildFusekiView(reRender: () => void): HTMLElement {
    const dataset = readDataset();
    const el = document.createElement("div");
    el.className = "af-overview";

    const wrapper = document.createElement("div");
    wrapper.style.cssText = "display:flex;flex-direction:column;gap:14px;height:100%;min-height:0;";

    wrapper.appendChild(
        buildHeaderBar(dataset, ds => {
            localStorage.setItem(KEYS.dataset, ds);
            reRender();
        }),
    );
    wrapper.appendChild(buildHint());

    const mainRow = document.createElement("div");
    mainRow.style.cssText = "display:flex;gap:14px;flex:1;min-height:0;overflow:hidden;";

    const parts = buildEditorPanel();
    const run = makeRunner(parts, dataset);
    const sidebar = buildSidebar(sparql => {
        parts.editor.value = sparql;
        void run(sparql);
    });
    mainRow.append(sidebar, parts.panel);
    wrapper.appendChild(mainRow);
    el.appendChild(wrapper);

    parts.runBtn.addEventListener("click", () => void run(parts.editor.value));
    parts.editor.addEventListener("keydown", e => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            void run(parts.editor.value);
        }
    });
    void run(PRESETS[0]?.sparql ?? "");
    return el;
}
