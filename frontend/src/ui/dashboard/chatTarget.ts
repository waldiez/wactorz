/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Who the chat talks to: the agent's id, its name, and how the choice was made.
 *
 * The chat keeps its threads by id, since two agents may share a name, so the
 * target is an id. Its name is kept beside it for what shows it — the composer,
 * the header, the address — and for when the agent has gone from the list. An
 * address names a conversation by name (`#/chat/<name>`), or by `name@node`
 * where two agents share the name. One that names no agent on the list, or
 * several, stays the choice as it was written until it names one.
 *
 * No DOM: the chat draws from `id` and `name`, and is told when they change.
 */
import type { AgentInfo } from "../../types/agent";
import { MAIN_AGENT } from "../../agents/naming";
import { labelAmong, resolveAmong } from "../../agents/addresses";
import type { View } from "./types";
import { rememberConversation, rememberedConversation } from "./viewRoutes";
import { replacementTarget, settlingTarget } from "./chatRouting";

/** How a target was arrived at; see `ChatTarget.move`. */
export type TargetMove = "claim" | "settle" | "default";

/** What the target needs from the chat. */
export interface TargetHost {
    /** The agents there are, by id. */
    agents(): ReadonlyMap<string, AgentInfo>;
    /** The view open now, for whether the address names the conversation. */
    getView(): View;
    /** The target changed: what names it is to be redrawn. */
    changed(): void;
}

export class ChatTarget {
    private _id = "";
    private _name = "";
    /** A name or an address asked for, which names no one agent on the list. */
    private _wanted: string | null = null;
    /** Whether the target is the person's rather than a default filled for them. */
    private _settled = false;
    /** The target's address as it was when it was chosen, or when `relabelled` last looked. */
    private _labelled = "";

    constructor(private readonly host: TargetHost) {}

    /** The id of the agent talked to, or "" when there is none, or it is wanted by name. */
    get id(): string {
        return this._id;
    }

    /** The name of the agent talked to, or "" before anyone is. */
    get name(): string {
        return this.host.agents().get(this._id)?.name ?? this._name;
    }

    /** Whether the person chose it, or was moved to it because theirs went away. */
    get settled(): boolean {
        return this._settled;
    }

    /** What the conversation's address calls the target: its name, its address where the
     *  name is shared, or what was asked for while that names no one agent. */
    get address(): string {
        if (this._wanted !== null) {
            return this._wanted;
        }
        const agents = this.host.agents();
        const agent = agents.get(this._id);
        return agent ? labelAmong(agent, agents.values()) : this._name;
    }

    /** Whether the target is a name whose agent has not arrived, or one several agents share. */
    get waiting(): boolean {
        return this._wanted !== null;
    }

    /** Take the target shown so far as the person's: they have seen it. */
    settle(): void {
        this._settled = true;
    }

    /**
     * Make the agent `id` (called `name`, when the list does not say) the target.
     *
     * `claim` is the person's own choice: picking an agent, sending or speaking
     * to one. It is settled, so nothing moves it under them, and remembered, so
     * it is in the address and is where the next load opens. `settle` is a move
     * made for them because theirs went away: settled, but not remembered, or
     * the next load would bring it back as though chosen. `default` fills an
     * empty choice while they have been shown nothing.
     */
    move(id: string, how: TargetMove, name?: string): void {
        this._wanted = null;
        this._id = id;
        this._name = name ?? this.host.agents().get(id)?.name ?? "";
        this._labelled = this.address;
        if (how !== "default") {
            this._settled = true;
        }
        if (how === "claim" && id) {
            rememberConversation(id, this.address, this.host.getView());
        }
        this.host.changed();
    }

    /** The one agent `wanted`, a name or `name@node`, names: undefined for none, or for several. */
    named(wanted: string): AgentInfo | undefined {
        const found = resolveAmong(wanted, this.host.agents().values());
        return found.length === 1 ? found[0] : undefined;
    }

    /** The agents `wanted` names when it names more than one: the choice it leaves open. */
    choices(wanted: string): AgentInfo[] {
        const found = resolveAmong(wanted, this.host.agents().values());
        return found.length > 1 ? found : [];
    }

    /**
     * The target's address, if it changed since the target was chosen or this
     * was last asked: another agent of its name came or went. Null when it is
     * called as before, and while the target is a name asked for.
     */
    relabelled(): string | null {
        const was = this._labelled;
        this._labelled = this.address;
        return this._id && this._labelled !== was ? this._labelled : null;
    }

    /** The addresses of the agents a name asked for fits, when it fits more than one: what to offer. */
    offered(): string[] {
        if (this._wanted === null) {
            return [];
        }
        const agents = [...this.host.agents().values()];
        return this.choices(this._wanted).map(agent => labelAmong(agent, agents));
    }

    /** Hold `name`, which names no one agent on the list, as the person's choice. */
    want(name: string): void {
        this._wanted = name;
        this._id = "";
        this._name = name;
        this._settled = true;
        this.host.changed();
    }

    /** The agent the wanted name now names, once it has arrived. */
    arrived(): AgentInfo | undefined {
        return this._wanted === null ? undefined : this.named(this._wanted);
    }

    /**
     * Fill an empty choice, or upgrade a default not yet shown (`settlingTarget`
     * says when); the agent a wanted name names, once it arrives, is returned for
     * the chat to open as the person's choice. Whether the target changed.
     */
    settleOn(): { changed: boolean; arrived?: AgentInfo } {
        if (this._wanted !== null) {
            const arrived = this.arrived();
            return arrived ? { changed: true, arrived } : { changed: false };
        }
        const before = this._id;
        const agents = [...this.host.agents().values()];
        const resolved = settlingTarget(agents, before, rememberedConversation(), this._settled);
        if (resolved !== before) {
            this.move(resolved, "default");
        }
        return { changed: this._id !== before };
    }

    /** Move off a target that has gone for good; the name of the one lost, or null to stay. */
    replaceIfGone(): string | null {
        const next = replacementTarget([...this.host.agents().values()], this._id);
        if (next === null) {
            return null;
        }
        const lost = this.name;
        this.move(next.id, "settle");
        return lost;
    }

    /** The id of main, which takes what is said to no one in particular, or "". */
    mainId(): string {
        return [...this.host.agents().values()].find(a => a.name === MAIN_AGENT && !a.node)?.id ?? "";
    }
}
