/**
 * Agent profile image generation.
 *
 * Primary:  Google Gemini image model (gemini-3.1-flash-image-preview — "Nano Banana")
 *           Activated when VITE_GOOGLE_AI_KEY is set in the environment.
 *
 * Fallback: DiceBear "bottts-neutral" SVG avatars — free, deterministic per
 *           agent name, works offline with zero config.
 *
 * Images are cached in memory.  When an AI image finishes generating it is
 * swapped in by firing a `"agent-image-ready"` CustomEvent on `document`.
 */

import type { AgentInfo } from "../types/agent";

const GOOGLE_AI_KEY = import.meta.env["VITE_GOOGLE_AI_KEY"] as string | undefined;
const GEMINI_IMG_MODEL = "gemini-3.1-flash-image-preview";
const GEMINI_IMG_URL   =
  `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_IMG_MODEL}:generateContent`;

// ── Fallback avatars ──────────────────────────────────────────────────────────

function dicebearUrl(name: string): string {
  // bottts-neutral gives clean robot heads; seed is the agent name for stability
  return (
    `https://api.dicebear.com/9.x/bottts-neutral/svg` +
    `?seed=${encodeURIComponent(name)}&backgroundColor=0d1117,111827&radius=50`
  );
}

// ── Prompt selection ──────────────────────────────────────────────────────────

function imagePrompt(name: string, type?: string): string {
  const t = (type ?? "").toLowerCase();
  const n = name.toLowerCase();

  if (n === "main-actor" || t.includes("orchestrator"))
    return "Wise AI director robot, warm amber-gold halo, leadership aura, flat digital art portrait, square, no text";
  if (t.includes("monitor") || n.includes("monitor"))
    return "Vigilant sentinel robot with glowing amber eyes, dark security aesthetic, flat digital portrait, square, no text";
  if (t.includes("guardian") || n.includes("qa"))
    return "Quality-inspector robot holding magnifying glass, calm teal tones, flat digital portrait, square, no text";
  if (t.includes("gateway") || n.includes("io"))
    return "Communication nexus robot, signal-wave motifs, electric sky-blue, flat digital portrait, square, no text";
  if (t.includes("dynamic") || t.includes("script"))
    return "Creative scripting robot, colourful code streams swirling around, energetic digital portrait, square, no text";
  if (n.includes("math"))
    return "Mathematician robot, floating equations and pi symbols, monochrome with cyan highlights, flat art, square, no text";
  if (n.includes("weather"))
    return "Meteorologist robot, cloud and sunshine motifs, soft pastel blue gradient, flat digital portrait, square, no text";
  if (n.includes("data") || n.includes("fetch"))
    return "Data retrieval robot, flowing data-stream ribbons, teal and violet palette, flat digital portrait, square, no text";
  if (n.includes("ml") || n.includes("classifier") || n.includes("model"))
    return "Machine-learning robot studying a glowing neural network, flat art, square portrait, no text";

  // Generic fallback uses the agent name as context
  const readable = name.replace(/-/g, " ");
  return `Professional AI agent robot (${readable}), clean minimal tech aesthetic, flat digital art, square portrait, no text`;
}

// ── Response shape from Gemini generateContent ────────────────────────────────

interface GeminiPart {
  text?: string;
  inlineData?: { mimeType: string; data: string };
}
interface GeminiResponse {
  candidates?: Array<{ content: { parts: GeminiPart[] } }>;
}

// ── Service ───────────────────────────────────────────────────────────────────

class AgentImageGen {
  /** agentId → URL (dicebear SVG or data:image/… base64) */
  private cache   = new Map<string, string>();
  private pending = new Set<string>();

  /**
   * Return the current best image URL for an agent.
   * Immediately returns a DiceBear URL, then fires an async Gemini call
   * if an API key is configured. When Gemini responds, it dispatches
   * `"agent-image-ready"` so UIs can swap the `<img src>`.
   */
  get(agent: Pick<AgentInfo, "id" | "name"> & { agentType?: string }): string {
    if (!this.cache.has(agent.id)) {
      this.cache.set(agent.id, dicebearUrl(agent.name));
      if (GOOGLE_AI_KEY && !this.pending.has(agent.id)) {
        void this.generateAsync(agent);
      }
    }
    return this.cache.get(agent.id)!;
  }

  private async generateAsync(
    agent: Pick<AgentInfo, "id" | "name"> & { agentType?: string },
  ): Promise<void> {
    this.pending.add(agent.id);
    try {
      const prompt = imagePrompt(agent.name, agent.agentType);
      const res = await fetch(`${GEMINI_IMG_URL}?key=${GOOGLE_AI_KEY}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          contents: [{ parts: [{ text: prompt }] }],
          generationConfig: { responseModalities: ["IMAGE"] },
        }),
      });

      if (!res.ok) {
        console.warn(`[AgentImageGen] HTTP ${res.status} for "${agent.name}"`);
        return;
      }

      const data = (await res.json()) as GeminiResponse;
      for (const part of data.candidates?.[0]?.content?.parts ?? []) {
        if (part.inlineData?.data) {
          const url = `data:${part.inlineData.mimeType};base64,${part.inlineData.data}`;
          this.cache.set(agent.id, url);
          document.dispatchEvent(
            new CustomEvent<{ id: string; url: string }>("agent-image-ready", {
              detail: { id: agent.id, url },
            }),
          );
          break;
        }
      }
    } catch (err) {
      console.warn(`[AgentImageGen] failed for "${agent.name}":`, err);
    } finally {
      this.pending.delete(agent.id);
    }
  }
}

/** Singleton — import and call `.get(agent)` anywhere. */
export const agentImageGen = new AgentImageGen();
