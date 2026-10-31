/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Chat concern of the dashboard, a composed sub-controller. Owns the chat
 * thread + streaming + send + history state and the iobar; reaches back to the
 * host only for the shared root element, the agents (read-only), and view switching.
 */
import type { AgentInfo, ChatMessage, Attachment } from "../../types/agent";
import type { View } from "./types";
import { canOpenThread, messageableNames } from "./agentState";
import { renderChatSidebar } from "./chatSidebar";
import { buildChatPane, buildChatSidebar, renderPaneHeader } from "./chatLayout";
import { renderTargetSelect } from "./targetSelect";
import { buildChatMessageEl, buildChatChoicesState, buildChatEmptyState } from "./chatThread";
import { buildIobar as buildChatIobar, nameRecipient } from "./chatIobar";
import { fetchChatHistory } from "./chatHistory";
import { ChatLog, threadOf } from "../../chat/chatLog";
import { ChatInput } from "./chatInput";
import { el } from "../dom";
import { filedWithLastSent, senderOf, voiceThreadTarget } from "./chatRouting";
import { ChatAnswers } from "./chatAnswers";
import { ChatStreamUI } from "./chatStreaming";
import { streamKey } from "../../io/agentStreams";
import { postOrWarn } from "./mutate";
import { apiUrl } from "../../io/api";
import { toast } from "../ToastManager";
import { dropAllAttachments, renderAttachTray, withoutAttachment } from "./attachTray";
import { emit, Listeners } from "../../events";
import { ChatSending } from "./chatSending";
import { ChatTarget } from "./chatTarget";

/** What the chat controller needs from its host (CardDashboard). */
export interface ChatHost {
    readonly root: HTMLElement;
    readonly agents: ReadonlyMap<string, AgentInfo>;
    /** The currently active view (read live — it changes over time). */
    getView(): View;
    /** Switch the active dashboard view. */
    setView(v: View): void;
    /** Agents sorted with main pinned first (shared with the overview). */
    sortedAgents(): AgentInfo[];
    /** What the page calls `agent`: its name, or `name@node` once the name is shared. */
    label(agent: AgentInfo): string;
}

export class DashboardChat {
    /**
     * Who the user is talking to — their choice, and nothing else. Empty until
     * one is made (or resolved for them on first arrival): "never chose" is not
     * "chose main", so nothing draws over a choice that was never made.
     */
    private _target = new ChatTarget({
        agents: () => this.host.agents,
        getView: () => this.host.getView(),
        changed: () => nameRecipient(this.root, this.chatTargetName),
    });

    /** The id of the agent the user is talking to, or "" before anyone is. */
    get chatTarget(): string {
        return this._target.id;
    }

    /** The name of the agent the user is talking to, or "" before anyone is. */
    get chatTargetName(): string {
        return this._target.name;
    }

    /** What the conversation's address (`#/chat/<…>`) calls the target: its name, or its address where the name is shared. */
    get chatTargetAddress(): string {
        return this._target.address;
    }
    /** Every message the chat holds; nothing else writes them. */
    private _log = new ChatLog();
    /** Getting what the person says to the agent they address. */
    private _sending = new ChatSending({
        log: this._log,
        target: () => ({ id: this.chatTarget, name: this.chatTargetName }),
        mainId: () => this._target.mainId(),
        agents: () => [...this.host.agents.values()],
        focus: thread => this._focusConversation(thread),
        showSent: (msg, switched) => this._showSentMessage(msg, switched),
        setTarget: id => this.setTarget(id),
        redrawThread: () => {
            if (this.host.getView() === "chat") {
                this.renderChatThread();
            }
        },
        work: {
            awaiting: (name, answerer, thread) => this._streamUI.awaiting(name, answerer, thread),
            isAwaiting: () => this._streamUI.isAwaiting,
            endWait: () => this._streamUI.endWait(),
        },
    });
    private sidebarFilter = "";

    /** Stream accumulation, live bubble and commit — extracted (size + per-agent keying). */
    private _streamUI: ChatStreamUI = new ChatStreamUI({
        isChatView: () => this.host.getView() === "chat",
        lastSentTarget: () => ({
            ...this._sending.lastSent,
            id: this._sending.lastSent.id || this._target.mainId(),
        }),
        isOpenThread: id => id === this.chatTarget,
        thread: () => this.root.querySelector<HTMLElement>(".af-chat-thread"),
        scrollThread: () => this._scrollThread(),
        commit: msg => {
            this._log.add(msg);
        },
    });
    /**
     * Mobile master–detail: the pane is open unless the user asked for the list.
     * Derived in `_syncPaneVisibility` rather than toggled at each call site —
     * the class used to be added only in `_selectAgent`, so arriving at the view
     * with a target already chosen left the pane hidden and the screen blank.
     */
    private _listVisible = false;

    private _chatInput = new ChatInput({
        // Only messageable agents — mirrors the target <select> (see _populateSelect).
        agentNames: () => messageableNames(this.host.agents.values(), agent => this.host.label(agent)),
        setTarget: (id: string) => this.setTarget(id),
        send: input => this._sendMessage(input),
    });

    /** Questions and "which one?" replies, answered by a press. */
    private _answers = new ChatAnswers({
        log: this._log,
        redrawThread: msg => {
            if (this.host.getView() === "chat" && (!msg || this._msgBelongsHere(msg))) {
                this.renderChatThread();
            }
        },
        redrawSidebar: () => this.renderSidebar(),
        send: (content, attachments) => void this._sending.send(content, attachments),
    });

    /** What `wire` subscribed to, dropped together by `unwire`. */
    private _listeners = new Listeners();
    /** The input bar's, which outlive `unwire`: the bar is built once. */
    private _barListeners = new Listeners();

    /** Files attached but not yet sent (rendered as chips in the iobar tray). */
    private _pendingAttachments: Attachment[] = [];

    constructor(private host: ChatHost) {}

    private get root(): HTMLElement {
        return this.host.root;
    }

    /**
     * Switch the conversation to the agent `id` — the one owner of a target change.
     *
     * Every way of choosing an agent lands here (wactor-card "Chat" button, the
     * target `<select>`, the `@mention` panel, the sidebar), so the pane header,
     * the thread and the composer move together.
     */
    setTarget(id: string): void {
        this._target.move(id, "claim");
        this._refreshForTarget();
        void this.loadHistory(id);
        this.updateTargetSelect();
    }

    /**
     * Open the conversation an address names: `#/chat/<name>` or
     * `#/chat/<name>@<node>`. A name is one agent while no other has it. One
     * that names no agent on the list stays the choice, as a choice of an agent
     * that stopped does: the conversation says whom it is with, a send to it is
     * refused, and it opens on the agent once one of that name arrives. A name
     * several agents share opens none of them: the pane offers each by its
     * address.
     */
    openByName(name: string): void {
        const agent = this._target.named(name);
        if (agent) {
            this.setTarget(agent.id);
            return;
        }
        this._target.want(name);
        this._refreshForTarget();
        this.updateTargetSelect();
    }

    /** The open conversation's address, if another agent of its name came or went since it was
     *  last asked (see `ChatTarget.relabelled`); null when it is called as before. */
    relabelled(): string | null {
        return this._target.relabelled();
    }

    /** Let everything go (the dashboard goes). */
    destroy(): void {
        this.unwire();
        this._barListeners.off();
    }

    /** Drop all chat state (a full reset / wipe). */
    clearAll(): void {
        this._log.clear();
        this._sending.turns.clear();
        this._pendingAttachments = dropAllAttachments(this._pendingAttachments);
    }

    /** Forget that the agent `id`'s history was loaded (so it re-fetches if re-added). */
    forgetHistory(id: string): void {
        this._log.forgetHistory(id);
    }

    /** Build the chat view element (sidebar + pane); renders run again in afterMount once attached. */
    buildChatView(): HTMLElement {
        // Building the view is the first moment there is something to open on.
        this.resolveDefaultTarget();
        const chat = el("div", "af-chat");
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
            this._target.settle();
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
        // Sending is a choice too. An `@mention` that reaches another agent
        // moves the view there once the server says which (`_turnReached`).
        this._target.move(target, "claim");
        this._listVisible = false;
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
        nameRecipient(this.root, this.chatTargetName);
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
        renderChatSidebar(
            list,
            sorted,
            this.chatTarget,
            id => this._selectAgent(id),
            this._log.asking(),
            agent => this.host.label(agent),
        );
    }

    private _selectAgent(id: string): void {
        const latest = this.host.agents.get(id);
        if (!latest || !canOpenThread(latest, this._log.asking())) {
            return;
        }
        this.setTarget(id);
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
        const agent = this.host.agents.get(this.chatTarget);
        renderPaneHeader(hdr, agent ? this.host.label(agent) : this.chatTargetName, agent, () => {
            this._listVisible = true;
            this._syncPaneVisibility();
        });
    }

    /** True when `msg` belongs to the currently open agent thread. */
    private _msgBelongsHere(msg: ChatMessage): boolean {
        return threadOf(msg) === this.chatTarget;
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
        const msgs = this._log.thread(this.chatTarget);
        const offered = this._target.offered();
        if (offered.length) {
            thread.appendChild(buildChatChoicesState(this.chatTargetName, offered));
        } else if (msgs.length === 0 && !streamText && !this._streamUI.awaitingHere()) {
            thread.appendChild(buildChatEmptyState(this.chatTargetName));
        } else {
            msgs.forEach(m => this._appendChatMsgEl(m, thread));
        }
        if (streamText) {
            this._streamUI.reattachRow(thread);
        }
        this._streamUI.reattachWaiting();
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

    /** Fetch and merge the persisted chat history of the agent `id` once (kept by
     *  id, since two agents may share a name; subsequent calls no-op). */
    async loadHistory(id: string): Promise<void> {
        // No target chosen yet — there is no conversation to fetch.
        if (!id || this._log.historyLoaded(id)) {
            return;
        }
        const name = this.host.agents.get(id)?.name ?? (id === this.chatTarget ? this.chatTargetName : "");
        const incoming = await fetchChatHistory(id, name);
        // null = transient failure — don't mark loaded, so the next open retries.
        if (incoming === null) {
            return;
        }
        this._log.addHistory(id, incoming);
        if (incoming.length) {
            this.renderChatThread();
        }
    }

    /** Build the chat input bar (textarea, target select, attach/send controls). */
    buildIobar(): HTMLElement {
        return buildChatIobar({
            chatInput: this._chatInput,
            listeners: this._barListeners,
            target: () => this.chatTargetName,
            setTarget: id => this.setTarget(id),
            populateSelect: select => this._populateSelect(select),
            send: input => this._sendMessage(input),
            stop: () => void this._stopGeneration(),
        });
    }

    /** Cancel the in-flight generation. Success is silent (the server confirms on
     *  the chat reply path); failure is not — an unreachable backend is exactly
     *  when the user reaches for Stop. */
    private async _stopGeneration(): Promise<void> {
        // Before the request: the answer already read aloud stops now, whether
        // or not the server is there to cancel the rest of it.
        emit("tts-stop");
        await postOrWarn(apiUrl("/api/chat/stop"), { method: "POST" }, "stop");
    }

    private _populateSelect(select: HTMLSelectElement): void {
        renderTargetSelect(select, [...this.host.agents.values()], this.chatTarget, agent =>
            this.host.label(agent),
        );
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
        nameRecipient(this.root, this.chatTargetName);
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
        const lost = this._target.replaceIfGone();
        if (lost === null) {
            return;
        }
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
            message: `@${lost} ${what} — now messaging @${this.chatTargetName}.`,
        });
    }

    /**
     * Fill an empty choice, and let the agent that should have had it win the
     * race it would otherwise lose.
     *
     * Never overwrites a *settled* target: one the user picked, or one they were
     * moved to because theirs went away. Moving those under them is the whole
     * class of bug the target split removed.
     *
     * The one exception is the load window, and it exists because agents arrive
     * one frame at a time. Resolving against the first of them picks whatever
     * that partial list defaults to, and "first resolution sticks" would then
     * hold that against the agent a complete list would have chosen. So an
     * unsettled default steps aside for that agent: the remembered one, or
     * `main` when nothing is remembered — a fallback is never stored, so an
     * absent preference means the user has not chosen yet rather than that they
     * chose what they were given. It is not a move under the user: mounting the
     * chat view settles the target, so this can only fire while they have been
     * shown nothing.
     */
    resolveDefaultTarget(): void {
        const { changed, arrived } = this._target.settleOn();
        if (arrived) {
            // The address asked for it, so it is the person's choice: it stood
            // until its agent arrived, and is opened as one.
            this.setTarget(arrived.id);
            return;
        }
        // Only for a chat already on screen: one still being built is drawn,
        // and its history loaded, once it is attached (`afterMount`).
        if (changed && this.root.querySelector("#af-chat-pane-header")) {
            // A fresh page lands on the chat before any agent has arrived,
            // showing nobody's conversation, and the target filled in afterwards
            // has to reach the header and thread too. Not claimed: it is what
            // the page chose, not what the user did.
            this._refreshForTarget();
            this._syncPaneVisibility();
            void this.loadHistory(this.chatTarget);
        }
    }

    private _sendMessage(input: HTMLTextAreaElement): void {
        const content = input.value.trim();
        const attachments = this._pendingAttachments;
        if ((!content && !attachments.length) || !this._sending.send(content, attachments)) {
            return;
        }
        if (content) {
            this._chatInput.recordSent(content, input);
        }
        this._pendingAttachments = dropAllAttachments(this._pendingAttachments);
        this._renderAttachTray();
        input.value = "";
        input.style.height = "auto";
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
        this._listeners.on("af-attachment-added", detail => {
            this._pendingAttachments.push(detail.attachment);
            this._renderAttachTray();
            // Attaching a file is the first half of composing a message, so the
            // caret belongs in the composer for the second half. No view switch:
            // the iobar sits below every view, so a file dropped from the
            // overview is already visible there — and sending moves to the chat
            // view on its own.
            this.root.querySelector<HTMLTextAreaElement>("#af-iobar-input")?.focus();
        });
    }

    /** Remove all event listeners added by wire() (call when the dashboard is hidden). */
    unwire(): void {
        this._listeners.off();
    }

    private _wireChatEvents(): void {
        this._answers.wire(this._listeners);
        this._listeners.on("af-chat-message", detail => {
            const msg = detail.msg;
            const stored = filedWithLastSent(msg, this._sending.lastSent, this._target.mainId());
            // A frame delivered twice is one message.
            if (!this._log.add(stored)) {
                return;
            }
            // Some agents answer with a single chat frame and no stream at all,
            // so for those turns this is the only ending there is. Attributed
            // like any other, so a bystander's message does not end a turn it has
            // nothing to do with.
            const sender = senderOf(msg);
            if (sender) {
                this._streamUI.endWait(sender);
                this._sending.turns.ended(sender);
            }
            const voiceTarget = voiceThreadTarget(stored, this.chatTargetName, [
                ...this.host.agents.values(),
            ]);
            if (voiceTarget) {
                // Speaking to an agent is choosing it, as typing to one is.
                this._target.move(voiceTarget.id, "claim");
                this._sending.address(voiceTarget.name, voiceTarget.id);
            }
            if (this.host.getView() === "chat" && this._msgBelongsHere(stored)) {
                this._showSentMessage(stored, Boolean(voiceTarget));
            }
            // A question may open a thread the sidebar keeps closed otherwise.
            this.renderSidebar();
        });

        this._listeners.on("af-turn-reached", detail =>
            this._sending.reached(detail.id, detail.agentId, detail.name),
        );

        this._listeners.on("af-reset-chat", detail => {
            const agent = detail.agent;
            // Scoped reset: drop that agent's replies and the user's messages
            // *to that agent* — user messages addressed to others survive.
            this._log.reset(agent, detail.agentId);
            if (this.host.getView() === "chat") {
                this.renderChatThread();
            }
        });

        this._listeners.on("af-send-failed", detail => {
            // Nothing went on the wire, so no reply is coming. The send goes back to
            // wait for the socket, and its bubble with it, not looking delivered.
            this._sending.turns.failed();
            if (
                this._log.update(detail.id, m => ({ ...m, queued: true })) &&
                this.host.getView() === "chat"
            ) {
                this.renderChatThread();
            }
        });
    }

    private _wireStreamEvents(): void {
        // The socket closing means no reply is arriving for anything outstanding,
        // whichever agent it was sent to; its opening lets what waits go.
        this._listeners.on("af-socket", detail => this._sending.turns.connection(detail.open));
        this._listeners.on("af-stream-chunk", detail => {
            this._streamUI.onChunk(detail);
            this._sending.turns.chunk(streamKey(detail.agentId, detail.from));
        });
        this._listeners.on("af-stream-end", detail => {
            this._streamUI.onEnd(detail);
            this._sending.turns.ended(streamKey(detail.agentId, detail.from));
        });
    }
}
