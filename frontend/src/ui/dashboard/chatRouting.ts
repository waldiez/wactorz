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
import { canDirectMessage } from "./agentState";

/**
 * Pick the default chat target for the current agent set.
 *
 * Until the user has explicitly picked a target (`userPicked` false), prefer main
 * whenever it is present — otherwise an agent that registers before main on
 * startup (added first, and often alphabetically first, e.g. `catalog`) gets
 * auto-selected and then sticks. Once the user has picked, keep `current` while
 * it's still messageable, else fall back to main, else the first human-named
 * agent. Never auto-selects an id-named agent — the backend uses UUID ids (not
 * WIDs), so an agent that never resolved keeps the id as its name and must not
 * silently become the chat target.
 */
export function pickChatTarget(agents: AgentInfo[], current: string, userPicked = false): string {
    const messageable = agents.filter(canDirectMessage);
    if (!messageable.length) {
        return current;
    }
    const main = messageable.find(a => a.name === MAIN_AGENT);
    if (!userPicked && main) {
        return main.name;
    }
    if (messageable.some(a => a.name === current)) {
        return current;
    }
    const named = messageable
        .filter(a => !looksLikeAgentId(a.name))
        .sort((a, b) => a.name.localeCompare(b.name));
    return main?.name ?? named[0]?.name ?? current;
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
 * The agent to move to when a reset destroyed the current one, or null to stay.
 *
 * Null when the target survived, and null when nothing is messageable — during
 * the churn an empty list means "not back yet", never "yours is gone".
 */
export function replacementAfterReset(agents: AgentInfo[], current: string): string | null {
    const messageable = agents.filter(canDirectMessage);
    if (!messageable.length || messageable.some(a => a.name === current)) {
        return null;
    }
    return messageable.find(a => a.name === MAIN_AGENT)?.name ?? messageable[0]!.name;
}
