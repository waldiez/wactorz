/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Agent profile image resolution.
 *
 * Priority order:
 *   1. Static waldiez WebP avatars (served from /avatars/) — matched by agent name/type.
 *   2. DiceBear "bottts-neutral" SVG — deterministic per agent name, works offline.
 *
 * No external API calls, no API keys required.
 */

import type { AgentInfo } from "../types/agent";

/**
 * Ordered avatar mapping rules. The first rule whose `exact` name, `names`
 * substring, or `types` substring matches wins — order encodes priority.
 */
interface AvatarRule {
    avatar: string;
    exact?: string[];
    names?: string[];
    types?: string[];
}

const AVATAR_RULES: AvatarRule[] = [
    // Orchestrator / main actor
    { avatar: "captain.webp", exact: ["main", "main-actor"], types: ["orchestrator"] },
    // Monitor / supervisor / anomaly-detector
    { avatar: "manager.webp", names: ["monitor", "anomaly"], types: ["monitor"] },
    // Code execution / dynamic / reasoning
    { avatar: "reasoning.webp", names: ["code", "dynamic", "reasoning"], types: ["script"] },
    // Manual / assistant / knowledge / udx
    { avatar: "assistant.webp", names: ["manual", "assistant", "udx", "rag"] },
    // Remote / home-assistant / nautilus
    { avatar: "remote.webp", names: ["home-assistant", "nautilus", "remote"] },
    // IO / user gateway
    { avatar: "user.webp", names: ["io"], types: ["gateway", "io"] },
    // Docs / knowledge
    { avatar: "rag.webp", names: ["docs", "rag"] },
];

/** Maps agent name / type keywords → a static WebP path under /avatars/. */
function staticAvatar(name: string, type?: string): string | null {
    const n = name.toLowerCase();
    const t = (type ?? "").toLowerCase();

    for (const rule of AVATAR_RULES) {
        const hit =
            rule.exact?.includes(n) ||
            rule.names?.some(k => n.includes(k)) ||
            rule.types?.some(k => t.includes(k));
        if (hit) {
            return `./avatars/${rule.avatar}`;
        }
    }

    return null; // fall through to DiceBear
}

function dicebearUrl(name: string): string {
    return (
        `https://api.dicebear.com/9.x/bottts-neutral/svg` +
        `?seed=${encodeURIComponent(name)}&backgroundColor=0d1117,111827&radius=50`
    );
}

export class AgentImageGen {
    private cache = new Map<string, string>();

    /**
     * Resolve (and cache, per agent id) the avatar URL: a static WebP when a
     * rule matches the name/type, otherwise a deterministic DiceBear SVG.
     */
    get(agent: Pick<AgentInfo, "id" | "name"> & { agentType?: string }): string {
        if (!this.cache.has(agent.id)) {
            const url = staticAvatar(agent.name, agent.agentType) ?? dicebearUrl(agent.name);
            this.cache.set(agent.id, url);
        }
        return this.cache.get(agent.id)!;
    }
}

/** Singleton — import and call `.get(agent)` anywhere. */
export const agentImageGen = new AgentImageGen();
