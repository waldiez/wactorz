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
import { looksLikeAgentId } from "../../agents/naming";
import { canDirectMessage } from "./agentState";

/**
 * Pick the default chat target for the current agent set: keep `current` if it's
 * still messageable, else prefer main, else the first human-named agent. Never
 * auto-selects an id-named agent — the backend uses UUID ids (not WIDs), so an
 * agent that never resolved keeps the id as its name and must not silently become
 * the chat target (it leaks into the placeholder). The user can still pick one.
 */
export function pickChatTarget(agents: AgentInfo[], current: string): string {
    const messageable = agents.filter(canDirectMessage);
    if (!messageable.length || messageable.some(a => a.name === current)) {
        return current;
    }
    const main = messageable.find(a => a.name === "main" || a.name === "main-actor");
    const named = messageable
        .filter(a => !looksLikeAgentId(a.name))
        .sort((a, b) => a.name.localeCompare(b.name));
    return main?.name ?? named[0]?.name ?? current;
}

/**
 * A leading `@name` mention overrides the picker target. Falls back to the picker
 * when there's no mention or it names no known agent. Without this a message
 * shows under the picked agent while the @mentioned one actually replies.
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
