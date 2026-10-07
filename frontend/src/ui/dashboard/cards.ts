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
import { button, el } from "../dom";
import { buildSparkline, lastValue, type Point } from "./trend";

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
    const bar = el("div", "af-host-bar");
    bar.id = "af-host-bar";

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
        const card = el("div", "af-stat-card");
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

export type AgentAction = "start" | "stop" | "delete";

export interface WactorCardCallbacks {
    onChat: (agent: AgentInfo) => void;
    onCommand: (agentId: string, action: AgentAction, btn: HTMLButtonElement) => void;
    /** Open the agent's history; from the card itself or its History button. */
    onHistory: (agent: AgentInfo) => void;
}

/** Append the start/stop/delete action buttons appropriate to the state. */
export function appendActionBtns(controls: HTMLElement, agent: AgentInfo): void {
    if (!canDirectMessage(agent)) {
        return;
    }
    const status = stateLabel(agent.state);
    // "caution" and "danger" read differently on purpose: stopping an agent is
    // undone by starting it again, and deleting one is not. They sit next to each
    // other, so sharing a colour invited the second when the first was meant.
    const add = (label: string, action: AgentAction, tone: "" | "caution" | "danger" = "") => {
        const b = button(`af-mini-btn${tone ? ` ${tone}` : ""}`, label);
        b.dataset["action"] = action;
        controls.appendChild(b);
    };
    if (status === "stopped") {
        // Without this, delete was the only thing left to do with a stopped
        // agent — stopping one was effectively irreversible.
        add("Start", "start");
    }
    // Two different questions: a protected agent is defined in code and cannot be
    // recreated once deleted, while an essential one cannot be stopped because
    // stopping it removes the way back.
    if (!agent.essential && status !== "stopped") {
        add("Stop", "stop", "caution");
    }
    if (!agent.protected) {
        add("Delete", "delete", "danger");
    }
}

function buildCardControls(agent: AgentInfo, cb: WactorCardCallbacks): HTMLElement {
    const controls = el("div", "af-card-controls");

    if (canDirectMessage(agent)) {
        const chatBtn = button("af-mini-btn af-chat-btn", "Chat");
        chatBtn.hidden = stateLabel(agent.state) === "stopped";
        chatBtn.addEventListener("click", e => {
            e.stopPropagation();
            cb.onChat(agent);
        });
        controls.appendChild(chatBtn);
    }
    const history = button("af-mini-btn af-history-btn", "History");
    history.addEventListener("click", e => {
        e.stopPropagation();
        cb.onHistory(agent);
    });
    controls.appendChild(history);
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

    const dot = el("div");
    // Pre-apply af-card-pulse when we already know this agent's heartbeat.
    dot.className = hbMs > 0 ? "af-card-state-dot af-card-pulse" : "af-card-state-dot";
    dot.style.background = color;
    dot.style.boxShadow = `0 0 8px ${color}`;

    const name = el("div", "af-card-name", agent.name);

    const stateLbl = el("div", "af-card-state-label", stateLabel(agent.state));
    stateLbl.style.color = color;

    const meta = el("div", "af-card-meta");
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
    const tokens = el("div", "af-card-tokens", `${fmtTokens(inTok)}↑ ${fmtTokens(outTok)}↓`);
    tokens.title = "tokens in / out";
    card.appendChild(tokens);
}

/** Build a single agent ("wactor") card, wiring its control buttons to `cb`. */
export function buildWactorCard(agent: AgentInfo, hbMs: number, cb: WactorCardCallbacks): HTMLElement {
    const card = el("div", "af-card");
    card.dataset["id"] = agent.id;
    card.dataset["name"] = agent.name;

    appendCardHeader(card, agent, hbMs);

    if (agent.task) {
        const task = el("div", "af-card-task", agent.task);
        task.title = agent.task;
        card.appendChild(task);
    }

    // Filled with the agent's activity trend once it has been fetched.
    card.appendChild(el("div", "af-card-trend"));
    card.appendChild(buildCardControls(agent, cb));
    card.addEventListener("click", () => cb.onHistory(agent));

    if (agent.protected) {
        const shield = el("div", "af-card-protected", "🔒");
        shield.title = "Protected wactor";
        card.appendChild(shield);
    }

    return card;
}

/**
 * Paint an agent's activity over the last hour into its card's trend slot: a
 * sparkline of messages per minute and the latest rate.
 *
 * ``points`` undefined means nothing was fetched for it — an agent with no
 * history yet — and leaves the slot empty rather than drawing a flat zero.
 */
export function paintCardTrend(card: HTMLElement, points: Point[] | undefined): void {
    const slot = card.querySelector<HTMLElement>(".af-card-trend");
    if (!slot) {
        return;
    }
    if (!points || points.length === 0) {
        slot.replaceChildren();
        return;
    }
    const last = lastValue(points);
    const label =
        last === null ? "—" : last === 0 ? "idle" : `${last >= 10 ? last.toFixed(0) : last.toFixed(1)}/min`;
    slot.replaceChildren(
        buildSparkline(points, `${card.dataset["name"] ?? "agent"}: messages per minute over the last hour`),
        el("span", "af-card-trend-value", label),
    );
    slot.title = "messages per minute, last hour";
}
