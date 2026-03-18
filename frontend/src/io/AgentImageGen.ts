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

// ── Static waldiez avatar mapping ─────────────────────────────────────────────

/** Maps agent name / type keywords → a static WebP path under /avatars/. */
function staticAvatar(name: string, type?: string): string | null {
  const n = name.toLowerCase();
  const t = (type ?? "").toLowerCase();

  // Orchestrator / main actor
  if (n === "main" || n === "main-actor" || t.includes("orchestrator"))
    return "/avatars/captain.webp";

  // Monitor / supervisor / anomaly-detector
  if (n.includes("monitor") || n.includes("anomaly") || t.includes("monitor"))
    return "/avatars/manager.webp";

  // Code execution / dynamic / reasoning
  if (n.includes("code") || n.includes("dynamic") || n.includes("reasoning") || t.includes("script"))
    return "/avatars/reasoning.webp";

  // Manual / assistant / knowledge / udx
  if (n.includes("manual") || n.includes("assistant") || n.includes("udx") || n.includes("rag"))
    return "/avatars/assistant.webp";

  // Remote / home-assistant / nautilus
  if (n.includes("home-assistant") || n.includes("nautilus") || n.includes("remote"))
    return "/avatars/remote.webp";

  // IO / user gateway
  if (n.includes("io") || t.includes("gateway") || t.includes("io"))
    return "/avatars/user.webp";

  // Docs / knowledge / fuseki
  if (n.includes("fuseki") || n.includes("docs") || n.includes("rag"))
    return "/avatars/rag.webp";

  return null; // fall through to DiceBear
}

// ── DiceBear fallback ─────────────────────────────────────────────────────────

function dicebearUrl(name: string): string {
  return (
    `https://api.dicebear.com/9.x/bottts-neutral/svg` +
    `?seed=${encodeURIComponent(name)}&backgroundColor=0d1117,111827&radius=50`
  );
}

// ── Service ───────────────────────────────────────────────────────────────────

export class AgentImageGen {
  private cache = new Map<string, string>();

  get(agent: Pick<AgentInfo, "id" | "name"> & { agentType?: string }): string {
    if (!this.cache.has(agent.id)) {
      const url =
        staticAvatar(agent.name, agent.agentType) ??
        dicebearUrl(agent.name);
      this.cache.set(agent.id, url);
    }
    return this.cache.get(agent.id)!;
  }
}

/** Singleton — import and call `.get(agent)` anywhere. */
export const agentImageGen = new AgentImageGen();
