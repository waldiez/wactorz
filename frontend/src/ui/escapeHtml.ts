/**
 * HTML-escape untrusted text before interpolating it into an `innerHTML`
 * template. Agent names / tasks come from spawned agents (LLM-authored) over
 * MQTT/WS, so they must never be trusted in a markup sink.
 */
const ENTITIES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ENTITIES[c] ?? c);
}
