/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Getting what the person says to the agent they address: planning a send,
 * putting it in the thread, and onto the wire when the turn under way lets it,
 * and following the message when the server says who it reached.
 *
 * Drawing stays with `DashboardChat`, which this asks through `SendingHost`.
 */
import { MAIN_AGENT } from "../../agents/naming";
import type { ChatLog } from "../../chat/chatLog";
import { TurnTracker, type PendingSend, type TurnState } from "../../chat/turnTracker";
import { emit } from "../../events";
import { uid } from "../../ids";
import type { AgentInfo, Attachment, ChatMessage } from "../../types/agent";
import { toast } from "../ToastManager";
import { planSend, type SendPlan } from "./chatRouting";

/** What sending needs from the chat. */
export interface SendingHost {
    /** Where messages are kept. */
    log: ChatLog;
    /** The agent the person is talking to. */
    target(): { id: string; name: string };
    /** Main's id, for what only main answers. */
    mainId(): string;
    /** Every agent. */
    agents(): AgentInfo[];
    /** Open the conversation `thread`, as the person's own move. */
    focus(thread: string): void;
    /** Put `msg` on screen; `switched` says the conversation changed to show it. */
    showSent(msg: ChatMessage, switched: boolean): void;
    /** Make `id` the agent the person talks to. */
    setTarget(id: string): void;
    /** Draw the open thread again, if it is on screen. */
    redrawThread(): void;
    /** The "working" row. */
    work: {
        /** Show the agent `name` working on `thread`, as `answerer`. */
        awaiting(name: string, answerer: string, thread: string): void;
        /** Whether it shows. */
        isAwaiting(): boolean;
        /** Stop showing it. */
        endWait(): void;
    };
}

export class ChatSending {
    /** Whom the person last sent to: replies the gateway stamps are filed with it. */
    lastSent = { name: MAIN_AGENT, id: "" };

    /** Whether a turn is under way, and the person's messages waiting for it. */
    readonly turns = new TurnTracker({
        deliver: send => this._wire(send),
        changed: state => this._turnChanged(state),
    });

    constructor(private readonly host: SendingHost) {}

    /** Send `content`, with `attachments`, from the conversation open now; false when it
     *  cannot go, which the person is told. */
    send(content: string, attachments: Attachment[]): boolean {
        // The choice, not the control. `select.value` is set programmatically on
        // every redraw and shows a fallback when the choice is not among the
        // options, so reading it here let drawing a dropdown decide where the
        // message went.
        const target = this.host.target();
        const plan = planSend(content, target, this.host.agents(), this.host.mainId());
        if (plan.blocked) {
            toast.show({ type: "alert-error", title: "Agent unavailable", message: plan.blocked });
            return false;
        }
        this._deliver(plan, target.id, attachments);
        return true;
    }

    /** Show the message; the turn tracker puts it on the wire now, or once the
     *  turn under way or the connection lets it. */
    private _deliver(plan: SendPlan, prevTarget: string, attachments: Attachment[]): void {
        this.host.focus(plan.thread);
        const msg: ChatMessage = {
            id: uid("user"),
            from: "user",
            to: plan.name,
            threadId: plan.thread,
            content: plan.shown,
            timestampMs: Date.now(),
            ...(attachments.length ? { attachments: [...attachments] } : {}),
            ...(this.turns.sendsNow() ? {} : { queued: true }),
        };
        // In the thread before the request: a send that fails at once looks
        // for its bubble to take it back.
        this.host.log.add(msg);
        this.host.showSent(msg, prevTarget !== plan.thread);
        this.turns.request({
            id: msg.id,
            content: plan.routed,
            target: plan.thread,
            name: plan.name,
            answerer: plan.answerer,
            attachments: msg.attachments?.map(a => a.id) ?? [],
        });
    }

    /** Put `send` on the wire, now the turn tracker says so: its bubble no longer
     *  waits, and the thread shows its agent working. */
    private _wire(send: PendingSend): void {
        this.address(send.name, send.target);
        const waited = this.host.log.get(send.id)?.queued === true;
        if (waited) {
            this.host.log.update(send.id, ({ queued: _sent, ...rest }) => rest);
            this.host.redrawThread();
        }
        // Not for a command: it is handled before any agent sees it and answered
        // by main, so claiming the target is working would name the wrong agent
        // and wait for a reply that is never attributed to it.
        if (!send.content.startsWith("/")) {
            this.host.work.awaiting(send.name, send.answerer, send.target);
        }
        emit("af-send-message", send);
    }

    /** The person now talks to `name`, the agent `id`: replies the gateway stamps
     *  are its, here and on the socket, which hears it from here rather than
     *  keeping its own. */
    address(name: string, id: string): void {
        this.lastSent = { name, id };
        emit("af-addressed", { agent: name });
    }

    /**
     * The server says which agent the person's message `id` reached. A mention can
     * reach an agent other than the thread it was typed in: the message moves to
     * that agent's thread, and the view with it, as sending to an agent opens its
     * conversation.
     */
    reached(id: string, agentId: string, name: string): void {
        this.turns.reached(id, agentId);
        const was = this.host.log.get(id);
        if (!agentId || !was || was.threadId === agentId) {
            return;
        }
        this.host.log.move(id, agentId, name);
        this.address(name, agentId);
        if (this.host.work.isAwaiting()) {
            this.host.work.awaiting(name, agentId, agentId);
        }
        if (was.threadId === this.host.target().id) {
            this.host.setTarget(agentId);
        } else {
            this.host.redrawThread();
        }
    }

    /** Say what the turn is now, and stop showing an agent working once it ends. */
    private _turnChanged(state: TurnState): void {
        emit("af-turn", state);
        if (state.agent === null) {
            this.host.work.endWait();
        }
    }
}
