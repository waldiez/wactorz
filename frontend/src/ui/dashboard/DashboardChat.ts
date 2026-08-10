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
import {
    canDirectMessage,
    messageableNames,
    stateColor,
    stateLabel,
    MESSAGEABLE_PRIORITY,
} from "./agentState";
import { renderChatSidebar } from "./chatSidebar";
import { buildChatMessageEl, buildChatEmptyState } from "./chatThread";
import { buildIobar as buildChatIobar } from "./chatIobar";
import { fetchChatHistory, mergeChatHistory } from "./chatHistory";
import { ChatInput } from "./chatInput";
import { pickChatTarget, replacementAfterReset, resolveSendTarget, stripLeadingMention } from "./chatRouting";
import { SpeechToText } from "../../io/SpeechToText";
import { ChatStreamUI } from "./chatStreaming";
import { postOrWarn } from "./mutate";
import { MAIN_AGENT } from "../../agents/naming";
import { toast } from "../ToastManager";
import { UPLOADS_ENABLED } from "./uploads";
import { dropAllAttachments, renderAttachTray, withoutAttachment } from "./attachTray";
import { emit, listen } from "../../events";

/** What the chat controller needs from its host (CardDashboard). */
export interface ChatHost {
    readonly root: HTMLElement;
    readonly agents: Map<string, AgentInfo>;
    /** The currently active view (read live — it changes over time). */
    getView(): View;
    /** Switch the active dashboard view. */
    setView(v: View): void;
    /** Agents sorted with main pinned first (shared with the overview). */
    sortedAgents(): AgentInfo[];
}

export class DashboardChat {
    chatTarget = MAIN_AGENT;
    private chatMessages: ChatMessage[] = [];
    private sidebarFilter = "";

    /** Stream accumulation, live bubble and commit — extracted (size + per-agent keying). */
    private _streamUI = new ChatStreamUI({
        isChatView: () => this.host.getView() === "chat",
        lastSentTarget: () => this._lastSentTarget,
        belongsHere: (from, to) => this._msgBelongsHere({ id: "", from, to, content: "", timestampMs: 0 }),
        thread: () => this.root.querySelector<HTMLElement>(".af-chat-thread"),
        scrollThread: () => this._scrollThread(),
        commit: msg => {
            this.chatMessages.push(msg);
        },
    });
    private _lastSentTarget = MAIN_AGENT;
    // True once the user has explicitly chosen a target. Until then the picker
    // prefers main (so an agent registering before main on startup can't stick).
    private _userPicked = false;
    /**
     * Mobile master–detail: the pane is open unless the user asked for the list.
     * Derived in `_syncPaneVisibility` rather than toggled at each call site —
     * the class used to be added only in `_selectAgent`, so arriving at the view
     * with a target already chosen left the pane hidden and the screen blank.
     */
    private _listVisible = false;

    private _historyLoaded = new Set<string>();
    private _selfDispatching = false;

    private _stt = new SpeechToText(window.__WACTORZ_INGRESS_PATH ?? "");
    private _chatInput = new ChatInput({
        // Only messageable agents — mirrors the target <select> (see _populateSelect).
        agentNames: () => messageableNames(this.host.agents.values()),
        setTarget: (name: string) => this.setTarget(name),
        send: (input, select) => this._sendMessage(input, select),
    });

    private _evChat: EventListener | null = null;
    private _evChunk: EventListener | null = null;
    private _evEnd: EventListener | null = null;
    private _evResetChat: EventListener | null = null;
    private _evSendMessage: EventListener | null = null;
    private _evSendFailed: EventListener | null = null;
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
        this._userPicked = true;
    }

    /** Release the mic if a recording was in progress (dashboard hidden). */
    cancelMic(): void {
        this._stt.cancel();
    }

    /** Drop all chat state (a full reset / wipe). */
    clearAll(): void {
        this.chatMessages = [];
        this._historyLoaded.clear();
        this._pendingAttachments = dropAllAttachments(this._pendingAttachments);
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
        this._syncPaneVisibility();
        void this.loadHistory(this.chatTarget);
    }

    /** Render the pending-attachment chips into the iobar tray. */
    private _renderAttachTray(): void {
        renderAttachTray(this.root, this._pendingAttachments, att => {
            this._pendingAttachments = withoutAttachment(this._pendingAttachments, att);
            this._renderAttachTray();
        });
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

    /**
     * Point the chat view at `target` and open it.
     *
     * Sending a message is a request to see that conversation, so it overrides
     * an earlier Back. Without this, "@catalog spawn weather agent" typed on
     * Overview arrives in the chat view showing the agent list, with the reply
     * already on its way to an agent the user cannot see.
     */
    private _focusConversation(target: string): void {
        this.chatTarget = target;
        this._listVisible = false;
        // The composer names its recipient, and nothing on the send paths
        // refreshed it — an `@mention` left it advertising the previous agent,
        // "Message @main…" while replies went to catalog. Only the placeholder:
        // `updateTargetSelect` also repopulates the select, which re-runs
        // `syncChatTarget` and would snap the target back to main here, since
        // `_userPicked` is not set until after this returns.
        this._updateComposerPlaceholder();
    }

    /**
     * Entering the chat view opens a conversation rather than the agent list.
     *
     * Back is a choice within one visit, not a lasting preference: without this
     * a single Back changed what the Chat tab means until the user happened to
     * select an agent or send something, with nothing on screen saying so.
     */
    showConversation(): void {
        this._listVisible = false;
    }

    /** Re-render everything that names the current target, together. */
    private _refreshForTarget(): void {
        this.renderSidebar();
        this.renderChatPaneHeader();
        this.renderChatThread();
        this._updateComposerPlaceholder();
    }

    /** Show the chat pane, or the agent list when the user asked for it. */
    private _syncPaneVisibility(): void {
        const open = !this._listVisible && Boolean(this.chatTarget);
        this.root.querySelector(".af-chat")?.classList.toggle("agent-selected", open);
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
        this._userPicked = true;
        this._refreshForTarget();
        void this.loadHistory(name);
        this.updateTargetSelect();
        this._listVisible = false;
        this._syncPaneVisibility();
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
            this._listVisible = true;
            this._syncPaneVisibility();
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
        this._streamUI.dropRefs();

        const streamText = this._streamUI.streamHereText();
        const msgs = this.chatMessages.filter(m => this._msgBelongsHere(m));
        if (msgs.length === 0 && !streamText) {
            thread.appendChild(buildChatEmptyState(this.chatTarget));
        } else {
            msgs.forEach(m => this._appendChatMsgEl(m, thread));
        }
        if (streamText) {
            this._streamUI.reattachRow(thread);
        }
        this._scrollThread();
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
        const incoming = await fetchChatHistory(agentName);
        // null = transient failure — don't mark loaded, so the next open retries.
        if (incoming === null) {
            return;
        }
        this._historyLoaded.add(agentName);
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
            setTarget: name => this.setTarget(name),
            populateSelect: select => this._populateSelect(select),
            send: (input, select) => this._sendMessage(input, select),
            stop: () => void this._stopGeneration(),
        });
    }

    /** Cancel the in-flight generation. Success is silent (the server confirms on
     *  the chat reply path); failure is not — an unreachable backend is exactly
     *  when the user reaches for Stop. */
    private async _stopGeneration(): Promise<void> {
        await postOrWarn(`${window.__WACTORZ_INGRESS_PATH ?? ""}/api/chat/stop`, { method: "POST" }, "stop");
    }

    private _populateSelect(select: HTMLSelectElement): void {
        select.innerHTML = "";
        [...this.host.agents.values()]
            .filter(canDirectMessage)
            .sort((a, b) => {
                const ai = MESSAGEABLE_PRIORITY.indexOf(a.name);
                const bi = MESSAGEABLE_PRIORITY.indexOf(b.name);
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
        // Render only. This used to call `syncChatTarget()` and fall back to the
        // first option, so drawing the dropdown decided who the next message went
        // to — and it runs on every agent-list change, including each individual
        // removal during a reset. Once the chosen agent was momentarily absent the
        // target moved to `main`, while the pane header and thread went on naming
        // the agent the user picked. The select shows a fallback when the target
        // is not in the list; the target itself is the user's, and only a user
        // action changes it.
        const hasTarget = [...select.options].some(o => o.value === this.chatTarget);
        select.value = hasTarget ? this.chatTarget : (select.options[0]?.value ?? "");
    }

    /** Rebuild the target-agent `<select>` options from the current agent list. */
    updateTargetSelect(): void {
        const select = this.root.querySelector<HTMLSelectElement>("#af-target-select");
        if (select) {
            this._populateSelect(select);
        }
        this._updateComposerPlaceholder();
    }

    /** The composer names the agent it will send to; keep it on the target. */
    private _updateComposerPlaceholder(): void {
        const input = this.root.querySelector<HTMLTextAreaElement>("#af-iobar-input");
        if (input) {
            input.placeholder = `Message @${this.chatTarget}…`;
        }
    }

    /**
     * After a reset, move off an agent that did not survive it — and say so.
     *
     * Safe only here: the reset frame carries the settled list, so "gone" means
     * gone rather than "not back yet". A spawned agent is destroyed by a reset;
     * a system agent returns.
     */
    dropTargetIfResetRemovedIt(): void {
        const next = replacementAfterReset([...this.host.agents.values()], this.chatTarget);
        if (next === null) {
            return;
        }
        const lost = this.chatTarget;
        this.chatTarget = next;
        this._userPicked = false;
        this._refreshForTarget();
        this.updateTargetSelect();
        toast.show({
            type: "system",
            title: "Chat moved",
            message: `@${lost} did not survive the reset — now messaging @${this.chatTarget}.`,
        });
    }

    /** Keep chatTarget on a live messageable agent (prefers main; never an id). */
    syncChatTarget(): void {
        this.chatTarget = pickChatTarget([...this.host.agents.values()], this.chatTarget, this._userPicked);
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
            select.value || MAIN_AGENT,
        );
        this._focusConversation(target);
        // An @mention that routes elsewhere is a deliberate pick — keep it sticky
        // so syncChatTarget() won't snap the view back to main on the next reply.
        this._userPicked ||= target !== prevTarget;
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
        this._pendingAttachments = dropAllAttachments(this._pendingAttachments);
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
        // Already in the chat view: `_focusConversation` cleared the list, but
        // only a mount applies that. Sending from the agent list otherwise left
        // the user on the list, with the reply arriving out of sight.
        this._syncPaneVisibility();
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
            ["af-send-failed", this._evSendFailed],
            ["af-attachment-added", this._evAttach],
        ];
        pairs.forEach(([name, fn]) => {
            if (fn) {
                document.removeEventListener(name, fn);
            }
        });
        this._evChat = this._evChunk = this._evEnd = this._evResetChat = this._evSendMessage = null;
        this._evSendFailed = this._evAttach = null;
    }

    private _wireChatEvents(): void {
        this._evChat = listen("af-chat-message", detail => {
            const msg = detail.msg;
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
        });

        this._evResetChat = listen("af-reset-chat", detail => {
            const agent = detail.agent;
            // Scoped reset: drop that agent's replies and the user's messages
            // *to that agent* — user messages addressed to others survive.
            this.chatMessages = agent
                ? this.chatMessages.filter(m => m.from !== agent && !(m.from === "user" && m.to === agent))
                : [];
            this._historyLoaded.clear();
            if (this.host.getView() === "chat") {
                this.renderChatThread();
            }
        });

        this._evSendFailed = listen("af-send-failed", detail => {
            // The optimistic bubble was rendered before the transport attempt;
            // a failed send must not stay on screen looking delivered.
            let idx = -1;
            for (let i = this.chatMessages.length - 1; i >= 0; i--) {
                const m = this.chatMessages[i];
                if (m && m.from === "user" && m.content === detail.content) {
                    idx = i;
                    break;
                }
            }
            if (idx === -1) {
                return;
            }
            this.chatMessages.splice(idx, 1);
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
            this._focusConversation(target);
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
        this._evChunk = listen("af-stream-chunk", detail => this._streamUI.onChunk(detail));
        this._evEnd = listen("af-stream-end", detail => this._streamUI.onEnd(detail));
    }
}
