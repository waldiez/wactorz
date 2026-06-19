/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Chat panel — per-agent threaded conversation view.
 *
 * Each agent gets an isolated message thread.  Switching agents fades the old
 * thread out and cross-fades the new one in.  Agent messages include a small
 * avatar resolved from waldiez static WebP files (or DiceBear fallback).
 *
 * Events fired on `document`:
 *   "panel-opened"          → { agent }        when panel slides in
 *   "panel-closed"          → (none)
 *   "agent-unread"          → { name, count }  when a background thread gets a message
 *   "agent-unread-cleared"  → { name }         when user opens that agent's thread
 */

import type { AgentInfo, ChatMessage } from "../types/agent";
import { agentImageGen } from "../io/AgentImageGen";
import { escapeHtml } from "./escapeHtml";
import { renderMarkdown } from "./markdown";
import { buildMessageEl, buildCopyBtn, buildAvatarHeader, dicebearFor } from "./chatMessageView";

/** Protected agents that are still directly reachable from the chat UI. */
const REACHABLE_PROTECTED = ["main", "main-actor", "home-assistant-agent", "catalog"];

/** Status-dot colour per agent state. */
const AGENT_DOT_COLORS: Record<string, string> = {
    running: "#34d399",
    paused: "#fbbf24",
    stopped: "#4b5563",
};

/** Status-dot colour for an agent state (an object state means "failed"). */
function agentDotColor(state: AgentInfo["state"]): string {
    if (typeof state === "object") {
        return "#f87171";
    }
    return AGENT_DOT_COLORS[state ?? ""] ?? "#60a5fa";
}

export class ChatPanel {
    private panel: HTMLElement;
    private agentNameEl: HTMLElement;
    private agentStatusEl: HTMLElement;
    private avatarEl: HTMLImageElement | null;
    private closeBtn: HTMLButtonElement;
    private messagesEl: HTMLElement;

    private sidebarListEl: HTMLElement;
    private sidebarSearchEl: HTMLInputElement;
    private agentList: AgentInfo[] = [];
    private sidebarFilter: string = "";

    private selectedAgent: AgentInfo | null = null;
    private activeAgentName: string | null = null;

    /** Per-agent conversation history.  Key = agent name. */
    private threads: Map<string, ChatMessage[]> = new Map();

    private _apiBase = "";
    private _historyFetched = new Set<string>();

    /** Active typing bubbles keyed by agent name. */
    private typingBubbles: Map<string, HTMLElement> = new Map();
    private typingTimeouts: Map<string, ReturnType<typeof setTimeout>> = new Map();

    /** Streaming state — one active stream at a time. */
    private _streamRow: HTMLElement | null = null;
    private _streamBody: HTMLElement | null = null;
    private _streamFrom: string | null = null;
    private _streamText: string = "";
    private _lastStreamedText: string = "";

    constructor() {
        this.panel = document.getElementById("chat-panel")!;
        this.agentNameEl = document.getElementById("panel-agent-name")!;
        this.agentStatusEl = document.getElementById("panel-agent-status")!;
        this.avatarEl = document.getElementById("panel-agent-avatar") as HTMLImageElement | null;
        this.closeBtn = document.getElementById("panel-close") as HTMLButtonElement;
        this.messagesEl = document.getElementById("chat-messages")!;
        this.sidebarListEl = document.getElementById("chat-agent-list")!;
        this.sidebarSearchEl = document.getElementById("chat-sidebar-search") as HTMLInputElement;

        this.sidebarSearchEl.addEventListener("input", () => {
            this.sidebarFilter = this.sidebarSearchEl.value.toLowerCase();
            this.renderSidebar();
        });

        this.closeBtn.addEventListener("click", () => this.close());

        // Back button (mobile): show agent list without closing the panel
        const backBtn = document.getElementById("panel-back");
        if (backBtn) {
            backBtn.addEventListener("click", () => {
                this.panel.classList.remove("agent-selected");
                this.activeAgentName = null;
                this.selectedAgent = null;
            });
        }

        document.addEventListener("keydown", e => {
            if (e.key === "Escape") {
                this.close();
            }
        });

        // Swipe-right to close (mobile)
        let _touchX = 0;
        this.panel.addEventListener(
            "touchstart",
            e => {
                _touchX = e.touches[0]?.clientX ?? 0;
            },
            { passive: true },
        );
        this.panel.addEventListener(
            "touchend",
            e => {
                if ((e.changedTouches[0]?.clientX ?? 0) - _touchX > 60) {
                    this.close();
                }
            },
            { passive: true },
        );

        document.addEventListener("agent-selected", e => {
            this.open((e as CustomEvent<{ agent: AgentInfo }>).detail.agent);
        });
    }

    /** Open or switch to the given agent's thread. */
    /** Update the panel header (name, status, avatar) for the active agent. */
    private _updateHeader(agent: AgentInfo): void {
        this.agentNameEl.textContent = agent.name;
        this.agentStatusEl.textContent =
            typeof agent.state === "object" ? "failed" : (agent.state ?? "active");
        if (this.avatarEl) {
            this.avatarEl.src = agentImageGen.get(agent);
            this.avatarEl.alt = agent.name;
            this.avatarEl.style.opacity = "1";
        }
    }

    open(agent: AgentInfo): void {
        const prev = this.activeAgentName;
        this.selectedAgent = agent;
        this.activeAgentName = agent.name;

        this._updateHeader(agent);

        // Clear unread notification for this agent
        document.dispatchEvent(new CustomEvent("agent-unread-cleared", { detail: { name: agent.name } }));

        // Update sidebar active state
        this.sidebarListEl.querySelectorAll<HTMLElement>(".af-chat-agent-row").forEach(row => {
            row.classList.toggle("active", row.dataset["name"] === agent.name);
        });

        // Fetch backend history the first time this agent's thread is opened
        if (!this._historyFetched.has(agent.name)) {
            this._historyFetched.add(agent.name);
            this._loadHistory(agent.name);
        }

        const alreadyOpen = this.panel.classList.contains("open");
        if (!alreadyOpen) {
            this.renderThread(agent.name, false);
            this.panel.classList.add("open");
        } else if (prev !== agent.name) {
            this.renderThread(agent.name, true); // animated cross-fade
        }
        this.panel.classList.add("agent-selected");

        document.dispatchEvent(
            new CustomEvent<{ agent: AgentInfo }>("panel-opened", {
                detail: { agent },
            }),
        );
    }

    /**
     * Ensure the panel is visible.  If already open, leave it untouched.
     * If closed, open with a generic header derived from `hint`.
     */
    ensureOpen(hint = "Chat"): void {
        if (this.panel.classList.contains("open")) {
            return;
        }
        this.agentNameEl.textContent = hint;
        this.agentStatusEl.textContent = "active";
        if (this.avatarEl) {
            this.avatarEl.src = dicebearFor(hint);
            this.avatarEl.alt = hint;
            this.avatarEl.style.opacity = "1";
        }
        if (!this.activeAgentName) {
            this.activeAgentName = hint;
        }
        this.renderThread(hint, false);
        this.panel.classList.add("open");
    }

    close(): void {
        this.panel.classList.remove("open", "agent-selected");
        this.selectedAgent = null;
        document.dispatchEvent(new CustomEvent("panel-closed"));
    }

    /** Route and display a chat message in the correct thread. */
    appendMessage(msg: ChatMessage): void {
        // User / system messages — and io-gateway proxy replies — belong to the
        // active thread. io-gateway is a transparent routing layer, not a real agent.
        const key =
            msg.from === "user" || msg.from === "system" || msg.from === "io-gateway"
                ? (this.activeAgentName ?? "main-actor")
                : msg.from;

        this._pushToThread(key, msg);

        if (key === this.activeAgentName) {
            this.renderMessageEl(msg);
            this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
        } else {
            // Background thread → fire unread notification
            const agentMsgCount = (this.threads.get(key) ?? []).filter(
                m => m.from !== "user" && m.from !== "system",
            ).length;
            document.dispatchEvent(
                new CustomEvent("agent-unread", {
                    detail: { name: key, count: agentMsgCount },
                }),
            );
        }
    }

    updateAgentStatus(agentId: string, state: string): void {
        if (this.selectedAgent?.id === agentId) {
            this.agentStatusEl.textContent = state;
        }
    }

    updateAgentList(agents: AgentInfo[]): void {
        this.agentList = agents;
        this.renderSidebar();
    }

    private renderSidebar(): void {
        const sorted = this._sortedAgents();
        const existing = this._existingRows();

        // Remove rows for agents no longer in the list.
        const keep = new Set(sorted.map(a => a.name));
        existing.forEach((row, name) => {
            if (!keep.has(name)) {
                row.remove();
            }
        });

        // Upsert rows in sorted order without touching unchanged rows.
        sorted.forEach((agent, idx) => this._upsertAgentRow(agent, idx, existing));
    }

    /** Filtered agents, sorted with main-actor pinned first. */
    private _sortedAgents(): AgentInfo[] {
        const filtered = this.sidebarFilter
            ? this.agentList.filter(a => a.name.toLowerCase().includes(this.sidebarFilter))
            : this.agentList;
        return [...filtered].sort((a, b) => {
            if (a.name === "main-actor") {
                return -1;
            }
            if (b.name === "main-actor") {
                return 1;
            }
            return a.name.localeCompare(b.name);
        });
    }

    /** Current sidebar rows keyed by agent name, for diffing. */
    private _existingRows(): Map<string, HTMLElement> {
        const existing = new Map<string, HTMLElement>();
        this.sidebarListEl.querySelectorAll<HTMLElement>(".af-chat-agent-row").forEach(r => {
            if (r.dataset["name"]) {
                existing.set(r.dataset["name"], r);
            }
        });
        return existing;
    }

    /** A protected agent that isn't one of the directly-reachable system agents. */
    private _isUnreachable(agent: AgentInfo): boolean {
        return agent.protected === true && !REACHABLE_PROTECTED.includes(agent.name);
    }

    /** Create a fresh sidebar row button wired to emit "agent-selected". */
    private _createAgentRow(agent: AgentInfo): HTMLButtonElement {
        const row = document.createElement("button");
        row.dataset["name"] = agent.name;
        row.innerHTML = `
          <span class="af-chat-agent-dot"></span>
          <span class="af-chat-agent-name">${escapeHtml(agent.name)}</span>
          <span class="af-chat-agent-lock" aria-hidden="true"></span>
        `;
        // Delegated name lookup so the closure always reflects latest state.
        row.addEventListener("click", () => {
            const a = this.agentList.find(x => x.name === agent.name);
            if (!a || this._isUnreachable(a)) {
                return;
            }
            document.dispatchEvent(
                new CustomEvent<{ agent: AgentInfo }>("agent-selected", { detail: { agent: a } }),
            );
        });
        return row;
    }

    /** Create-or-patch the row for `agent` and move it to position `idx`. */
    private _upsertAgentRow(agent: AgentInfo, idx: number, existing: Map<string, HTMLElement>): void {
        const isDisabled = this._isUnreachable(agent);
        const row = existing.get(agent.name) ?? this._createAgentRow(agent);

        const cls = ["af-chat-agent-row"];
        if (agent.name === this.activeAgentName) {
            cls.push("active");
        }
        if (isDisabled) {
            cls.push("protected-agent");
        }
        row.className = cls.join(" ");
        (row as HTMLButtonElement).disabled = isDisabled;
        row.title = isDisabled ? `${agent.name} — system agent, not directly reachable` : agent.name;

        const dotColor = agentDotColor(agent.state);
        const dot = row.querySelector<HTMLElement>(".af-chat-agent-dot");
        if (dot && dot.style.background !== dotColor) {
            dot.style.background = dotColor;
        }
        const lock = row.querySelector<HTMLElement>(".af-chat-agent-lock");
        if (lock) {
            lock.textContent = isDisabled ? "🔒" : "";
        }

        // Ensure correct position without re-inserting if already there.
        const sibling = this.sidebarListEl.children[idx];
        if (sibling !== row) {
            this.sidebarListEl.insertBefore(row, sibling ?? null);
        }
    }

    get activeAgent(): AgentInfo | null {
        return this.selectedAgent;
    }
    /** The full text of the most recently finalized stream (cleared after read). */
    get lastStreamedText(): string {
        const t = this._lastStreamedText;
        this._lastStreamedText = "";
        return t;
    }

    /**
     * Append a chunk to the in-progress streaming bubble.
     * Creates the bubble on the first chunk.
     */
    streamChunk(chunk: string, from: string): void {
        if (!this._streamRow) {
            this._startStreamRow(from);
        }

        this._streamText += chunk;
        if (this._streamBody) {
            // Show plain text while streaming (fast, no XSS risk)
            this._streamBody.textContent = this._streamText;
        }
        this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
    }

    /** Create the empty agent bubble that streamed chunks are appended into. */
    private _startStreamRow(from: string): void {
        this._streamFrom = from;
        this._streamText = "";

        const wrapper = document.createElement("div");
        wrapper.className = "af-chat-msg af-chat-msg-agent";

        const body = document.createElement("div");
        body.className = "af-chat-msg-body";
        const bubble = document.createElement("div");
        bubble.className = "af-chat-msg-bubble";
        body.appendChild(bubble);

        wrapper.appendChild(buildAvatarHeader(from));
        wrapper.appendChild(body);

        // Attach to the active thread in the DOM
        if (this.panel.classList.contains("open")) {
            this.messagesEl.appendChild(wrapper);
        }

        this._streamRow = wrapper;
        this._streamBody = bubble;
    }

    /** Finalize the streaming bubble: render markdown, store in thread history. */
    finalizeStream(): void {
        if (!this._streamBody || !this._streamFrom || !this._streamRow) {
            return;
        }

        // Render markdown on the completed text
        this._streamBody.replaceChildren(renderMarkdown(this._streamText));
        this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
        this._decorateFinalStream(this._streamRow, this._streamBody, this._streamText);

        // Store in thread history under the actual sender, not the active panel
        const key = this._streamFrom ?? this.activeAgentName ?? "main-actor";
        this._pushToThread(key, {
            id: `stream-${Date.now()}`,
            from: this._streamFrom,
            to: "user",
            content: this._streamText,
            timestampMs: Date.now(),
        });

        // Reset streaming state
        this._lastStreamedText = this._streamText;
        this._streamRow = null;
        this._streamBody = null;
        this._streamFrom = null;
        this._streamText = "";
    }

    /** Add the copy button + finalised timestamp once a stream completes. */
    private _decorateFinalStream(row: HTMLElement, streamBody: HTMLElement, text: string): void {
        const body = streamBody.parentElement;
        if (body) {
            body.appendChild(buildCopyBtn(text));
        }
        const header = row.querySelector<HTMLElement>(".af-chat-msg-header");
        if (header) {
            const time = document.createElement("span");
            time.className = "af-chat-msg-time";
            time.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
            header.appendChild(time);
        }
    }

    /** Append `msg` to the named thread, creating it on first use. */
    private _pushToThread(key: string, msg: ChatMessage): void {
        if (!this.threads.has(key)) {
            this.threads.set(key, []);
        }
        this.threads.get(key)!.push(msg);
    }

    /** Show a three-dot typing bubble for the given agent. */
    showTyping(agentId: string, agentName?: string): void {
        if (this.typingBubbles.has(agentId)) {
            return;
        }

        const el = document.createElement("div");
        el.className = "af-chat-msg af-chat-msg-agent";
        el.dataset["typingFor"] = agentId;

        const fromEl = document.createElement("div");
        fromEl.className = "af-chat-msg-from";
        fromEl.textContent = agentName ?? agentId;
        el.appendChild(fromEl);

        const dots = document.createElement("div");
        dots.className = "af-chat-typing";
        for (let i = 0; i < 3; i++) {
            dots.appendChild(document.createElement("span"));
        }
        el.appendChild(dots);

        // Only attach if this agent's thread is currently active
        if (agentId === this.activeAgentName || !this.activeAgentName) {
            this.messagesEl.appendChild(el);
            this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
        }

        this.typingBubbles.set(agentId, el);

        const timer = setTimeout(() => {
            this.hideTyping(agentId);
            this.appendMessage({
                id: `timeout-${agentId}`,
                from: "system",
                to: "user",
                content: `⏳ No response from **${agentName ?? agentId}** — the agent may still be processing.`,
                timestampMs: Date.now(),
            });
        }, 45_000);
        this.typingTimeouts.set(agentId, timer);
    }

    /** Remove the typing bubble for the given agent. */
    hideTyping(agentId: string): void {
        const el = this.typingBubbles.get(agentId);
        if (el) {
            el.remove();
            this.typingBubbles.delete(agentId);
        }
        const t = this.typingTimeouts.get(agentId);
        if (t !== undefined) {
            clearTimeout(t);
            this.typingTimeouts.delete(agentId);
        }
    }

    /** Set the API base URL so history can be fetched from the backend. */
    setApiBase(base: string): void {
        this._apiBase = base;
    }

    private _loadHistory(agentName: string): void {
        fetch(`${this._apiBase}/api/actors/${encodeURIComponent(agentName)}/history`)
            .then(r => (r.ok ? r.json() : Promise.resolve([])))
            .then((msgs: Array<{ role: string; content: string; ts?: number }>) => {
                if (!Array.isArray(msgs) || msgs.length === 0) {
                    return;
                }
                const firstTs = msgs[0]?.ts ? Math.round(msgs[0].ts * 1000) : Date.now();
                const histThread: ChatMessage[] = [
                    {
                        id: `history-sep-${agentName}`,
                        from: "system",
                        to: agentName,
                        content: "─── restored history ───",
                        timestampMs: firstTs,
                    },
                    ...msgs.map((m, i) => ({
                        id: `hist-${m.ts ?? i}`,
                        from: m.role === "user" ? "user" : agentName,
                        to: m.role === "user" ? agentName : "user",
                        content: m.content,
                        timestampMs: m.ts ? Math.round(m.ts * 1000) : firstTs + i,
                    })),
                ];
                const live = this.threads.get(agentName) ?? [];
                this.threads.set(agentName, [...histThread, ...live]);
                if (this.activeAgentName === agentName) {
                    this.renderThread(agentName, false);
                }
            })
            .catch(() => {});
    }

    private renderThread(agentName: string, animate: boolean): void {
        const paint = () => {
            this.messagesEl.innerHTML = "";
            for (const msg of this.threads.get(agentName) ?? []) {
                this.renderMessageEl(msg);
            }
            // Re-attach typing bubble if this agent is currently typing
            const typing = this.typingBubbles.get(agentName);
            if (typing) {
                this.messagesEl.appendChild(typing);
            }
            this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
        };

        if (animate) {
            this.messagesEl.style.opacity = "0";
            this.messagesEl.style.transition = "opacity 0.14s ease";
            setTimeout(() => {
                paint();
                this.messagesEl.style.opacity = "1";
            }, 140);
        } else {
            paint();
        }
    }

    private renderMessageEl(msg: ChatMessage): void {
        this.messagesEl.appendChild(buildMessageEl(msg));
    }
}
