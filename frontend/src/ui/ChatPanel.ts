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
import { escapeHtml } from "./escapeHtml";

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

  private _apiBase = "";
  private _historyFetched = new Set<string>();

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

    // Back button (mobile): show agent list without closing the panel
    const backBtn = document.getElementById("panel-back");
    if (backBtn) {
      backBtn.addEventListener("click", () => {
        this.panel.classList.remove("agent-selected");
        this.activeAgentName = null;
        this.selectedAgent = null;
      });
    }

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

    // Fetch backend history the first time this agent's thread is opened
    if (!this._historyFetched.has(agent.name)) {
      this._historyFetched.add(agent.name);
      this._loadHistory(agent.name);
    }

    const alreadyOpen = this.panel.classList.contains("open");
    if (!alreadyOpen) {
      this.renderThread(agent.name, false);
      this.panel.classList.add("open");
    } else if (prev !== agent.name) {
      this.renderThread(agent.name, true); // animated cross-fade
    }
    this.panel.classList.add("agent-selected");

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
    this.panel.classList.remove("open", "agent-selected");
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
      const isDisabled =
        agent.protected === true &&
        !["main", "main-actor", "home-assistant-agent", "catalog"].includes(
          agent.name,
        );

      let row = existing.get(agent.name);
      if (!row) {
        row = document.createElement("button");
        row.dataset["name"] = agent.name;
        row.innerHTML = `
          <span class="af-chat-agent-dot"></span>
          <span class="af-chat-agent-name">${escapeHtml(agent.name)}</span>
          <span class="af-chat-agent-lock" aria-hidden="true"></span>
        `;
        // Use delegated name lookup so the closure always reflects latest state
        row.addEventListener("click", () => {
          const a = this.agentList.find((x) => x.name === agent.name);
          if (
            !a ||
            (a.protected === true &&
              ![
                "main",
                "main-actor",
                "home-assistant-agent",
                "catalog",
              ].includes(a.name))
          )
            return;
          document.dispatchEvent(
            new CustomEvent<{ agent: AgentInfo }>("agent-selected", {
              detail: { agent: a },
            }),
          );
        });
      }

      // Patch only what may have changed
      const cls = ["af-chat-agent-row"];
      if (isActive) cls.push("active");
      if (isDisabled) cls.push("protected-agent");
      row.className = cls.join(" ");
      (row as HTMLButtonElement).disabled = isDisabled;
      row.title = isDisabled
        ? `${agent.name} — system agent, not directly reachable`
        : agent.name;
      const dot = row.querySelector<HTMLElement>(".af-chat-agent-dot");
      if (dot && dot.style.background !== dotColor)
        dot.style.background = dotColor;
      const lock = row.querySelector<HTMLElement>(".af-chat-agent-lock");
      if (lock) lock.textContent = isDisabled ? "🔒" : "";

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
      // First chunk — create the bubble with avatar header
      this._streamFrom = from;
      this._streamText = "";

      const wrapper = document.createElement("div");
      wrapper.className = "af-chat-msg af-chat-msg-agent";

      const header = document.createElement("div");
      header.className = "af-chat-msg-header";

      const avatar = document.createElement("img");
      avatar.className = "af-chat-msg-avatar";
      avatar.src = dicebearFor(from);
      avatar.alt = from;
      avatar.loading = "lazy";

      const fromEl = document.createElement("span");
      fromEl.className = "af-chat-msg-from";
      fromEl.textContent = from;

      header.appendChild(avatar);
      header.appendChild(fromEl);

      const body = document.createElement("div");
      body.className = "af-chat-msg-body";

      const bubble = document.createElement("div");
      bubble.className = "af-chat-msg-bubble";

      body.appendChild(bubble);
      wrapper.appendChild(header);
      wrapper.appendChild(body);

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

    // Add copy button + timestamp now that content is final
    const body = this._streamBody.parentElement;
    if (body) {
      body.appendChild(this._makeCopyBtn(this._streamText));
    }
    const header = this._streamRow.querySelector<HTMLElement>(".af-chat-msg-header");
    if (header) {
      const time = document.createElement("span");
      time.className = "af-chat-msg-time";
      time.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      header.appendChild(time);
    }

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

  /** Set the API base URL so history can be fetched from the backend. */
  setApiBase(base: string): void {
    this._apiBase = base;
  }

  // ── Private ─────────────────────────────────────────────────────────────────

  private _loadHistory(agentName: string): void {
    fetch(`${this._apiBase}/api/actors/${encodeURIComponent(agentName)}/history`)
      .then((r) => (r.ok ? r.json() : Promise.resolve([])))
      .then((msgs: Array<{ role: string; content: string; ts?: number }>) => {
        if (!Array.isArray(msgs) || msgs.length === 0) return;
        const firstTs = msgs[0]?.ts ? Math.round(msgs[0].ts * 1000) : Date.now();
        const histThread: ChatMessage[] = [
          {
            id: `history-sep-${agentName}`,
            from: "system",
            to: agentName,
            content: "─── restored history ───",
            timestampMs: firstTs,
          },
          ...msgs.map((m, i) => ({
            id: `hist-${m.ts ?? i}`,
            from: m.role === "user" ? "user" : agentName,
            to: m.role === "user" ? agentName : "user",
            content: m.content,
            timestampMs: m.ts ? Math.round(m.ts * 1000) : firstTs + i,
          })),
        ];
        const live = this.threads.get(agentName) ?? [];
        this.threads.set(agentName, [...histThread, ...live]);
        if (this.activeAgentName === agentName) {
          this.renderThread(agentName, false);
        }
      })
      .catch(() => {});
  }

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

  private _makeCopyBtn(text: string): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.className = "af-chat-copy-btn";
    btn.title = "Copy message";
    btn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
    btn.addEventListener("click", () => {
      navigator.clipboard.writeText(text).then(() => {
        btn.textContent = "✓";
        btn.style.color = "#34d399";
        setTimeout(() => {
          btn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
          btn.style.color = "";
        }, 2000);
      }).catch(() => {});
    });
    return btn;
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
        : `you · ${new Date(msg.timestampMs).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;

      const bubble = document.createElement("div");
      bubble.className = "af-chat-msg-bubble";
      bubble.innerHTML = renderMarkdown(msg.content);

      el.appendChild(from);
      el.appendChild(bubble);
      this.messagesEl.appendChild(el);
    } else {
      // Agent message with avatar, copy button
      const wrapper = document.createElement("div");
      wrapper.className = "af-chat-msg af-chat-msg-agent";

      const header = document.createElement("div");
      header.className = "af-chat-msg-header";

      const avatar = document.createElement("img");
      avatar.className = "af-chat-msg-avatar";
      avatar.src = dicebearFor(msg.from);
      avatar.alt = msg.from;
      avatar.loading = "lazy";

      const from = document.createElement("span");
      from.className = "af-chat-msg-from";
      from.textContent = msg.from;

      const time = document.createElement("span");
      time.className = "af-chat-msg-time";
      time.textContent = new Date(msg.timestampMs).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

      header.appendChild(avatar);
      header.appendChild(from);
      header.appendChild(time);

      const body = document.createElement("div");
      body.className = "af-chat-msg-body";

      const bubble = document.createElement("div");
      bubble.className = "af-chat-msg-bubble";
      bubble.innerHTML = renderMarkdown(msg.content);

      body.appendChild(bubble);
      body.appendChild(this._makeCopyBtn(msg.content));

      wrapper.appendChild(header);
      wrapper.appendChild(body);
      this.messagesEl.appendChild(wrapper);
    }
  }
}

// ── Markdown renderer (XSS-safe, no external deps) ───────────────────────────

function renderMarkdown(raw: string): string {
  // HTML-escape via textContent trick
  const tmp = document.createElement("div");
  tmp.textContent = raw;
  let s = tmp.innerHTML;

  // Protect fenced code blocks from inline processing
  const blocks: string[] = [];
  s = s.replace(/```[\s\S]*?```/g, (m) => {
    const inner = m.slice(3, -3).replace(/^\w+\n/, ""); // strip optional lang tag
    blocks.push(`<pre><code>${inner}</code></pre>`);
    return `\x02${blocks.length - 1}\x03`;
  });

  // Inline code
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");

  // Line-by-line: headings and lists
  const lines = s.split("\n");
  let inUl = false;
  let inOl = false;
  const out: string[] = [];
  for (const line of lines) {
    const mH = line.match(/^(#{1,3}) (.+)/);
    const mUl = !mH && line.match(/^[*\-] (.+)/);
    const mOl = !mH && !mUl && line.match(/^\d+\. (.+)/);
    if (mH) {
      if (inUl) { out.push("</ul>"); inUl = false; }
      if (inOl) { out.push("</ol>"); inOl = false; }
      const lvl = mH[1]!.length;
      out.push(`<h${lvl}>${mH[2]}</h${lvl}>`);
    } else if (mUl) {
      if (inOl) { out.push("</ol>"); inOl = false; }
      if (!inUl) { out.push("<ul>"); inUl = true; }
      out.push(`<li>${mUl[1]}</li>`);
    } else if (mOl) {
      if (inUl) { out.push("</ul>"); inUl = false; }
      if (!inOl) { out.push("<ol>"); inOl = true; }
      out.push(`<li>${mOl[1]}</li>`);
    } else {
      if (inUl) { out.push("</ul>"); inUl = false; }
      if (inOl) { out.push("</ol>"); inOl = false; }
      out.push(line);
    }
  }
  if (inUl) out.push("</ul>");
  if (inOl) out.push("</ol>");
  s = out.join("\n");

  // Bold / italic (after list processing to avoid * in list markers)
  s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__(.+?)__/g, "<strong>$1</strong>");
  s = s.replace(/\*([^*<\n]+)\*/g, "<em>$1</em>");
  s = s.replace(/_([^_<\n]+)_/g, "<em>$1</em>");

  // Newlines → <br>
  s = s.replace(/\n/g, "<br>");

  // Restore code blocks
  s = s.replace(/\x02(\d+)\x03/g, (_, i) => blocks[+i] ?? "");

  return s;
}
