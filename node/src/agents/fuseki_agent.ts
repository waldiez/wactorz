/**
 * FusekiAgent — SPARQL knowledge-graph interface (Node.js port).
 * NATO: FERN / Foxtrot. agentType: "librarian"
 */

import axios from "axios";
import { Actor, MqttPublisher } from "../core/actor";
import { Message, MessageType } from "../core/types";

const BASE_URL = (process.env["FUSEKI_URL"] ?? "http://fuseki:3030").replace(/\/$/, "");
const DATASET  = process.env["FUSEKI_DATASET"] ?? "/ds";
const DS       = DATASET.startsWith("/") ? DATASET : `/${DATASET}`;

const SPARQL_ENDPOINT = `${BASE_URL}${DS}/sparql`;
const ADMIN_ENDPOINT  = `${BASE_URL}/$/datasets`;

const PREFIXES = `PREFIX rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX foaf:  <http://xmlns.com/foaf/0.1/>
PREFIX schema:<https://schema.org/>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>`;

const HELP = `**FERN — FusekiAgent** 🌿
_SPARQL knowledge-graph interface_

| Command | Description |
|---------|-------------|
| \`query <sparql>\` | SELECT / CONSTRUCT query |
| \`ask <sparql>\` | ASK query → true/false |
| \`prefixes\` | Common RDF prefix bindings |
| \`datasets\` | List Fuseki datasets |
| \`help\` | This message |

**Example:** \`query SELECT * WHERE { ?s ?p ?o } LIMIT 5\``;

const NS_SHORT: [string, string][] = [
  ["rdf:", "http://www.w3.org/1999/02/22-rdf-syntax-ns#"],
  ["rdfs:", "http://www.w3.org/2000/01/rdf-schema#"],
  ["owl:", "http://www.w3.org/2002/07/owl#"],
  ["xsd:", "http://www.w3.org/2001/XMLSchema#"],
];

export class FusekiAgent extends Actor {
  constructor(publish: MqttPublisher, actorId?: string) {
    super("fern-agent", publish, actorId);
  }

  protected override async onStart(): Promise<void> {
    this.publishSpawn("librarian");
  }

  override async handleMessage(msg: Message): Promise<void> {
    if (msg.type !== MessageType.Task && msg.type !== MessageType.Text) return;
    let text = Actor.extractText(msg.payload).trim();
    text = Actor.stripPrefix(text, "@fern-agent", "@fern_agent", "@fuseki-agent", "@fuseki_agent");
    await this._dispatch(text.trim());
    this.metrics.tasksCompleted++;
  }

  private async _dispatch(text: string): Promise<void> {
    const lower = text.toLowerCase().trim();
    if (!lower || lower === "help")     { this.replyChat(HELP); return; }
    if (lower === "prefixes")           { this.replyChat(`**Common RDF Prefixes:**\n\n\`\`\`sparql\n${PREFIXES}\n\`\`\``); return; }
    if (lower === "datasets")           { await this._cmdDatasets(); return; }
    if (lower.startsWith("ask "))       { await this._cmdAsk(text.slice(4).trim()); return; }
    if (lower.startsWith("query "))     { await this._cmdQuery(text.slice(6).trim()); return; }
    // Raw SPARQL
    const up = text.trimStart().toUpperCase();
    if (["SELECT","CONSTRUCT","DESCRIBE","PREFIX","ASK"].some(k => up.startsWith(k))) {
      await this._cmdQuery(text); return;
    }
    this.replyChat(`Unknown command. Type \`help\`.`);
  }

  private async _cmdQuery(sparql: string): Promise<void> {
    if (!sparql) { this.replyChat("Usage: `query <sparql>`"); return; }
    try {
      const resp = await axios.get(SPARQL_ENDPOINT, {
        params: { query: sparql, format: "json" },
        headers: { Accept: "application/sparql-results+json,application/json" },
        timeout: 20_000,
      });
      this.replyChat(this._formatResults(resp.data as Record<string, unknown>));
    } catch (e: unknown) {
      const msg = axios.isAxiosError(e) ? `HTTP ${e.response?.status}: ${String(e.response?.data ?? e.message).slice(0, 300)}` : String(e);
      this.replyChat(`✗ Query failed: ${msg}`);
    }
  }

  private async _cmdAsk(sparql: string): Promise<void> {
    const q = sparql.trimStart().toUpperCase().startsWith("ASK") ? sparql : `ASK { ${sparql} }`;
    try {
      const resp = await axios.get(SPARQL_ENDPOINT, {
        params: { query: q, format: "json" },
        headers: { Accept: "application/sparql-results+json,application/json" },
        timeout: 20_000,
      });
      const bool = (resp.data as Record<string, unknown>)["boolean"] as boolean;
      this.replyChat(`**ASK Result:** ${bool ? "✓" : "✗"} \`${bool ? "true" : "false"}\`\n\nQuery: \`${q}\``);
    } catch (e: unknown) {
      this.replyChat(`✗ ASK failed: ${e}`);
    }
  }

  private async _cmdDatasets(): Promise<void> {
    try {
      const resp = await axios.get(ADMIN_ENDPOINT, { timeout: 10_000 });
      const datasets = ((resp.data as Record<string, unknown>)["datasets"] as unknown[]) ?? [];
      if (!datasets.length) { this.replyChat("No datasets found."); return; }
      const lines = [`**Fuseki Datasets (${datasets.length}):**\n`];
      for (const ds of datasets) {
        const d = ds as Record<string, unknown>;
        lines.push(`- \`${d["ds.name"]}\` — ${d["ds.state"]}`);
      }
      this.replyChat(lines.join("\n"));
    } catch (e: unknown) {
      this.replyChat(`✗ Cannot reach Fuseki at \`${BASE_URL}\`: ${e}`);
    }
  }

  private _formatResults(data: Record<string, unknown>): string {
    const head = data["head"] as Record<string, unknown> | undefined;
    const vars = (head?.["vars"] as string[]) ?? [];
    const bindings = ((data["results"] as Record<string, unknown>)?.["bindings"] as unknown[]) ?? [];
    if (!bindings.length) return `Query returned 0 rows. Columns: ${vars.join(", ")}`;
    const lines = [`**Results** (${bindings.length} rows, columns: ${vars.join(", ")}):\n`];
    (bindings as Record<string, unknown>[]).slice(0, 20).forEach((row, i) => {
      const parts = vars.map((v) => {
        const cell = (row[v] as Record<string, string>) ?? {};
        let val = cell["value"] ?? "null";
        if (cell["type"] === "uri") {
          for (const [pfx, ns] of NS_SHORT) {
            if (val.startsWith(ns)) { val = pfx + val.slice(ns.length); break; }
          }
        }
        return `\`${v}\`=${JSON.stringify(val)}`;
      });
      lines.push(`${i + 1}. ${parts.join(" | ")}`);
    });
    if (bindings.length > 20) lines.push(`… and ${bindings.length - 20} more rows`);
    return lines.join("\n");
  }
}
