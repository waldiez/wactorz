/**
 * LLMCostTracker — HUD widget that tracks LLM API spending per agent.
 *
 * Subscribes to `agents/\*\/metrics` via MQTTClient and accumulates
 * cost_usd, input_tokens, and output_tokens per agent.
 *
 * Renders into `#llm-cost-display` in the DOM.
 */

import type { MetricsPayload } from "../mqtt/MQTTClient";

interface AgentCost {
  name:    string;
  costUsd: number;
  inTok:   number;
  outTok:  number;
  calls:   number;
}

const LS_KEY = "waldiez_llm_cost";

function loadPersisted(): Record<string, AgentCost> {
  try {
    const raw = localStorage.getItem(LS_KEY);
    return raw ? (JSON.parse(raw) as Record<string, AgentCost>) : {};
  } catch {
    return {};
  }
}

function save(data: Record<string, AgentCost>): void {
  try { localStorage.setItem(LS_KEY, JSON.stringify(data)); } catch { /* quota */ }
}

export class LLMCostTracker {
  private costs: Record<string, AgentCost> = loadPersisted();
  private el: HTMLElement | null;
  private expanded = false;

  constructor() {
    this.el = document.getElementById("llm-cost-display");
    if (this.el) {
      this.el.addEventListener("click", () => {
        this.expanded = !this.expanded;
        this.render();
      });
      this.el.title = "LLM spend — click to expand/collapse";
    }
    this.render();
  }

  /** Call with each `metrics` MQTT event. */
  onMetrics(payload: MetricsPayload): void {
    if (!payload.agentId) return;
    const cost = payload.cost_usd ?? payload.costUsd ?? 0;
    const inTok  = payload.input_tokens  ?? payload.inputTokens  ?? 0;
    const outTok = payload.output_tokens ?? payload.outputTokens ?? 0;
    if (cost === 0 && inTok === 0 && outTok === 0) return;

    const prev = this.costs[payload.agentId] ?? {
      name:    payload.agentName ?? payload.agentId,
      costUsd: 0, inTok: 0, outTok: 0, calls: 0,
    };
    this.costs[payload.agentId] = {
      name:    payload.agentName ?? prev.name,
      costUsd: prev.costUsd + cost,
      inTok:   prev.inTok  + inTok,
      outTok:  prev.outTok + outTok,
      calls:   prev.calls  + 1,
    };
    save(this.costs);
    this.render();
  }

  /** Reset all accumulated cost data. */
  reset(): void {
    this.costs = {};
    save(this.costs);
    this.render();
  }

  private render(): void {
    if (!this.el) return;

    const entries = Object.values(this.costs);
    const totalUsd  = entries.reduce((s, e) => s + e.costUsd, 0);
    const totalIn   = entries.reduce((s, e) => s + e.inTok,   0);
    const totalOut  = entries.reduce((s, e) => s + e.outTok,  0);
    const totalTok  = totalIn + totalOut;

    if (entries.length === 0) {
      this.el.innerHTML = `<span class="llm-cost-idle" title="No LLM calls recorded yet">LLM $0.00</span>`;
      return;
    }

    const fmtUsd = (v: number) =>
      v < 0.01 ? `$${(v * 1000).toFixed(2)}m` : `$${v.toFixed(4)}`;
    const fmtTok = (n: number) =>
      n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` :
      n >= 1_000     ? `${(n / 1_000).toFixed(1)}k`     :
                       String(n);

    let html = `<span class="llm-cost-total">${fmtUsd(totalUsd)}</span>`;
    html += `<span class="llm-cost-tok">${fmtTok(totalTok)} tok</span>`;

    if (this.expanded && entries.length > 0) {
      html += `<div class="llm-cost-breakdown" role="list" aria-label="Per-agent LLM spend">`;
      const sorted = [...entries].sort((a, b) => b.costUsd - a.costUsd);
      for (const e of sorted) {
        const pct = totalUsd > 0 ? Math.round((e.costUsd / totalUsd) * 100) : 0;
        html += `
          <div class="llm-cost-row" role="listitem" title="${e.inTok} in + ${e.outTok} out tokens">
            <span class="llm-cost-name">${e.name}</span>
            <span class="llm-cost-val">${fmtUsd(e.costUsd)} <small>(${pct}%)</small></span>
          </div>`;
      }
      html += `<button class="llm-cost-reset" title="Reset cost data" aria-label="Reset LLM cost tracking">↺ reset</button>`;
      html += `</div>`;
    }

    this.el.innerHTML = html;
    this.el.setAttribute("aria-label", `LLM spend: ${fmtUsd(totalUsd)}, ${fmtTok(totalTok)} tokens`);

    // Wire reset button
    this.el.querySelector(".llm-cost-reset")?.addEventListener("click", (e) => {
      e.stopPropagation();
      this.reset();
    });
  }
}
