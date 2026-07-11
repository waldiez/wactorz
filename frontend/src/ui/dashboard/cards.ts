/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Overview-view builders: the host resource bar, the summary stat cards and the
 * per-agent "wactor" cards. Pure DOM factories — agent interactions are routed
 * back through the supplied callbacks.
 */
import type { AgentInfo } from "../../types/agent";
import { stateColor, stateLabel, relTime, canDirectMessage } from "./agentState";
import type { CostLimitInfo } from "./settings";

/** Compact token count for the card meta line: 1234 → "1.2k", 1_200_000 → "1.2M". */
function fmtTokens(n: number): string {
    if (n >= 1_000_000) {
        return `${(n / 1_000_000).toFixed(1)}M`;
    }
    if (n >= 1_000) {
        return `${(n / 1_000).toFixed(1)}k`;
    }
    return String(n);
}

export interface HostBarValues {
    cpuPct: number;
    cpuText: string;
    memPct: number;
    memText: string;
}

/** Clamp/format CPU + memory into host-bar display values, shared by the
 *  initial build and the live-patch paint so the two can't drift apart. */
export function hostBarValues(
    cpu: number | null,
    memUsed: number | null,
    memTotal: number | null,
): HostBarValues {
    const cpuPct = cpu != null ? Math.max(0, Math.min(100, cpu)) : 0;
    const cpuText = cpu != null ? `${cpu.toFixed(1)}%` : "—";
    const memPct =
        memUsed != null && memTotal != null && memTotal > 0
            ? Math.max(0, Math.min(100, (memUsed / memTotal) * 100))
            : 0;
    const memText =
        memUsed != null
            ? memTotal != null && memTotal > 0
                ? `${(memUsed / 1024).toFixed(1)} / ${(memTotal / 1024).toFixed(1)} GB`
                : `${memUsed.toFixed(0)} MB`
            : "—";
    return { cpuPct, cpuText, memPct, memText };
}

/** Build the host CPU/memory resource bar (gracefully blank when a stat is null). */
export function buildHostBar(
    cpu: number | null,
    memUsed: number | null,
    memTotal: number | null,
): HTMLElement {
    const bar = document.createElement("div");
    bar.id = "af-host-bar";
    bar.className = "af-host-bar";

    const { cpuPct, cpuText, memPct, memText } = hostBarValues(cpu, memUsed, memTotal);

    bar.innerHTML = `
      <div class="af-host-label">APP</div>
      <div class="af-host-metric">
        <div class="af-host-metric-label">CPU</div>
        <div class="af-host-bar-track">
          <div class="af-host-bar-fill af-host-bar-fill-cpu" style="width:${cpuPct.toFixed(1)}%"></div>
        </div>
        <div class="af-host-metric-val af-host-cpu-val">${cpuText}</div>
      </div>
      <div class="af-host-metric">
        <div class="af-host-metric-label">MEM</div>
        <div class="af-host-bar-track">
          <div class="af-host-bar-fill af-host-bar-fill-mem" style="width:${memPct.toFixed(1)}%"></div>
        </div>
        <div class="af-host-metric-val af-host-mem-val">${memText}</div>
      </div>
    `;
    return bar;
}

export interface StatCardData {
    agents: AgentInfo[];
    totalMessages: number | null;
    totalCostUsd: number | null;
    feedCount: number;
    costLimit: CostLimitInfo | null;
}

interface StatSpec {
    label: string;
    value: string;
    detail: string;
    accent: string;
    extra: string;
}

function costExtraBar(pct: number, barColor: string): string {
    return `
      <div style="margin-top:8px;background:rgba(255,255,255,0.08);border-radius:4px;height:6px;overflow:hidden">
        <div style="width:${pct}%;height:100%;background:${barColor};border-radius:4px;transition:width 0.4s"></div>
      </div>`;
}

/** Detail/accent/progress-bar for the Cost stat card from the spend-limit info. */
function costSummary(lim: CostLimitInfo | null): { detail: string; accent: string; extra: string } {
    if (!lim || typeof lim.limit_usd !== "number" || lim.limit_usd <= 0) {
        return { detail: "reported by actors", accent: "#f59e0b", extra: "" };
    }
    const barColor = lim.limit_reached ? "#ef4444" : lim.warning ? "#f59e0b" : "#22d3a0";
    const pct = Math.min(lim.pct_used ?? 0, 100);
    const periodLabel =
        lim.period === "daily" ? "today" : lim.period === "weekly" ? "this week" : "this month";
    return {
        detail: `$${(lim.spend_usd ?? 0).toFixed(4)} / $${lim.limit_usd.toFixed(2)} ${periodLabel}`,
        accent: lim.limit_reached ? "#ef4444" : "#f59e0b",
        extra: costExtraBar(pct, barColor),
    };
}

function computeStatSpecs(data: StatCardData): StatSpec[] {
    const { agents } = data;
    const healthy = agents.filter(a => stateLabel(a.state) === "running").length;
    const msgs =
        data.totalMessages !== null
            ? data.totalMessages
            : agents.reduce((s, a) => s + (a.messagesProcessed ?? 0), 0);
    const cost =
        data.totalCostUsd !== null ? data.totalCostUsd : agents.reduce((s, a) => s + (a.costUsd ?? 0), 0);
    const costInfo = costSummary(data.costLimit);

    return [
        {
            label: "Wactorz",
            value: String(agents.length),
            detail: `${healthy} running`,
            accent: "#60a5fa",
            extra: "",
        },
        {
            label: "Messages",
            value: String(msgs),
            detail: "processed across actors",
            accent: "#22d3a0",
            extra: "",
        },
        { label: "Cost", value: `$${cost.toFixed(4)}`, ...costInfo },
        {
            label: "Feed Events",
            value: String(data.feedCount),
            detail: "since dashboard loaded",
            accent: "#8b5cf6",
            extra: "",
        },
    ];
}

/** Render the summary stat cards (agents, messages, cost, feed count) into `container`. */
export function buildStatCards(container: HTMLElement, data: StatCardData): void {
    container.innerHTML = "";
    computeStatSpecs(data).forEach(({ label, value, detail, accent, extra }) => {
        const card = document.createElement("div");
        card.className = "af-stat-card";
        card.style.borderColor = `${accent}44`;
        // Safe innerHTML: label/value/detail/accent/extra all come from
        // computeStatSpecs — fixed strings, numbers and hex colors, never
        // user- or agent-supplied input.
        card.innerHTML = `
        <div class="af-stat-label">${label}</div>
        <div class="af-stat-value" style="color:${accent}">${value}</div>
        <div class="af-stat-detail">${detail}</div>
        ${extra}
      `;
        container.appendChild(card);
    });
}

export type AgentAction = "pause" | "resume" | "stop" | "delete";

export interface WactorCardCallbacks {
    onChat: (agent: AgentInfo) => void;
    onCommand: (agentId: string, action: AgentAction, btn: HTMLButtonElement) => void;
}

/** Append the pause/resume/stop/delete action buttons appropriate to the state. */
export function appendActionBtns(controls: HTMLElement, agent: AgentInfo): void {
    if (!canDirectMessage(agent)) {
        return;
    }
    const status = stateLabel(agent.state);
    const add = (label: string, action: AgentAction, danger = false) => {
        const b = document.createElement("button");
        b.className = `af-mini-btn${danger ? " danger" : ""}`;
        b.textContent = label;
        b.dataset["action"] = action;
        controls.appendChild(b);
    };
    if (status === "running") {
        add("Pause", "pause");
    }
    if (status === "paused") {
        add("Resume", "resume");
    }
    if (!agent.protected && status !== "stopped") {
        add("Stop", "stop", true);
    }
    if (!agent.protected) {
        add("Delete", "delete", true);
    }
}

function buildCardControls(agent: AgentInfo, cb: WactorCardCallbacks): HTMLElement {
    const controls = document.createElement("div");
    controls.className = "af-card-controls";

    if (canDirectMessage(agent)) {
        const chatBtn = document.createElement("button");
        chatBtn.className = "af-mini-btn af-chat-btn";
        chatBtn.textContent = "Chat";
        chatBtn.hidden = stateLabel(agent.state) === "stopped";
        chatBtn.addEventListener("click", e => {
            e.stopPropagation();
            cb.onChat(agent);
        });
        controls.appendChild(chatBtn);
    }
    appendActionBtns(controls, agent);
    controls.addEventListener("click", e => {
        const btn = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-action]");
        if (!btn || btn.disabled) {
            return;
        }
        e.stopPropagation();
        cb.onCommand(agent.id, btn.dataset["action"] as AgentAction, btn);
    });
    return controls;
}

/** The dot + name + state label + meta line (everything above the controls). */
function appendCardHeader(card: HTMLElement, agent: AgentInfo, hbMs: number): void {
    const color = stateColor(agent.state);

    const dot = document.createElement("div");
    // Pre-apply af-card-pulse when we already know this agent's heartbeat.
    dot.className = hbMs > 0 ? "af-card-state-dot af-card-pulse" : "af-card-state-dot";
    dot.style.background = color;
    dot.style.boxShadow = `0 0 8px ${color}`;

    const name = document.createElement("div");
    name.className = "af-card-name";
    name.textContent = agent.name;

    const stateLbl = document.createElement("div");
    stateLbl.className = "af-card-state-label";
    stateLbl.style.color = color;
    stateLbl.textContent = stateLabel(agent.state);

    const meta = document.createElement("div");
    meta.className = "af-card-meta";
    // Cost only when actually spent — an idle LLM agent reports $0.0000, which is noise.
    const cost = agent.costUsd ?? 0;
    meta.innerHTML = `
      <span>♥ <span class="af-card-hb-time">${hbMs ? relTime(hbMs) : "—"}</span></span>
      <span>${agent.messagesProcessed ?? 0} msgs</span>
      ${cost > 0 ? `<span>$${cost.toFixed(4)}</span>` : ""}
    `;

    card.append(dot, name, stateLbl, meta);
    appendTokenLine(card, agent);
}

/** Append the LLM token-usage line — only when there's real usage: an idle LLM
 *  agent reports 0/0 and non-LLM agents report nothing, so neither shows a line. */
function appendTokenLine(card: HTMLElement, agent: AgentInfo): void {
    const inTok = agent.inputTokens ?? 0;
    const outTok = agent.outputTokens ?? 0;
    if (inTok === 0 && outTok === 0) {
        return;
    }
    const tokens = document.createElement("div");
    tokens.className = "af-card-tokens";
    tokens.title = "tokens in / out";
    tokens.textContent = `${fmtTokens(inTok)}↑ ${fmtTokens(outTok)}↓`;
    card.appendChild(tokens);
}

/** Build a single agent ("wactor") card, wiring its control buttons to `cb`. */
export function buildWactorCard(agent: AgentInfo, hbMs: number, cb: WactorCardCallbacks): HTMLElement {
    const card = document.createElement("div");
    card.className = "af-card";
    card.dataset["id"] = agent.id;

    appendCardHeader(card, agent, hbMs);

    if (agent.task) {
        const task = document.createElement("div");
        task.className = "af-card-task";
        task.textContent = agent.task;
        task.title = agent.task;
        card.appendChild(task);
    }

    card.appendChild(buildCardControls(agent, cb));

    if (agent.protected) {
        const shield = document.createElement("div");
        shield.className = "af-card-protected";
        shield.title = "Protected wactor";
        shield.textContent = "🔒";
        card.appendChild(shield);
    }

    return card;
}
