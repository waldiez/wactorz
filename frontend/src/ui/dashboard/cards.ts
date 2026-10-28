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
import { button, el, labelled, listItem, testId } from "../dom";
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

    bar.append(
        el("div", "af-host-label", "APP"),
        hostMetric("CPU", "cpu", cpuPct, cpuText),
        hostMetric("MEM", "mem", memPct, memText),
    );
    return bar;
}

/** One reading of the host bar: its label, a filled track, and the value in words. */
function hostMetric(label: string, kind: "cpu" | "mem", pct: number, text: string): HTMLElement {
    const fill = el("div", `af-host-bar-fill af-host-bar-fill-${kind}`);
    fill.style.width = `${pct.toFixed(1)}%`;
    // The bar shows what the value beside it says, so it is not read out as well.
    const track = el("div", "af-host-bar-track");
    track.setAttribute("aria-hidden", "true");
    track.appendChild(fill);
    const metric = el("div", "af-host-metric");
    metric.append(
        el("div", "af-host-metric-label", label),
        track,
        el("div", `af-host-metric-val af-host-${kind}-val`, text),
    );
    return metric;
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
    /** How much of a limit is used, drawn as a bar under the detail; null for none. */
    bar: { pct: number; color: string } | null;
}

/** The spend-limit bar under the Cost card's detail, which says the same in words. */
function costBar({ pct, color }: { pct: number; color: string }): HTMLElement {
    const track = el("div");
    track.setAttribute("aria-hidden", "true");
    track.style.cssText =
        "margin-top:8px;background:rgba(255,255,255,0.08);border-radius:4px;height:6px;overflow:hidden";
    const fill = el("div");
    fill.style.cssText = `width:${pct}%;height:100%;background:${color};border-radius:4px;transition:width 0.4s`;
    track.appendChild(fill);
    return track;
}

/** Detail/accent/progress-bar for the Cost stat card from the spend-limit info. */
function costSummary(lim: CostLimitInfo | null): Pick<StatSpec, "detail" | "accent" | "bar"> {
    if (!lim || typeof lim.limit_usd !== "number" || lim.limit_usd <= 0) {
        return { detail: "reported by actors", accent: "#f59e0b", bar: null };
    }
    const barColor = lim.limit_reached ? "#ef4444" : lim.warning ? "#f59e0b" : "#22d3a0";
    const pct = Math.min(lim.pct_used ?? 0, 100);
    const periodLabel =
        lim.period === "daily" ? "today" : lim.period === "weekly" ? "this week" : "this month";
    return {
        detail: `$${(lim.spend_usd ?? 0).toFixed(4)} / $${lim.limit_usd.toFixed(2)} ${periodLabel}`,
        accent: lim.limit_reached ? "#ef4444" : "#f59e0b",
        bar: { pct, color: barColor },
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
            bar: null,
        },
        {
            label: "Messages",
            value: String(msgs),
            detail: "processed across actors",
            accent: "#22d3a0",
            bar: null,
        },
        { label: "Cost", value: `$${cost.toFixed(4)}`, ...costInfo },
        {
            label: "Feed Events",
            value: String(data.feedCount),
            detail: "since dashboard loaded",
            accent: "#8b5cf6",
            bar: null,
        },
    ];
}

/** Render the summary stat cards (agents, messages, cost, feed count) into `container`. */
export function buildStatCards(container: HTMLElement, data: StatCardData): void {
    container.innerHTML = "";
    computeStatSpecs(data).forEach(({ label, value, detail, accent, bar }) => {
        const card = el("div", "af-stat-card");
        card.style.borderColor = `${accent}44`;
        const shown = el("div", "af-stat-value", value);
        shown.style.color = accent;
        card.append(el("div", "af-stat-label", label), shown, el("div", "af-stat-detail", detail));
        if (bar) {
            card.appendChild(costBar(bar));
        }
        container.appendChild(card);
    });
}

export type AgentAction = "start" | "stop" | "delete";

export interface WactorCardCallbacks {
    onChat: (agent: AgentInfo) => void;
    onCommand: (agentId: string, action: AgentAction, btn: HTMLButtonElement) => void;
    /** Open the agent's history, from the card's History button. */
    onHistory: (agent: AgentInfo) => void;
}

/** Append the start/stop/delete action buttons appropriate to the state.
 *
 *  Every card has the same buttons, so each is named after its agent as well:
 *  to someone moving between them by name, "Stop" alone does not say which
 *  agent it stops. The name starts with the word on the button, so a person
 *  who speaks the visible label to control the page still matches it. `label`
 *  is what the card calls the agent, its node included where its name is shared. */
export function appendActionBtns(controls: HTMLElement, agent: AgentInfo, label = agent.name): void {
    if (!canDirectMessage(agent)) {
        return;
    }
    const status = stateLabel(agent.state);
    // "caution" and "danger" read differently on purpose: stopping an agent is
    // undone by starting it again, and deleting one is not. They sit next to each
    // other, so sharing a colour invited the second when the first was meant.
    const add = (text: string, action: AgentAction, tone: "" | "caution" | "danger" = "") => {
        const b = labelled(button(`af-mini-btn${tone ? ` ${tone}` : ""}`, text), `${text} ${label}`);
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

function buildCardControls(agent: AgentInfo, cb: WactorCardCallbacks, label: string): HTMLElement {
    const controls = el("div", "af-card-controls");

    if (canDirectMessage(agent)) {
        const chatBtn = labelled(button("af-mini-btn af-chat-btn", "Chat"), `Chat with ${label}`);
        chatBtn.hidden = stateLabel(agent.state) === "stopped";
        chatBtn.addEventListener("click", e => {
            e.stopPropagation();
            cb.onChat(agent);
        });
        controls.appendChild(chatBtn);
    }
    const history = labelled(button("af-mini-btn af-history-btn", "History"), `History of ${label}`);
    history.addEventListener("click", () => cb.onHistory(agent));
    controls.appendChild(history);
    appendActionBtns(controls, agent, label);
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

/**
 * Say on `card` which node its agent runs on; nothing for one on main, the
 * common case. Kept apart so a card can follow an agent that moves.
 */
export function setCardNode(card: HTMLElement, agent: AgentInfo): void {
    const line = card.querySelector<HTMLElement>(".af-card-node");
    if (line) {
        line.textContent = agent.node ? `on ${agent.node}` : "";
        line.hidden = !agent.node;
    }
}

/**
 * Repaint what an existing card says of its agent: the state, in its dot and
 * its label, what it is called, and where it runs. The controls are the caller's.
 */
export function paintCard(card: HTMLElement, agent: AgentInfo, label: string): void {
    const color = stateColor(agent.state);
    card.dataset["state"] = stateLabel(agent.state);
    card.setAttribute("aria-label", label);
    const dot = card.querySelector<HTMLElement>(".af-card-state-dot");
    if (dot) {
        dot.style.background = color;
        dot.style.boxShadow = `0 0 8px ${color}`;
    }
    const state = card.querySelector<HTMLElement>(".af-card-state-label");
    if (state) {
        state.style.color = color;
        state.textContent = stateLabel(agent.state);
    }
    const name = card.querySelector<HTMLElement>(".af-card-name");
    if (name) {
        name.textContent = label;
    }
    setCardNode(card, agent);
}

/** The dot + name + node + state label + meta line (everything above the controls). */
function appendCardHeader(card: HTMLElement, agent: AgentInfo, hbMs: number, label: string): void {
    const color = stateColor(agent.state);

    const dot = el("div");
    // Pre-apply af-card-pulse when we already know this agent's heartbeat.
    dot.className = hbMs > 0 ? "af-card-state-dot af-card-pulse" : "af-card-state-dot";
    dot.style.background = color;
    dot.style.boxShadow = `0 0 8px ${color}`;

    const name = el("div", "af-card-name", label);
    const node = testId(el("div", "af-card-node"), "agent-node");

    const stateLbl = testId(el("div", "af-card-state-label", stateLabel(agent.state)), "agent-state");
    stateLbl.style.color = color;

    const meta = el("div", "af-card-meta");
    // Cost only when actually spent — an idle LLM agent reports $0.0000, which is noise.
    const cost = agent.costUsd ?? 0;
    const heartbeat = el("span", "", "♥ ");
    heartbeat.appendChild(el("span", "af-card-hb-time", hbMs ? relTime(hbMs) : "—"));
    meta.append(heartbeat, el("span", "", `${agent.messagesProcessed ?? 0} msgs`));
    if (cost > 0) {
        meta.appendChild(el("span", "", `$${cost.toFixed(4)}`));
    }

    card.append(dot, name, node, stateLbl, meta);
    setCardNode(card, agent);
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

/** Build a single agent ("wactor") card, wiring its control buttons to `cb`. `label`
 *  is what it calls the agent: its name, or `name@node` where the name is shared. */
export function buildWactorCard(
    agent: AgentInfo,
    hbMs: number,
    cb: WactorCardCallbacks,
    label = agent.name,
): HTMLElement {
    const card = listItem(testId(el("div", "af-card"), "agent-card"), label);
    card.dataset["id"] = agent.id;
    card.dataset["name"] = agent.name;
    card.dataset["state"] = stateLabel(agent.state);

    appendCardHeader(card, agent, hbMs, label);

    if (agent.task) {
        const task = el("div", "af-card-task", agent.task);
        task.title = agent.task;
        card.appendChild(task);
    }

    // Filled with the agent's activity trend once it has been fetched.
    card.appendChild(el("div", "af-card-trend"));
    card.appendChild(buildCardControls(agent, cb, label));

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
