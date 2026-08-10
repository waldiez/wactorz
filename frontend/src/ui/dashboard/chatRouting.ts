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
import type { AgentInfo } from "../../types/agent";
import { looksLikeAgentId, MAIN_AGENT } from "../../agents/naming";
import { canDirectMessage, isReachable } from "./agentState";

/**
 * The agent to open on when nothing has been chosen yet, or "" if there is none.
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
    return main?.name ?? named[0]?.name ?? "";
}

/**
 * A leading `@name` mention overrides the picker target. Falls back to the picker
 * when there's no mention or it names no known agent. Without this a message
 * shows under the picked agent while the mentioned one actually replies.
 */
export function resolveSendTarget(content: string, agentNames: string[], fallback: string): string {
    const mention = /^@([A-Za-z0-9_-]+)/.exec(content.trimStart())?.[1];
    if (mention) {
        const match = agentNames.find(n => n.toLowerCase() === mention.toLowerCase());
        if (match) {
            return match;
        }
    }
    return fallback;
}

/**
 * Why a message to `target` cannot be sent right now, or null if it can.
 *
 * The send-time half of the choice/reachability split: `chatTarget` holds who the
 * user chose and keeps holding it across a stop, so this is what has to notice
 * that the choice cannot answer. Returning a reason rather than a boolean keeps
 * the wording next to the rule that produced it.
 */
export function sendBlockedReason(agents: AgentInfo[], target: string): string | null {
    const chosen = agents.find(a => a.name === target);
    if (!chosen) {
        // Only a race now that a gone target is moved off, but it is the branch
        // where "start it" would be wrong: there is nothing left to start.
        return `@${target} is no longer available.`;
    }
    return isReachable(chosen) ? null : `@${target} isn't running — start it and try again.`;
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
 * The agent to take over from `current` once it has gone, or null to stay put.
 *
 * Null when the target is still there, and null when nothing is messageable —
 * an empty list means "not back yet", never "yours is gone".
 */
export function replacementTarget(agents: AgentInfo[], current: string): string | null {
    const messageable = agents.filter(canDirectMessage);
    if (!messageable.length || messageable.some(a => a.name === current)) {
        return null;
    }
    return messageable.find(a => a.name === MAIN_AGENT)?.name ?? messageable[0]!.name;
}
