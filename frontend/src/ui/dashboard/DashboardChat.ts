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
import { uid } from "../../ids";
import type { View } from "./types";
import { canDirectMessage, messageableNames, stateColor, stateLabel } from "./agentState";
import { renderChatSidebar } from "./chatSidebar";
import { buildChatMessageEl, buildChatEmptyState } from "./chatThread";
import { buildIobar as buildChatIobar } from "./chatIobar";
import { fetchChatHistory, mergeChatHistory } from "./chatHistory";
import { ChatInput } from "./chatInput";
import { pickChatTarget, resolveSendTarget, stripLeadingMention, voiceThreadTarget } from "./chatRouting";
import { SpeechToText } from "../../io/SpeechToText";
import { renderMarkdown } from "../markdown";
import { UPLOADS_ENABLED } from "./uploads";
import { renderAttachTray } from "./attachTray";
import { emit, listen } from "../../events";

/** What the chat controller needs from its host (CardDashboard). */
export interface ChatHost {
    readonly root: HTMLElement;
    readonly agents: Map<string, AgentInfo>;
    /** The currently active view (read live — it changes over time). */
    getView(): View;
    /** Switch the active dashboard view. */
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

    private _stt = new SpeechToText(window.__WACTORZ_INGRESS_PATH ?? "");
    private _chatInput = new ChatInput({
        // Only messageable agents — mirrors the target <select> (see _populateSelect).
        agentNames: () => messageableNames(this.host.agents.values()),
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

    /** Build the chat view element (sidebar + pane); renders run again in afterMount once attached. */
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
        // Keep the default text type — `type="search"` adds browser chrome (a
        // clear button / WebKit rounding) that would change this field's look.
        searchInput.id = "af-agent-filter";
        searchInput.name = "agent-filter";
        searchInput.placeholder = "Filter agents…";
        searchInput.setAttribute("aria-label", "Filter agents");
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

    /** Render the agent list in the chat sidebar (honouring the current search filter). */
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

    /** Render the chat pane header (target agent name, state dot, back button). */
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

    /** Render the message thread for the active target (empty state when no messages). */
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

    /** Fetch and merge an agent's persisted chat history once, by agent NAME
     *  (history is keyed by name, not actor id; subsequent calls no-op). */
    async loadHistory(agentName: string): Promise<void> {
        if (this._historyLoaded.has(agentName)) {
            return;
        }
        this._historyLoaded.add(agentName);
        const incoming = await fetchChatHistory(agentName);
        if (!incoming.length) {
            return;
        }
        this.chatMessages.unshift(...mergeChatHistory(this.chatMessages, incoming));
        // Cap to the most recent 500, matching the live feed.
        this.chatMessages = this.chatMessages.slice(-500);
        this.renderChatThread();
    }

    /** Build the chat input bar (textarea, target select, mic/attach/send controls). */
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
        const base = window.__WACTORZ_INGRESS_PATH ?? "";
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
        const first = select.options[0];
        if (!hasTarget && first) {
            this.chatTarget = first.value;
        }
        select.value = this.chatTarget;
    }

    /** Rebuild the target-agent `<select>` options from the current agent list. */
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

        const prevTarget = this.chatTarget;
        const target = resolveSendTarget(
            content,
            [...this.host.agents.values()].map(a => a.name),
            select.value || "main-actor",
        );
        this.chatTarget = target;
        this._lastSentTarget = target;
        // The leading @mention is the routing prefix; drop it from the displayed
        // bubble/feed (the transport re-adds the canonical one). Keep the original
        // if stripping would leave nothing to send.
        const body = stripLeadingMention(content, target) || content;
        const msg: ChatMessage = {
            id: uid("user"),
            from: "user",
            to: target,
            content: body,
            timestampMs: Date.now(),
            ...(attachments.length ? { attachments: [...attachments] } : {}),
        };
        this.chatMessages.push(msg);
        this._pendingAttachments = [];
        this._renderAttachTray();
        this._showSentMessage(msg, prevTarget !== target);
        input.value = "";
        input.style.height = "auto";
        this._emitSend(body, target, msg.attachments?.map(a => a.id) ?? []);
    }

    /** Dispatch the send while flagging it as our own, so the af-send-message
     *  listener below skips the message we've already shown optimistically. */
    private _emitSend(content: string, target: string, attachments: string[]): void {
        this._selfDispatching = true;
        emit("af-send-message", { content, target, attachments });
        this._selfDispatching = false;
    }

    /** Put a just-sent user message on screen. Not in chat view → switch to it
     *  (re-renders the thread). Already there: a `@mention` may have switched the
     *  target, so re-render the pane for the new target; otherwise just append. */
    private _showSentMessage(msg: ChatMessage, targetSwitched: boolean): void {
        if (this.host.getView() !== "chat") {
            this.host.setView("chat");
            return;
        }
        if (targetSwitched) {
            this.renderSidebar();
            this.renderChatPaneHeader();
            this.renderChatThread();
            this.updateTargetSelect();
            void this.loadHistory(this.chatTarget);
            this._scrollThread();
            return;
        }
        this._appendChatMsgEl(msg);
        this._scrollThread();
    }

    /** Subscribe to chat/stream/attachment DOM events (call when the dashboard is shown). */
    wire(): void {
        this._wireChatEvents();
        this._wireStreamEvents();
        if (UPLOADS_ENABLED) {
            this._evAttach = listen("af-attachment-added", detail => {
                this._pendingAttachments.push(detail.attachment);
                this._renderAttachTray();
            });
        }
    }

    /** Remove all event listeners added by wire() (call when the dashboard is hidden). */
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
        this._evChat = listen("af-chat-message", detail => {
            const msg = detail.msg;
            const stored: ChatMessage =
                msg.from === "io-gateway" || msg.from === "system"
                    ? { ...msg, to: this._lastSentTarget }
                    : msg;
            this.chatMessages = [...this.chatMessages, stored].slice(-500);
            const voiceTarget = voiceThreadTarget(stored, this.chatTarget, [...this.host.agents.values()]);
            this.chatTarget = voiceTarget ?? this.chatTarget;
            this._lastSentTarget = voiceTarget ?? this._lastSentTarget;
            if (this.host.getView() === "chat" && this._msgBelongsHere(stored)) {
                this._showSentMessage(stored, Boolean(voiceTarget));
            }
        });

        this._evResetChat = listen("af-reset-chat", detail => {
            const agent = detail.agent;
            this.chatMessages = agent
                ? this.chatMessages.filter(m => m.from !== agent && m.from !== "user")
                : [];
            this._historyLoaded.clear();
            if (this.host.getView() === "chat") {
                this.renderChatThread();
            }
        });

        this._evSendMessage = listen("af-send-message", detail => {
            if (this._selfDispatching) {
                return;
            }
            const { content, target } = detail;
            const switched = this.chatTarget !== target;
            this.chatTarget = target;
            this._lastSentTarget = target;
            const msg: ChatMessage = {
                id: uid("user"),
                from: "user",
                to: target,
                content,
                timestampMs: Date.now(),
            };
            this.chatMessages.push(msg);
            this._showSentMessage(msg, switched);
        });
    }

    private _wireStreamEvents(): void {
        this._evChunk = listen("af-stream-chunk", detail => {
            const { chunk, from } = detail;
            if (this._streamFrom === null) {
                this._streamFrom = from;
                this._streamTarget = this._lastSentTarget;
                this._streamText = "";
            }
            // Cap accumulation so a runaway/looping stream can't grow this without bound.
            this._streamText =
                this._streamText.length < 200_000 ? this._streamText + chunk : this._streamText;
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
        });

        this._evEnd = listen("af-stream-end", () => {
            if (this._streamFrom && this._streamText) {
                this.chatMessages.push({
                    id: uid("stream"),
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
            this._streamRow = this._streamBody = null;
            this._streamFrom = this._streamTarget = null;
            this._streamText = "";
        });
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
