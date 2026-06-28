/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Chat concern of the dashboard, a composed sub-controller. Owns the chat
 * thread + streaming + send + history state and the iobar; reaches back to the
 * host only for the shared root element, the agent map, and view switching.
 */
import type { AgentInfo, ChatMessage, Attachment } from "../../types/agent";
import type { View } from "./types";
import { canDirectMessage, pickChatTarget, stateColor, stateLabel } from "./agentState";
import { renderChatSidebar } from "./chatSidebar";
import { buildChatMessageEl, buildChatEmptyState } from "./chatThread";
import { buildIobar as buildChatIobar } from "./chatIobar";
import { fetchChatHistory, mergeChatHistory } from "./chatHistory";
import { ChatInput } from "./chatInput";
import { SpeechToText } from "../../io/SpeechToText";
import { renderMarkdown } from "../markdown";
import { UPLOADS_ENABLED } from "./uploads";
import { renderAttachTray } from "./attachTray";

/** What the chat controller needs from its host (CardDashboard). */
export interface ChatHost {
    readonly root: HTMLElement;
    readonly agents: Map<string, AgentInfo>;
    /** The currently active view (read live — it changes over time). */
    getView(): View;
    setView(v: View): void;
    /** Agents sorted with main-actor pinned first (shared with the overview). */
    sortedAgents(): AgentInfo[];
}

export class DashboardChat {
    chatTarget = "main-actor";
    private chatMessages: ChatMessage[] = [];
    private sidebarFilter = "";

    private _streamRow: HTMLElement | null = null;
    private _streamBody: HTMLElement | null = null;
    private _streamFrom: string | null = null;
    private _streamTarget: string | null = null;
    private _streamText = "";
    private _lastSentTarget = "main-actor";

    private _historyLoaded = new Set<string>();
    private _selfDispatching = false;

    private _stt = new SpeechToText((window as any).__WACTORZ_INGRESS_PATH ?? "");
    private _chatInput = new ChatInput({
        agentNames: () => [...this.host.agents.values()].map(a => a.name).filter(Boolean),
        setTarget: (name: string) => {
            this.chatTarget = name;
        },
        send: (input, select) => this._sendMessage(input, select),
    });

    private _evChat: EventListener | null = null;
    private _evChunk: EventListener | null = null;
    private _evEnd: EventListener | null = null;
    private _evResetChat: EventListener | null = null;
    private _evSendMessage: EventListener | null = null;
    private _evAttach: EventListener | null = null;

    /** Files attached but not yet sent (rendered as chips in the iobar tray). */
    private _pendingAttachments: Attachment[] = [];

    constructor(private host: ChatHost) {}

    private get root(): HTMLElement {
        return this.host.root;
    }

    /** Switch the open thread to `agent` (wactor-card "Chat" button). */
    setTarget(name: string): void {
        this.chatTarget = name;
    }

    /** Release the mic if a recording was in progress (dashboard hidden). */
    cancelMic(): void {
        this._stt.cancel();
    }

    /** Drop all chat state (a full reset / wipe). */
    clearAll(): void {
        this.chatMessages = [];
        this._historyLoaded.clear();
    }

    /** Forget that an agent's history was loaded (so it re-fetches if re-added). */
    forgetHistory(name: string): void {
        this._historyLoaded.delete(name);
    }

    buildChatView(): HTMLElement {
        const chat = document.createElement("div");
        chat.className = "af-chat";
        chat.append(this._buildChatSidebar(), this._buildChatPane());

        this.renderSidebar();
        this.renderChatPaneHeader();
        this.renderChatThread();
        return chat;
    }

    /** Re-run the in-DOM renders after the chat view is attached, load history. */
    afterMount(): void {
        this.renderSidebar();
        this.renderChatPaneHeader();
        this.renderChatThread();
        this._renderAttachTray();
        void this.loadHistory(this.chatTarget);
    }

    /** Render the pending-attachment chips into the iobar tray. */
    private _renderAttachTray(): void {
        renderAttachTray(this.root, this._pendingAttachments, att => this._removeAttachment(att));
    }

    private _removeAttachment(att: Attachment): void {
        this._pendingAttachments = this._pendingAttachments.filter(a => a !== att);
        if (att.url?.startsWith("blob:")) {
            URL.revokeObjectURL(att.url);
        }
        this._renderAttachTray();
    }

    private _buildChatSidebar(): HTMLElement {
        const sidebar = document.createElement("div");
        sidebar.className = "af-chat-sidebar";

        const searchWrap = document.createElement("div");
        searchWrap.className = "af-chat-sidebar-search";
        const searchInput = document.createElement("input");
        searchInput.placeholder = "Filter agents…";
        searchInput.value = this.sidebarFilter;
        searchInput.addEventListener("input", () => {
            this.sidebarFilter = searchInput.value.toLowerCase();
            this.renderSidebar();
        });
        searchWrap.appendChild(searchInput);
        sidebar.appendChild(searchWrap);

        const agentList = document.createElement("div");
        agentList.className = "af-chat-agent-list";
        agentList.id = "af-chat-agent-list";
        sidebar.appendChild(agentList);
        return sidebar;
    }

    private _buildChatPane(): HTMLElement {
        const pane = document.createElement("div");
        pane.className = "af-chat-pane";

        const paneHdr = document.createElement("div");
        paneHdr.className = "af-chat-pane-header";
        paneHdr.id = "af-chat-pane-header";

        const thread = document.createElement("div");
        thread.className = "af-chat-thread";
        thread.id = "af-chat-thread";

        pane.append(paneHdr, thread);
        return pane;
    }

    renderSidebar(): void {
        const list = this.root.querySelector<HTMLElement>("#af-chat-agent-list");
        if (!list) {
            return;
        }
        const sorted = this.host
            .sortedAgents()
            .filter(a => !this.sidebarFilter || a.name.toLowerCase().includes(this.sidebarFilter));
        renderChatSidebar(list, sorted, this.chatTarget, name => this._selectAgent(name));
    }

    private _selectAgent(name: string): void {
        const latest = [...this.host.agents.values()].find(a => a.name === name);
        if (!latest || !canDirectMessage(latest)) {
            return;
        }
        this.chatTarget = name;
        this.renderSidebar();
        this.renderChatPaneHeader();
        this.renderChatThread();
        void this.loadHistory(name);
        this.updateTargetSelect();
        // Mobile: switch to pane view.
        this.root.querySelector(".af-chat")?.classList.add("agent-selected");
    }

    renderChatPaneHeader(): void {
        const hdr = this.root.querySelector<HTMLElement>("#af-chat-pane-header");
        if (!hdr) {
            return;
        }
        hdr.innerHTML = "";
        hdr.appendChild(this._buildChatBackBtn());

        const agent = [...this.host.agents.values()].find(a => a.name === this.chatTarget);
        if (agent) {
            const dot = document.createElement("span");
            dot.className = "af-chat-agent-dot";
            dot.style.background = stateColor(agent.state);
            hdr.appendChild(dot);
        }
        const title = document.createElement("span");
        title.className = "af-chat-pane-title";
        title.textContent = `@${this.chatTarget}`;
        hdr.appendChild(title);
        if (agent) {
            const st = document.createElement("span");
            st.className = "af-chat-pane-state";
            st.textContent = stateLabel(agent.state);
            hdr.appendChild(st);
        }
    }

    private _buildChatBackBtn(): HTMLButtonElement {
        const backBtn = document.createElement("button");
        backBtn.className = "af-chat-back-btn";
        backBtn.textContent = "‹ Back";
        backBtn.addEventListener("click", () => {
            this.root.querySelector(".af-chat")?.classList.remove("agent-selected");
        });
        return backBtn;
    }

    /** True when `msg` belongs to the currently open agent thread. */
    private _msgBelongsHere(msg: ChatMessage): boolean {
        if (msg.from === "user" || msg.from === "io-gateway" || msg.from === "system") {
            return msg.to === this.chatTarget;
        }
        return msg.from === this.chatTarget;
    }

    renderChatThread(): void {
        const thread = this.root.querySelector<HTMLElement>("#af-chat-thread");
        if (!thread) {
            return;
        }
        thread.innerHTML = "";
        // The wipe detached any live stream bubble; drop the stale DOM refs.
        this._streamRow = null;
        this._streamBody = null;

        const streamHere =
            !!this._streamFrom &&
            this._streamText.length > 0 &&
            this._msgBelongsHere({
                id: "",
                from: this._streamFrom,
                to: this._streamTarget ?? this._lastSentTarget,
                content: "",
                timestampMs: 0,
            });

        const msgs = this.chatMessages.filter(m => this._msgBelongsHere(m));
        if (msgs.length === 0 && !streamHere) {
            thread.appendChild(buildChatEmptyState(this.chatTarget));
        } else {
            msgs.forEach(m => this._appendChatMsgEl(m, thread));
        }
        if (streamHere) {
            this._buildStreamRow(thread, this._streamText);
        }
        this._scrollThread();
    }

    /** Build the streaming agent bubble, append it to `thread`, and latch the
     *  row/body refs. `initialText` pre-fills the bubble (reattach on re-render). */
    private _buildStreamRow(thread: HTMLElement, initialText = ""): void {
        const row = document.createElement("div");
        row.className = "af-chat-msg af-chat-msg-agent";
        const fromEl = document.createElement("div");
        fromEl.className = "af-chat-msg-from";
        fromEl.textContent = this._streamFrom ?? "";
        const bubble = document.createElement("div");
        bubble.className = "af-chat-msg-bubble";
        bubble.textContent = initialText;
        row.append(fromEl, bubble);
        thread.appendChild(row);
        this._streamRow = row;
        this._streamBody = bubble;
    }

    private _appendChatMsgEl(msg: ChatMessage, container?: HTMLElement): void {
        const thread = container ?? this.root.querySelector<HTMLElement>("#af-chat-thread");
        if (!thread) {
            return;
        }
        thread.querySelector(".af-chat-empty")?.remove();
        thread.appendChild(buildChatMessageEl(msg));
    }

    private _scrollThread(): void {
        const thread = this.root.querySelector<HTMLElement>("#af-chat-thread");
        if (thread) {
            thread.scrollTop = thread.scrollHeight;
        }
    }

    async loadHistory(agentId: string): Promise<void> {
        if (this._historyLoaded.has(agentId)) {
            return;
        }
        this._historyLoaded.add(agentId);
        const incoming = await fetchChatHistory(agentId);
        if (!incoming.length) {
            return;
        }
        this.chatMessages.unshift(...mergeChatHistory(this.chatMessages, incoming));
        this.renderChatThread();
    }

    buildIobar(): HTMLElement {
        return buildChatIobar({
            chatInput: this._chatInput,
            stt: this._stt,
            target: () => this.chatTarget,
            setTarget: name => {
                this.chatTarget = name;
            },
            populateSelect: select => this._populateSelect(select),
            send: (input, select) => this._sendMessage(input, select),
            stop: () => this._stopGeneration(),
        });
    }

    /** Ask the backend to cancel the in-flight generation. Fire-and-forget: the
     *  server emits the "⏹ Stopped." confirmation on the usual chat reply path. */
    private _stopGeneration(): void {
        const base = (window as any).__WACTORZ_INGRESS_PATH ?? "";
        void fetch(`${base}/api/chat/stop`, { method: "POST" }).catch(() => {});
    }

    private _populateSelect(select: HTMLSelectElement): void {
        const PRIORITY = ["main", "main-actor", "home-assistant-agent", "catalog"];
        select.innerHTML = "";
        [...this.host.agents.values()]
            .filter(canDirectMessage)
            .sort((a, b) => {
                const ai = PRIORITY.indexOf(a.name);
                const bi = PRIORITY.indexOf(b.name);
                if (ai !== -1 && bi !== -1) {
                    return ai - bi;
                }
                if (ai !== -1) {
                    return -1;
                }
                if (bi !== -1) {
                    return 1;
                }
                return a.name.localeCompare(b.name);
            })
            .forEach(agent => {
                const opt = document.createElement("option");
                opt.value = agent.name;
                opt.textContent = `@${agent.name}`;
                select.appendChild(opt);
            });
        // Keep chatTarget a live, messageable agent and guarantee a selection.
        this.syncChatTarget();
        const hasTarget = [...select.options].some(o => o.value === this.chatTarget);
        if (!hasTarget && select.options.length) {
            this.chatTarget = select.options[0]!.value;
        }
        select.value = this.chatTarget;
    }

    updateTargetSelect(): void {
        const select = this.root.querySelector<HTMLSelectElement>("#af-target-select");
        if (select) {
            this._populateSelect(select);
        }
        const input = this.root.querySelector<HTMLTextAreaElement>("#af-iobar-input");
        if (input) {
            input.placeholder = `Message @${this.chatTarget}…`;
        }
    }

    /** Keep chatTarget on a live messageable agent (prefers main; never an id). */
    syncChatTarget(): void {
        this.chatTarget = pickChatTarget([...this.host.agents.values()], this.chatTarget);
    }

    private _sendMessage(input: HTMLTextAreaElement, select: HTMLSelectElement): void {
        const content = input.value.trim();
        const attachments = this._pendingAttachments;
        if (!content && !attachments.length) {
            return;
        }
        if (content) {
            this._chatInput.recordSent(content, input);
        }

        const target = select.value || "main-actor";
        this.chatTarget = target;
        this._lastSentTarget = target;
        const msg: ChatMessage = {
            id: `user-${Date.now()}`,
            from: "user",
            to: target,
            content,
            timestampMs: Date.now(),
            ...(attachments.length ? { attachments: [...attachments] } : {}),
        };
        this.chatMessages.push(msg);
        this._pendingAttachments = [];
        this._renderAttachTray();
        this._showSentMessage(msg);
        input.value = "";
        input.style.height = "auto";
        this._selfDispatching = true;
        document.dispatchEvent(
            new CustomEvent("af-send-message", {
                detail: { content, target, attachments: msg.attachments?.map(a => a.id) ?? [] },
            }),
        );
        this._selfDispatching = false;
    }

    /** Put a just-sent user message on screen: switch to the chat view (which
     *  re-renders the thread) or append it directly if already there. */
    private _showSentMessage(msg: ChatMessage): void {
        if (this.host.getView() !== "chat") {
            this.host.setView("chat");
        } else {
            this._appendChatMsgEl(msg);
            this._scrollThread();
        }
    }

    wire(): void {
        this._wireChatEvents();
        this._wireStreamEvents();
        if (UPLOADS_ENABLED) {
            this._evAttach = e => {
                const att = (e as CustomEvent<{ attachment: Attachment }>).detail.attachment;
                this._pendingAttachments.push(att);
                this._renderAttachTray();
            };
            document.addEventListener("af-attachment-added", this._evAttach);
        }
    }

    unwire(): void {
        const pairs: [string, EventListener | null][] = [
            ["af-chat-message", this._evChat],
            ["af-stream-chunk", this._evChunk],
            ["af-stream-end", this._evEnd],
            ["af-reset-chat", this._evResetChat],
            ["af-send-message", this._evSendMessage],
            ["af-attachment-added", this._evAttach],
        ];
        pairs.forEach(([name, fn]) => {
            if (fn) {
                document.removeEventListener(name, fn);
            }
        });
        this._evChat = this._evChunk = this._evEnd = this._evResetChat = this._evSendMessage = null;
        this._evAttach = null;
    }

    private _wireChatEvents(): void {
        this._evChat = e => {
            const msg = (e as CustomEvent<{ msg: ChatMessage }>).detail.msg;
            const stored: ChatMessage =
                msg.from === "io-gateway" || msg.from === "system"
                    ? { ...msg, to: this._lastSentTarget }
                    : msg;
            this.chatMessages.push(stored);
            if (this.chatMessages.length > 500) {
                this.chatMessages.shift();
            }
            if (this.host.getView() === "chat" && this._msgBelongsHere(stored)) {
                this._appendChatMsgEl(stored);
                this._scrollThread();
            }
        };
        document.addEventListener("af-chat-message", this._evChat);

        this._evResetChat = (e: Event) => {
            const agent = (e as CustomEvent).detail?.agent as string | null;
            this.chatMessages = agent
                ? this.chatMessages.filter(m => m.from !== agent && m.from !== "user")
                : [];
            this._historyLoaded.clear();
            if (this.host.getView() === "chat") {
                this.renderChatThread();
            }
        };
        document.addEventListener("af-reset-chat", this._evResetChat);

        this._evSendMessage = (e: Event) => {
            if (this._selfDispatching) {
                return;
            }
            const { content, target } = (e as CustomEvent<{ content: string; target: string }>).detail;
            this.chatTarget = target;
            this._lastSentTarget = target;
            const msg: ChatMessage = {
                id: `user-${Date.now()}`,
                from: "user",
                to: target,
                content,
                timestampMs: Date.now(),
            };
            this.chatMessages.push(msg);
            this._showSentMessage(msg);
        };
        document.addEventListener("af-send-message", this._evSendMessage);
    }

    private _wireStreamEvents(): void {
        this._evChunk = e => {
            const { chunk, from } = (e as CustomEvent<{ chunk: string; from: string }>).detail;
            if (this._streamFrom === null) {
                this._streamFrom = from;
                this._streamTarget = this._lastSentTarget;
                this._streamText = "";
            }
            this._streamText += chunk;
            if (this.host.getView() !== "chat") {
                return;
            }
            if (!this._streamRow) {
                this._ensureStreamRow();
            }
            if (this._streamBody) {
                this._streamBody.textContent = this._streamText;
            }
            this._scrollThread();
        };
        document.addEventListener("af-stream-chunk", this._evChunk);

        this._evEnd = () => {
            if (this._streamFrom && this._streamText) {
                this.chatMessages.push({
                    id: `stream-${Date.now()}`,
                    from: this._streamFrom,
                    to: this._streamTarget ?? this._lastSentTarget,
                    content: this._streamText,
                    timestampMs: Date.now(),
                });
            }
            if (this._streamBody && this._streamText) {
                this._streamBody.textContent = "";
                this._streamBody.appendChild(renderMarkdown(this._streamText));
            }
            this._streamRow = null;
            this._streamBody = null;
            this._streamFrom = null;
            this._streamTarget = null;
            this._streamText = "";
        };
        document.addEventListener("af-stream-end", this._evEnd);
    }

    /** Lazily create the streaming agent bubble in the open chat thread. */
    private _ensureStreamRow(): void {
        const thread = this.root.querySelector<HTMLElement>(".af-chat-thread");
        if (!thread) {
            return;
        }
        this._buildStreamRow(thread);
    }
}
