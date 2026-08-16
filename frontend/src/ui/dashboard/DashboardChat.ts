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
import { canDirectMessage, messageableNames } from "./agentState";
import { renderChatSidebar } from "./chatSidebar";
import { buildChatPane, buildChatSidebar, renderPaneHeader } from "./chatLayout";
import { renderTargetSelect } from "./targetSelect";
import { buildChatMessageEl, buildChatEmptyState } from "./chatThread";
import { buildIobar as buildChatIobar, composerPlaceholder } from "./chatIobar";
import { fetchChatHistory, mergeChatHistory } from "./chatHistory";
import { ChatInput } from "./chatInput";
import {
    preferredChatTarget,
    replacementTarget,
    resolveSendTarget,
    sendBlockedReason,
    stripLeadingMention,
} from "./chatRouting";
import { SpeechToText } from "../../io/SpeechToText";
import { safeStorage } from "../../safeStorage";
import { ChatStreamUI } from "./chatStreaming";
import { postOrWarn } from "./mutate";
import { MAIN_AGENT } from "../../agents/naming";
import { toast } from "../ToastManager";
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

/** Where the picked agent is remembered between visits. */
const CHAT_TARGET_KEY = "wactorz-chat-target";

export class DashboardChat {
    /**
     * Who the user is talking to — their choice, and nothing else. Empty until
     * one is made (or resolved for them on first arrival), which is the state
     * the old `MAIN_AGENT` default could not express: "never chose" looked
     * exactly like "chose main", so renders felt free to overwrite it.
     */
    chatTarget = "";

    /** Whether the target is the user's rather than a default filled for them. */
    private _targetSettled = false;
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
        send: input => this._sendMessage(input),
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

    /**
     * Switch the conversation to `name` — the one owner of a target change.
     *
     * Every way of choosing an agent lands here (wactor-card "Chat" button, the
     * target `<select>`, the `@mention` panel, the sidebar). It used to set the
     * field and stop, so each caller re-rendered whatever it remembered — in
     * practice only the composer placeholder — and the pane header and thread
     * went on showing the previous conversation.
     */
    setTarget(name: string): void {
        this.chatTarget = name;
        this._claimTarget(name);
        this._refreshForTarget();
        void this.loadHistory(name);
        this.updateTargetSelect();
    }

    /**
     * Mark the target as the user's own: remembered, and no longer movable.
     *
     * The two ways in are picking an agent and sending to one. Not the third:
     * an agent going away moves the conversation by assigning the field
     * directly, and remembering that would bring it back next load as though
     * they had chosen it.
     */
    private _claimTarget(name: string): void {
        this._targetSettled = true;
        if (name) {
            safeStorage.set(CHAT_TARGET_KEY, name);
        }
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
        // Building the view is the first moment there is something to open on.
        this.resolveDefaultTarget();
        const chat = document.createElement("div");
        chat.className = "af-chat";
        chat.append(
            buildChatSidebar(this.sidebarFilter, value => {
                this.sidebarFilter = value;
                this.renderSidebar();
            }),
            buildChatPane(),
        );

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
        // Shown a real conversation, so it is theirs from here: a late-arriving
        // remembered agent must not take it over while they are reading it.
        // Guarded on there being one — mounting with nothing to open on has
        // shown them nothing, and the preference has not had its chance yet.
        if (this.chatTarget) {
            this._targetSettled = true;
        }
        void this.loadHistory(this.chatTarget);
    }

    /** Render the pending-attachment chips into the iobar tray. */
    private _renderAttachTray(): void {
        renderAttachTray(this.root, this._pendingAttachments, att => {
            this._pendingAttachments = withoutAttachment(this._pendingAttachments, att);
            this._renderAttachTray();
        });
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
        // Sending is a choice too. Both callers are a message going out — an
        // `@mention` routes it elsewhere and the view follows, so reopening on
        // the agent left behind contradicts what the screen last showed.
        this._claimTarget(target);
        this._listVisible = false;
        // The composer names its recipient, and nothing on the send paths
        // refreshed it — an `@mention` left it advertising the previous agent,
        // "Message @main…" while replies went to catalog.
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
        this.setTarget(name);
        // Picking from the sidebar is also a request to see the conversation.
        this._listVisible = false;
        this._syncPaneVisibility();
    }

    /** Render the chat pane header (target agent name, state dot, back button). */
    renderChatPaneHeader(): void {
        const hdr = this.root.querySelector<HTMLElement>("#af-chat-pane-header");
        if (!hdr) {
            return;
        }
        const agent = [...this.host.agents.values()].find(a => a.name === this.chatTarget);
        renderPaneHeader(hdr, this.chatTarget, agent, () => {
            this._listVisible = true;
            this._syncPaneVisibility();
        });
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
        // No target chosen yet — there is no conversation to fetch.
        if (!agentName || this._historyLoaded.has(agentName)) {
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
            send: input => this._sendMessage(input),
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
        renderTargetSelect(select, [...this.host.agents.values()], this.chatTarget);
    }

    /** Rebuild the target-agent `<select>` options from the current agent list. */
    updateTargetSelect(): void {
        // Resolve first, or this renders the empty choice it was called to
        // refresh. The composer is on screen from the first paint, before any
        // agent has arrived, so the call that matters is the one made when the
        // list first fills — and at that point nothing else has picked a target.
        this.resolveDefaultTarget();
        const select = this.root.querySelector<HTMLSelectElement>("#af-target-select");
        if (select) {
            this._populateSelect(select);
        }
        this._updateComposerPlaceholder();
    }

    /** Put the caret in the composer, if it is on the page yet. */
    private _focusComposer(): void {
        this.root.querySelector<HTMLTextAreaElement>("#af-iobar-input")?.focus();
    }

    /** The composer names the agent it will send to; keep it on the target. */
    private _updateComposerPlaceholder(): void {
        const input = this.root.querySelector<HTMLTextAreaElement>("#af-iobar-input");
        if (input) {
            input.placeholder = composerPlaceholder(this.chatTarget);
        }
    }

    /**
     * Move off a chat target that has left the list for good — and say so.
     *
     * Called only where "gone" is a fact: a reset frame carrying the settled
     * survivors, or a `delete_agent` frame naming the agent. Both destroy the
     * conversation rather than pausing it, so there is nothing to resume into
     * and staying put would strand the user. A *stopped* agent is the opposite
     * case — it keeps the choice, and the send blocks instead.
     */
    dropTargetIfGone(reason: "reset" | "deleted"): void {
        const next = replacementTarget([...this.host.agents.values()], this.chatTarget);
        if (next === null) {
            return;
        }
        const lost = this.chatTarget;
        this._targetSettled = true;
        this.chatTarget = next;
        this._refreshForTarget();
        this.updateTargetSelect();
        // The move always happens, or the chat is wrong when it is next opened.
        // The toast explains a change on screen, so it is only owed to someone
        // looking at the conversation that just changed.
        if (this.host.getView() !== "chat") {
            return;
        }
        const what = reason === "deleted" ? "was deleted" : "did not survive the reset";
        toast.show({
            type: "system",
            title: "Chat moved",
            message: `@${lost} ${what} — now messaging @${this.chatTarget}.`,
        });
    }

    /**
     * Fill an empty choice, and let a remembered one win the race it would lose.
     *
     * Never overwrites a *settled* target: one the user picked, or one they were
     * moved to because theirs went away. Moving those under them is the whole
     * class of bug the target split removed.
     *
     * The one exception is the load window, and it exists because agents arrive
     * one frame at a time. Resolving against the first of them picks whatever
     * that partial list defaults to — usually `main` — and "first resolution
     * sticks" would then hold that against the remembered agent arriving a
     * moment later. So an unsettled default steps aside when the remembered
     * agent turns up. It is not a move under the user: it happens before they
     * have chosen anything, and only ever towards the agent they last chose.
     */
    resolveDefaultTarget(): void {
        const agents = [...this.host.agents.values()];
        const remembered = safeStorage.get(CHAT_TARGET_KEY);
        if (!this.chatTarget) {
            this.chatTarget = preferredChatTarget(agents, remembered);
            return;
        }
        if (this._targetSettled || !remembered || remembered === this.chatTarget) {
            return;
        }
        if (agents.some(a => a.name === remembered && canDirectMessage(a))) {
            this.chatTarget = remembered;
        }
    }

    private _sendMessage(input: HTMLTextAreaElement): void {
        const content = input.value.trim();
        const attachments = this._pendingAttachments;
        if (!content && !attachments.length) {
            return;
        }
        const prevTarget = this.chatTarget;
        const agents = [...this.host.agents.values()];
        // The choice, not the control. `select.value` is set programmatically on
        // every redraw and shows a fallback when the choice is not among the
        // options, so reading it here let drawing a dropdown decide where the
        // message went.
        const target = resolveSendTarget(
            content,
            agents.map(a => a.name),
            this.chatTarget || MAIN_AGENT,
        );

        // Say so rather than sending somewhere else. Routing a message the user
        // addressed to a stopped agent onwards to main is what the original
        // complaint was, and a quiet fallback here would reproduce it.
        const blocked = sendBlockedReason(agents, target);
        if (blocked) {
            toast.show({ type: "alert-error", title: "Agent unavailable", message: blocked });
            return;
        }

        if (content) {
            this._chatInput.recordSent(content, input);
        }
        this._deliver(input, content, target, prevTarget);
    }

    /** Show the message, clear the composer and put it on the wire. */
    private _deliver(input: HTMLTextAreaElement, content: string, target: string, prevTarget: string): void {
        const attachments = this._pendingAttachments;
        this._focusConversation(target);
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
        // Wired unconditionally — `wire()` can run before /api/config answers.
        // The event only originates from the drop zone and the paste handler,
        // both of which check first, so with uploads off nothing ever arrives.
        this._evAttach = listen("af-attachment-added", detail => {
            this._pendingAttachments.push(detail.attachment);
            this._renderAttachTray();
            // Attaching a file is the first half of composing a message, so the
            // caret belongs in the composer for the second half. No view switch:
            // the iobar sits below every view, so a file dropped from the
            // overview is already visible there — and sending moves to the chat
            // view on its own.
            this._focusComposer();
        });
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
