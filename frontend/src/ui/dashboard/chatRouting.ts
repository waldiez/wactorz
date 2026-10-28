/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Chat target & mention routing: which agent a message goes to and how its text
 * is normalised. The backend routes by the inline `@mention` regardless of the
 * picker, so the UI resolves the same target, keeps the picker on a messageable
 * agent, and strips the now-redundant prefix from what it shows.
 */
import type { AgentInfo, ChatMessage } from "../../types/agent";
import { IO_GATEWAY, looksLikeAgentId, MAIN_AGENT } from "../../agents/naming";
import { canDirectMessage, isReachable } from "./agentState";
import { resolveAmong, SEPARATOR } from "../../agents/addresses";
import { answererOf } from "../../chat/turnTracker";

/**
 * The id of the agent to open on when nothing has been chosen yet, or "" if there is none.
 *
 * Used only to fill an empty choice — it never displaces one, so whatever it
 * returns first is what the user sees named on screen and keeps. It replaced a
 * version that re-derived the target on every call and took a `userPicked` flag
 * to know when not to: that flag was a single boolean standing in for "has a
 * choice been made", and it could not tell a fallback chosen *for* the user from
 * no choice at all, so main registering late silently took the conversation over.
 *
 * Never returns an id-named agent — the backend uses UUID ids (not WIDs), so an
 * agent that never resolved keeps the id as its name and must not become the
 * chat target on its own.
 */
export function defaultChatTarget(agents: AgentInfo[]): string {
    const messageable = agents.filter(canDirectMessage);
    const main = messageable.find(a => a.name === MAIN_AGENT);
    const named = messageable
        .filter(a => !looksLikeAgentId(a.name))
        .sort((a, b) => a.name.localeCompare(b.name));
    return main?.id ?? named[0]?.id ?? "";
}

/** What a leading mention spells, `name` or `name@node`: the name a person types up to a space, and a node. */
const MENTION = /^@([A-Za-z0-9_-]+(?:@[^\s@/#+]*)?)/;

/** A leading `@name` or `@name@node`: as the agents spell it, and the agents it finds. */
export interface Mention {
    /** The name or address, spelled as the agent spells its name. */
    spelled: string;
    /** The agents it finds: none, one, or several to choose between. */
    found: AgentInfo[];
}

/**
 * The leading mention of `content` among `agents`, or null when there is none.
 * The name in it matches case-insensitively, as a person types it. The server
 * routes by the mention, and says which agent it reached; until then the
 * message stays in the thread it was typed in.
 */
export function leadingMention(content: string, agents: AgentInfo[]): Mention | null {
    const typed = MENTION.exec(content.trimStart())?.[1];
    if (!typed) {
        return null;
    }
    const lower = typed.toLowerCase();
    const named = agents.find(
        a => lower === a.name.toLowerCase() || lower.startsWith(`${a.name.toLowerCase()}${SEPARATOR}`),
    );
    const spelled = named ? `${named.name}${typed.slice(named.name.length)}` : typed;
    return { spelled, found: resolveAmong(spelled, agents) };
}

/**
 * Why a message to `chosen`, called `target`, cannot be sent right now, or null if it can.
 *
 * The send-time half of the choice/reachability split: `chatTarget` holds who the
 * user chose and keeps holding it across a stop, so this is what has to notice
 * that the choice cannot answer. Returning a reason rather than a boolean keeps
 * the wording next to the rule that produced it.
 */
export function sendBlockedReason(chosen: AgentInfo | undefined, target: string): string | null {
    if (!chosen) {
        // Only a race now that a gone target is moved off, but it is the branch
        // where "start it" would be wrong: there is nothing left to start.
        return `@${target} is no longer available.`;
    }
    if (!canDirectMessage(chosen)) {
        // Its thread is open only for a question it asked; text sent to it
        // would be read as a task by an agent that takes none from people.
        return `@${target} can't be messaged — answer its question with the buttons.`;
    }
    return isReachable(chosen) ? null : `@${target} isn't running — start it and try again.`;
}

/** Where a send goes, and why it cannot. */
export interface SendPlan {
    /** The thread it is sent from: the target's, or main's when there is none. */
    thread: string;
    /** The name of the agent it is for: the one a mention names, or the target. */
    name: string;
    /** The id of the agent expected to answer: main's for a command, the one a mention names, else the thread's. */
    answerer: string;
    /** Why it cannot go now, or null. */
    blocked: string | null;
    /** What the bubble shows: without a mention of its own agent. */
    shown: string;
    /** What goes out: a mention of another agent stays on it, since it routes it. */
    routed: string;
}

/**
 * Plan a send of `content` from the conversation with `target` (an id, "" while a
 * named agent has not arrived, and its name). A mention that finds one agent is
 * to that agent; one that finds several goes out as it was typed, and the
 * server answers with the addresses to choose from; one that finds none is
 * text for the thread's agent. The server says which agent a send reached, and
 * the message moves there.
 */
export function planSend(
    content: string,
    target: { id: string; name: string },
    agents: AgentInfo[],
    mainId: string,
): SendPlan {
    const mention = leadingMention(content, agents);
    const one = mention?.found.length === 1 ? mention.found[0] : undefined;
    const thread = target.id || mainId;
    const name = one?.name ?? (target.name || MAIN_AGENT);
    // Say so rather than sending somewhere else. Routing a message the user
    // addressed to a stopped agent onwards to main is what the original
    // complaint was, and a quiet fallback here would reproduce it.
    const addressed = one ?? agents.find(a => a.id === (target.id || (target.name ? "" : mainId)));
    const shown = (mention && one && stripLeadingMention(content, mention.spelled)) || content;
    return {
        thread,
        name,
        answerer: answererOf(content, one?.id ?? thread, mainId),
        blocked: sendBlockedReason(addressed, name),
        shown,
        routed: one && one.id !== thread ? content : shown,
    };
}

/** `content` with its leading mention, if any, made `@address`; else `content` mentioning `address` first. */
export function readdressed(content: string, address: string): string {
    const typed = MENTION.exec(content.trimStart());
    const rest = typed ? content.trimStart().slice(typed[0].length).trimStart() : content;
    return rest ? `@${address} ${rest}` : `@${address}`;
}

/**
 * Drop a leading `@target` once it has been promoted to the routing target, so a
 * message shown in that agent's own thread (and the feed) isn't prefixed with the
 * agent's own name. The transport re-adds the canonical `@name` for routing, so
 * stripping it here is display/feed-only.
 */
export function stripLeadingMention(content: string, target: string): string {
    const escaped = target.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return content.replace(new RegExp(`^@${escaped}\\b\\s*`, "i"), "");
}

/**
 * The id of the agent `msg` is from, or "" for the person's own words and for
 * the gateway's and the system's, which are no agent's reply. An agent's own
 * message names its id; a reply over the socket is in its sender's thread.
 */
export function senderOf(msg: ChatMessage): string {
    if (msg.from === "user" || msg.from === IO_GATEWAY || msg.from === "system") {
        return "";
    }
    return msg.agentId || msg.threadId;
}

/**
 * `msg` filed where it belongs: the gateway's and the system's words name no
 * agent of their own, so they are in the thread of whom the person last spoke
 * to (`last`), or main's before anyone; anything else is as the server filed it.
 */
export function filedWithLastSent(
    msg: ChatMessage,
    last: { name: string; id: string },
    mainId: string,
): ChatMessage {
    if (msg.from !== IO_GATEWAY && msg.from !== "system") {
        return msg;
    }
    return { ...msg, to: last.name, threadId: last.id || mainId || msg.threadId };
}

/**
 * Keep an interface-mediated voice turn in that surface's user-facing thread: the
 * surface agent to open, or null to stay. `current` is the name of the agent open
 * now. The reasoning agent remains internal metadata and never becomes a second persona.
 */
export function voiceThreadTarget(msg: ChatMessage, current: string, agents: AgentInfo[]): AgentInfo | null {
    const surface = msg.surface?.trim();
    if (msg.source !== "voice" || !surface) {
        return null;
    }
    // The thread the server filed the turn in, when that is the surface's.
    const available = agents.find(a => a.id === msg.threadId && a.name === surface);
    if (!available) {
        return null;
    }
    const brain = msg.brain?.trim();
    const currentIsBrain = current === brain || (!brain && (current === "main" || current === "main-actor"));
    if (current !== surface && !currentIsBrain) {
        return null;
    }
    return available;
}

/**
 * The agent to take over from `current` (an id) once it has gone, or null to stay put.
 *
 * Null when the target is still there, and null when nothing is messageable —
 * an empty list means "not back yet", never "yours is gone".
 */
export function replacementTarget(agents: AgentInfo[], current: string): AgentInfo | null {
    const messageable = agents.filter(canDirectMessage);
    if (!messageable.length || messageable.some(a => a.id === current)) {
        return null;
    }
    return messageable.find(a => a.name === MAIN_AGENT) ?? messageable[0]!;
}

/**
 * The id of the agent to open on, honouring a remembered choice when it still stands.
 *
 * The remembered id is a *preference*, never the effective target: it is only
 * consulted when nothing has been chosen yet, and only if it is still an agent
 * that can be direct-messaged. One that no longer exists falls back to the
 * ordinary default rather than becoming a target nothing can deliver to.
 *
 * A stale id is left in storage rather than cleaned up. Validating on every
 * read already makes it inert, so removing it would add a write path on a read
 * for no visible difference.
 *
 * Deliberately not filtered by whether the agent is *running*. A stopped agent
 * stays the target across a reload for the same reason it stays the target
 * without one — stopping is a state the conversation survives, and moving the
 * user off it is the class of bug the target split removed.
 */
export function preferredChatTarget(agents: AgentInfo[], remembered: string | null): string {
    const kept = agents.filter(canDirectMessage).find(a => a.id === remembered);
    return kept?.id ?? defaultChatTarget(agents);
}

/**
 * The id of the conversation to have open, given the one open now (`current`, "" for none).
 *
 * None yet: the preferred one. One the page fell back on, before it has been
 * shown (`settled` false): the remembered one, or main, once that has arrived.
 * Anything else stays as it is.
 */
export function settlingTarget(
    agents: AgentInfo[],
    current: string,
    remembered: string | null,
    settled: boolean,
): string {
    if (!current) {
        return preferredChatTarget(agents, remembered);
    }
    const wanted = remembered ?? agents.find(a => a.name === MAIN_AGENT)?.id;
    if (settled || wanted === undefined || wanted === current) {
        return current;
    }
    // Only ever an upgrade of a fallback that still stands. A target that has
    // gone belongs to dropTargetIfGone, which moves the user and says so;
    // stepping aside here would move them off it in silence.
    const open = agents.find(a => a.id === current);
    if (!open || !canDirectMessage(open)) {
        return current;
    }
    return agents.some(a => a.id === wanted && canDirectMessage(a)) ? wanted : current;
}
