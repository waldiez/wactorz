/**
 * Chat panel — per-agent threaded conversation view.
 *
 * Each agent gets an isolated message thread.  Switching agents fades the old
 * thread out and cross-fades the new one in.  Agent messages include a small
 * avatar resolved from waldiez static WebP files (or DiceBear fallback).
 *
 * Events fired on `document`:
 *   "panel-opened"          → { agent }        when panel slides in
 *   "panel-closed"          → (none)
 *   "agent-unread"          → { name, count }  when a background thread gets a message
 *   "agent-unread-cleared"  → { name }         when user opens that agent's thread
 */

import type { AgentInfo, ChatMessage } from "../types/agent";
import { agentImageGen } from "../io/AgentImageGen";

/** DiceBear robot URL — instant, no API key needed. */
function dicebearFor(name: string): string {
  return (
    `https://api.dicebear.com/9.x/bottts-neutral/svg` +
    `?seed=${encodeURIComponent(name)}&backgroundColor=0d1117,111827&radius=50`
  );
}

export class ChatPanel {
  private panel: HTMLElement;
  private agentNameEl: HTMLElement;
  private agentStatusEl: HTMLElement;
  private avatarEl: HTMLImageElement | null;
  private closeBtn: HTMLButtonElement;
  private messagesEl: HTMLElement;

  private sidebarListEl: HTMLElement;
  private sidebarSearchEl: HTMLInputElement;
  private agentList: AgentInfo[] = [];
  private sidebarFilter: string = "";

  private selectedAgent: AgentInfo | null = null;
  private activeAgentName: string | null = null;

  /** Per-agent conversation history.  Key = agent name. */
  private threads: Map<string, ChatMessage[]> = new Map();

  /** Active typing bubbles keyed by agent name. */
  private typingBubbles: Map<string, HTMLElement> = new Map();
  private typingTimeouts: Map<string, ReturnType<typeof setTimeout>> =
    new Map();

  /** Streaming state — one active stream at a time. */
  private _streamRow: HTMLElement | null = null;
  private _streamBody: HTMLElement | null = null;
  private _streamFrom: string | null = null;
  private _streamText: string = "";
  private _lastStreamedText: string = "";

  constructor() {
    this.panel = document.getElementById("chat-panel")!;
    this.agentNameEl = document.getElementById("panel-agent-name")!;
    this.agentStatusEl = document.getElementById("panel-agent-status")!;
    this.avatarEl = document.getElementById(
      "panel-agent-avatar",
    ) as HTMLImageElement | null;
    this.closeBtn = document.getElementById("panel-close") as HTMLButtonElement;
    this.messagesEl = document.getElementById("chat-messages")!;
    this.sidebarListEl = document.getElementById("chat-agent-list")!;
    this.sidebarSearchEl = document.getElementById(
      "chat-sidebar-search",
    ) as HTMLInputElement;

    this.sidebarSearchEl.addEventListener("input", () => {
      this.sidebarFilter = this.sidebarSearchEl.value.toLowerCase();
      this.renderSidebar();
    });

    this.closeBtn.addEventListener("click", () => this.close());
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") this.close();
    });

    // Swipe-right to close (mobile)
    let _touchX = 0;
    this.panel.addEventListener(
      "touchstart",
      (e) => {
        _touchX = e.touches[0]?.clientX ?? 0;
      },
      { passive: true },
    );
    this.panel.addEventListener(
      "touchend",
      (e) => {
        if ((e.changedTouches[0]?.clientX ?? 0) - _touchX > 60) this.close();
      },
      { passive: true },
    );

    document.addEventListener("agent-selected", (e) => {
      this.open((e as CustomEvent<{ agent: AgentInfo }>).detail.agent);
    });
  }

  // ── Public ─────────────────────────────────────────────────────────────────

  /** Open or switch to the given agent's thread. */
  open(agent: AgentInfo): void {
    const prev = this.activeAgentName;
    this.selectedAgent = agent;
    this.activeAgentName = agent.name;

    // Header update
    this.agentNameEl.textContent = agent.name;
    this.agentStatusEl.textContent =
      typeof agent.state === "object" ? "failed" : (agent.state ?? "active");
    if (this.avatarEl) {
      this.avatarEl.src = agentImageGen.get(agent);
      this.avatarEl.alt = agent.name;
      this.avatarEl.style.opacity = "1";
    }

    // Clear unread notification for this agent
    document.dispatchEvent(
      new CustomEvent("agent-unread-cleared", { detail: { name: agent.name } }),
    );

    // Update sidebar active state
    this.sidebarListEl
      .querySelectorAll<HTMLElement>(".af-chat-agent-row")
      .forEach((row) => {
        row.classList.toggle("active", row.dataset["name"] === agent.name);
      });

    const alreadyOpen = this.panel.classList.contains("open");
    if (!alreadyOpen) {
      this.renderThread(agent.name, false);
      this.panel.classList.add("open");
    } else if (prev !== agent.name) {
      this.renderThread(agent.name, true); // animated cross-fade
    }

    document.dispatchEvent(
      new CustomEvent<{ agent: AgentInfo }>("panel-opened", {
        detail: { agent },
      }),
    );
  }

  /**
   * Ensure the panel is visible.  If already open, leave it untouched.
   * If closed, open with a generic header derived from `hint`.
   */
  ensureOpen(hint = "Chat"): void {
    if (this.panel.classList.contains("open")) return;
    this.agentNameEl.textContent = hint;
    this.agentStatusEl.textContent = "active";
    if (this.avatarEl) {
      this.avatarEl.src = dicebearFor(hint);
      this.avatarEl.alt = hint;
      this.avatarEl.style.opacity = "1";
    }
    if (!this.activeAgentName) this.activeAgentName = hint;
    this.renderThread(hint, false);
    this.panel.classList.add("open");
  }

  close(): void {
    this.panel.classList.remove("open");
    this.selectedAgent = null;
    document.dispatchEvent(new CustomEvent("panel-closed"));
  }

  /** Route and display a chat message in the correct thread. */
  appendMessage(msg: ChatMessage): void {
    // User / system messages — and io-gateway proxy replies — belong to the
    // active thread. io-gateway is a transparent routing layer, not a real agent.
    const key =
      msg.from === "user" || msg.from === "system" || msg.from === "io-gateway"
        ? (this.activeAgentName ?? "main-actor")
        : msg.from;

    if (!this.threads.has(key)) this.threads.set(key, []);
    this.threads.get(key)!.push(msg);

    if (key === this.activeAgentName) {
      this.renderMessageEl(msg);
      this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
    } else {
      // Background thread → fire unread notification
      const agentMsgCount = (this.threads.get(key) ?? []).filter(
        (m) => m.from !== "user" && m.from !== "system",
      ).length;
      document.dispatchEvent(
        new CustomEvent("agent-unread", {
          detail: { name: key, count: agentMsgCount },
        }),
      );
    }
  }

  updateAgentStatus(agentId: string, state: string): void {
    if (this.selectedAgent?.id === agentId) {
      this.agentStatusEl.textContent = state;
    }
  }

  updateAgentList(agents: AgentInfo[]): void {
    this.agentList = agents;
    this.renderSidebar();
  }

  private renderSidebar(): void {
    const filtered = this.sidebarFilter
      ? this.agentList.filter((a) =>
          a.name.toLowerCase().includes(this.sidebarFilter),
        )
      : this.agentList;

    const sorted = [...filtered].sort((a, b) => {
      if (a.name === "main-actor") return -1;
      if (b.name === "main-actor") return 1;
      return a.name.localeCompare(b.name);
    });

    // Collect existing rows by name for diffing
    const existing = new Map<string, HTMLElement>();
    this.sidebarListEl
      .querySelectorAll<HTMLElement>(".af-chat-agent-row")
      .forEach((r) => {
        if (r.dataset["name"]) existing.set(r.dataset["name"], r);
      });

    const keep = new Set(sorted.map((a) => a.name));
    // Remove rows for agents no longer in the list
    existing.forEach((row, name) => {
      if (!keep.has(name)) row.remove();
    });

    // Upsert rows in sorted order without touching unchanged rows
    sorted.forEach((agent, idx) => {
      const dotColor =
        typeof agent.state === "object"
          ? "#f87171"
          : agent.state === "running"
            ? "#34d399"
            : agent.state === "paused"
              ? "#fbbf24"
              : agent.state === "stopped"
                ? "#4b5563"
                : "#60a5fa";
      const isActive = agent.name === this.activeAgentName;

      let row = existing.get(agent.name);
      if (!row) {
        row = document.createElement("button");
        row.className = "af-chat-agent-row";
        row.dataset["name"] = agent.name;
        row.title = agent.name;
        row.innerHTML = `
          <span class="af-chat-agent-dot"></span>
          <span class="af-chat-agent-name">${agent.name}</span>
        `;
        // Use delegated name lookup so the closure always reflects latest state
        row.addEventListener("click", () => {
          const a = this.agentList.find((x) => x.name === agent.name);
          if (!a) return;
          document.dispatchEvent(
            new CustomEvent<{ agent: AgentInfo }>("agent-selected", {
              detail: { agent: a },
            }),
          );
        });
      }

      // Patch only what may have changed
      row.classList.toggle("active", isActive);
      const dot = row.querySelector<HTMLElement>(".af-chat-agent-dot");
      if (dot && dot.style.background !== dotColor)
        dot.style.background = dotColor;

      // Ensure correct position without re-inserting if already there
      const sibling = this.sidebarListEl.children[idx];
      if (sibling !== row)
        this.sidebarListEl.insertBefore(row, sibling ?? null);
    });
  }

  get activeAgent(): AgentInfo | null {
    return this.selectedAgent;
  }
  /** The full text of the most recently finalized stream (cleared after read). */
  get lastStreamedText(): string {
    const t = this._lastStreamedText;
    this._lastStreamedText = "";
    return t;
  }

  // ── Streaming ───────────────────────────────────────────────────────────────

  /**
   * Append a chunk to the in-progress streaming bubble.
   * Creates the bubble on the first chunk.
   */
  streamChunk(chunk: string, from: string): void {
    if (!this._streamRow) {
      // First chunk — create the bubble
      this._streamFrom = from;
      this._streamText = "";

      const wrapper = document.createElement("div");
      wrapper.className = "af-chat-msg af-chat-msg-agent";

      const fromEl = document.createElement("div");
      fromEl.className = "af-chat-msg-from";
      fromEl.textContent = from;

      const bubble = document.createElement("div");
      bubble.className = "af-chat-msg-bubble";

      wrapper.appendChild(fromEl);
      wrapper.appendChild(bubble);

      // Attach to the active thread in the DOM
      if (this.panel.classList.contains("open")) {
        this.messagesEl.appendChild(wrapper);
      }

      this._streamRow = wrapper;
      this._streamBody = bubble;
    }

    this._streamText += chunk;
    if (this._streamBody) {
      // Show plain text while streaming (fast, no XSS risk)
      this._streamBody.textContent = this._streamText;
    }
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
  }

  /** Finalize the streaming bubble: render markdown, store in thread history. */
  finalizeStream(): void {
    if (!this._streamBody || !this._streamFrom || !this._streamRow) return;

    // Render markdown on the completed text
    this._streamBody.innerHTML = renderMarkdown(this._streamText);
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;

    // Store in thread history under the actual sender, not the active panel
    const key = this._streamFrom ?? this.activeAgentName ?? "main-actor";
    const msg: ChatMessage = {
      id: `stream-${Date.now()}`,
      from: this._streamFrom,
      to: "user",
      content: this._streamText,
      timestampMs: Date.now(),
    };
    if (!this.threads.has(key)) this.threads.set(key, []);
    this.threads.get(key)!.push(msg);

    // Reset streaming state
    this._lastStreamedText = this._streamText;
    this._streamRow = null;
    this._streamBody = null;
    this._streamFrom = null;
    this._streamText = "";
  }

  // ── Typing indicator ────────────────────────────────────────────────────────

  /** Show a three-dot typing bubble for the given agent. */
  showTyping(agentId: string, agentName?: string): void {
    if (this.typingBubbles.has(agentId)) return;

    const el = document.createElement("div");
    el.className = "af-chat-msg af-chat-msg-agent";
    el.dataset["typingFor"] = agentId;

    const fromEl = document.createElement("div");
    fromEl.className = "af-chat-msg-from";
    fromEl.textContent = agentName ?? agentId;
    el.appendChild(fromEl);

    const dots = document.createElement("div");
    dots.className = "af-chat-typing";
    for (let i = 0; i < 3; i++) {
      dots.appendChild(document.createElement("span"));
    }
    el.appendChild(dots);

    // Only attach if this agent's thread is currently active
    if (agentId === this.activeAgentName || !this.activeAgentName) {
      this.messagesEl.appendChild(el);
      this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
    }

    this.typingBubbles.set(agentId, el);

    const timer = setTimeout(() => {
      this.hideTyping(agentId);
      this.appendMessage({
        id: `timeout-${agentId}`,
        from: "system",
        to: "user",
        content: `⏳ No response from **${agentName ?? agentId}** — the agent may still be processing.`,
        timestampMs: Date.now(),
      });
    }, 45_000);
    this.typingTimeouts.set(agentId, timer);
  }

  /** Remove the typing bubble for the given agent. */
  hideTyping(agentId: string): void {
    const el = this.typingBubbles.get(agentId);
    if (el) {
      el.remove();
      this.typingBubbles.delete(agentId);
    }
    const t = this.typingTimeouts.get(agentId);
    if (t !== undefined) {
      clearTimeout(t);
      this.typingTimeouts.delete(agentId);
    }
  }

  // ── Private ─────────────────────────────────────────────────────────────────

  private renderThread(agentName: string, animate: boolean): void {
    const paint = () => {
      this.messagesEl.innerHTML = "";
      for (const msg of this.threads.get(agentName) ?? []) {
        this.renderMessageEl(msg);
      }
      // Re-attach typing bubble if this agent is currently typing
      const typing = this.typingBubbles.get(agentName);
      if (typing) this.messagesEl.appendChild(typing);
      this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
    };

    if (animate) {
      this.messagesEl.style.opacity = "0";
      this.messagesEl.style.transition = "opacity 0.14s ease";
      setTimeout(() => {
        paint();
        this.messagesEl.style.opacity = "1";
      }, 140);
    } else {
      paint();
    }
  }

  private renderMessageEl(msg: ChatMessage): void {
    const isUser = msg.from === "user";
    const isSystem = msg.from === "system";

    if (isUser || isSystem) {
      const el = document.createElement("div");
      el.className = isSystem
        ? "af-chat-msg af-chat-msg-system"
        : "af-chat-msg af-chat-msg-user";

      const from = document.createElement("div");
      from.className = "af-chat-msg-from";
      from.textContent = isSystem
        ? "system"
        : `you · ${new Date(msg.timestampMs).toLocaleTimeString()}`;

      const bubble = document.createElement("div");
      bubble.className = "af-chat-msg-bubble";
      bubble.innerHTML = renderMarkdown(msg.content);

      el.appendChild(from);
      el.appendChild(bubble);
      this.messagesEl.appendChild(el);
    } else {
      // Agent message
      const wrapper = document.createElement("div");
      wrapper.className = "af-chat-msg af-chat-msg-agent";

      const from = document.createElement("div");
      from.className = "af-chat-msg-from";
      from.textContent = msg.from;

      const bubble = document.createElement("div");
      bubble.className = "af-chat-msg-bubble";
      bubble.innerHTML = renderMarkdown(msg.content);

      const time = document.createElement("div");
      time.className = "af-chat-msg-time";
      time.textContent = new Date(msg.timestampMs).toLocaleTimeString();

      wrapper.appendChild(from);
      wrapper.appendChild(bubble);
      wrapper.appendChild(time);
      this.messagesEl.appendChild(wrapper);
    }
  }
}

// ── Minimal markdown renderer (XSS-safe, no external deps) ───────────────────

function renderMarkdown(raw: string): string {
  const tmp = document.createElement("div");
  tmp.textContent = raw;
  let s = tmp.innerHTML;

  // Fenced code blocks
  s = s.replace(
    /```[\s\S]*?```/g,
    (m) => `<pre><code>${m.slice(3, -3).trim()}</code></pre>`,
  );
  // Inline code
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  // Bold
  s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__(.+?)__/g, "<strong>$1</strong>");
  // Italic
  s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  s = s.replace(/_([^_]+)_/g, "<em>$1</em>");
  // Line breaks
  s = s.replace(/\n/g, "<br>");

  return s;
}
