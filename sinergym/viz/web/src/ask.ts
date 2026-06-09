// "Ask the run" — natural-language Q&A over the run via the sinergym-hsml agent.
//
// This is Tier 2: it talks to a wactorz runtime's monitor WebSocket (/ws on :8888),
// the same channel the desktop app uses — sends {type:"chat", content:"@sinergym-hsml …"}
// and renders the gateway's chat / stream_chunk / stream_end replies. hsml appends the
// SPARQL it generated, which we render in a code block. When the WS isn't reachable
// (pure-viz deployment, no wactorz/LLM) the panel shows a clear "needs wactorz" hint.
const GATEWAY = "io-gateway";
const SUGGESTIONS = [
  "which agent controls Perimeter_top_ZN_1?",
  "what setpoints did maddpg-zone-11 choose near step 20000?",
  "what anomalies were detected, and of what kind?",
];

const el = (tag: string, cls?: string, html?: string) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
};

export class AskPanel {
  private ws?: WebSocket;
  private connected = false;
  private chatMode = "";
  private panel: HTMLElement;
  private thread: HTMLElement;
  private input: HTMLInputElement;
  private hint: HTMLElement;
  private streamBubble?: HTMLElement;
  private port: number;

  constructor(port = 8888) {
    this.port = port;
    const { panel, thread, input, hint } = this.build();
    this.panel = panel; this.thread = thread; this.input = input; this.hint = hint;
    this.connect();
  }

  toggle() { this.panel.classList.toggle("open"); if (this.panel.classList.contains("open")) this.input.focus(); }
  close() { this.panel.classList.remove("open"); }

  // ── WS ───────────────────────────────────────────────────────────────────────
  private connect() {
    let ws: WebSocket;
    try { ws = new WebSocket(`ws://${location.hostname}:${this.port}/ws`); }
    catch { this.setOffline(); return; }
    this.ws = ws;
    ws.onopen = () => { this.connected = true; this.setOnline(); };
    ws.onclose = () => { this.connected = false; this.setOffline(); setTimeout(() => this.connect(), 4000); };
    ws.onerror = () => { this.connected = false; this.setOffline(); };
    ws.onmessage = (ev) => this.onMessage(ev.data);
  }

  private onMessage(raw: string) {
    let m: any; try { m = JSON.parse(raw); } catch { return; }
    if (m.type === "config") { this.chatMode = m.chat_mode ?? ""; if (this.chatMode !== "direct_ws") this.setMqttNote(); return; }
    if (m.from && m.from !== GATEWAY) return;   // only gateway replies
    if (m.type === "stream_chunk") { this.appendStream(m.content ?? ""); }
    else if (m.type === "stream_end") { this.endStream(); }
    else if (m.type === "chat") { this.endStream(); this.addAgent(m.content ?? ""); }
  }

  private send(q: string) {
    q = q.trim(); if (!q) return;
    this.addUser(q);
    if (!this.connected || !this.ws) { this.addSystem("Not connected to a wactorz runtime — can't ask hsml right now."); return; }
    if (this.chatMode && this.chatMode !== "direct_ws") {
      this.addSystem("This wactorz is in MQTT chat mode; the viz panel needs direct_ws."); return;
    }
    this.ws.send(JSON.stringify({ type: "chat", content: `@sinergym-hsml ${q}`, agent_name: "sinergym-hsml" }));
    this.pending();
  }

  // ── message rendering ─────────────────────────────────────────────────────────
  private addUser(t: string) { this.push("ask-msg ask-user", this.escape(t)); }
  private addSystem(t: string) { this.push("ask-msg ask-sys", this.escape(t)); }

  private addAgent(t: string) {
    const { prose, sparql } = this.splitSparql(t);
    const wrap = el("div", "ask-msg ask-agent");
    wrap.appendChild(el("div", "ask-prose", this.escape(prose).replace(/\n/g, "<br>")));
    if (sparql) {
      const det = el("details", "ask-sparql") as HTMLDetailsElement;
      det.appendChild(el("summary", undefined, "generated SPARQL"));
      det.appendChild(el("pre", undefined, this.escape(sparql)));
      wrap.appendChild(det);
    }
    this.thread.appendChild(wrap);
    this.scroll();
  }

  private pending() {
    this.streamBubble = el("div", "ask-msg ask-agent ask-typing", "<span></span><span></span><span></span>");
    this.thread.appendChild(this.streamBubble); this.scroll();
  }
  private appendStream(chunk: string) {
    if (!this.streamBubble || this.streamBubble.classList.contains("ask-typing")) {
      this.streamBubble = el("div", "ask-msg ask-agent"); this.thread.appendChild(this.streamBubble);
    }
    this.streamBubble.textContent = (this.streamBubble.textContent ?? "") + chunk; this.scroll();
  }
  private endStream() {
    if (this.streamBubble?.classList.contains("ask-typing")) { this.streamBubble.remove(); }
    else if (this.streamBubble) { const t = this.streamBubble.textContent ?? ""; this.streamBubble.remove(); if (t) this.addAgent(t); }
    this.streamBubble = undefined;
  }

  private push(cls: string, html: string) { this.thread.appendChild(el("div", cls, html)); this.scroll(); }
  private scroll() { this.thread.scrollTop = this.thread.scrollHeight; }

  private splitSparql(t: string): { prose: string; sparql: string } {
    const fence = t.match(/```(?:sparql)?\s*([\s\S]*?)```/i);
    if (fence) return { prose: t.replace(fence[0], "").trim(), sparql: fence[1].trim() };
    const m = t.match(/\n\s*(PREFIX |SELECT |ASK |CONSTRUCT )/i);
    if (m && m.index != null) return { prose: t.slice(0, m.index).trim(), sparql: t.slice(m.index).trim() };
    return { prose: t.trim(), sparql: "" };
  }
  private escape(s: string) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

  // ── connection state ──────────────────────────────────────────────────────────
  private setOnline() { this.hint.classList.add("hidden"); this.input.disabled = false; this.input.placeholder = "ask about this run…"; }
  private setOffline() {
    this.hint.classList.remove("hidden");
    this.hint.innerHTML = `<b>Needs a wactorz runtime.</b> Start wactorz with an LLM provider and ` +
      `<code>@catalog spawn sinergym-hsml</code> to ask questions about the run.`;
    this.input.disabled = true; this.input.placeholder = "offline";
  }
  private setMqttNote() {
    this.hint.classList.remove("hidden");
    this.hint.innerHTML = `<b>MQTT chat mode.</b> This panel needs wactorz in <code>direct_ws</code> mode.`;
  }

  // ── DOM ───────────────────────────────────────────────────────────────────────
  private build() {
    const btn = el("button", "ask-launch", "✦ Ask the run");
    btn.addEventListener("click", () => this.toggle());
    document.getElementById("topbar")!.insertBefore(btn, document.getElementById("conn"));

    const panel = el("aside", "ask-panel glass");
    panel.innerHTML = `
      <div class="ask-head">
        <span>✦ Ask the run</span>
        <button class="ask-close" title="close">✕</button>
      </div>
      <div class="ask-hint hidden"></div>
      <div class="ask-thread"></div>
      <div class="ask-chips"></div>
      <form class="ask-form">
        <input class="ask-input" type="text" autocomplete="off" placeholder="ask about this run…" />
        <button class="ask-send" type="submit">↩</button>
      </form>`;
    document.getElementById("app")!.appendChild(panel);

    panel.querySelector(".ask-close")!.addEventListener("click", () => this.close());
    const form = panel.querySelector(".ask-form") as HTMLFormElement;
    const input = panel.querySelector(".ask-input") as HTMLInputElement;
    form.addEventListener("submit", (e) => { e.preventDefault(); this.send(input.value); input.value = ""; });

    const chips = panel.querySelector(".ask-chips")!;
    for (const s of SUGGESTIONS) {
      const c = el("button", "ask-chip", this.escape(s));
      c.addEventListener("click", () => { input.value = s; this.send(s); input.value = ""; });
      chips.appendChild(c);
    }
    return { panel, thread: panel.querySelector(".ask-thread") as HTMLElement, input, hint: panel.querySelector(".ask-hint") as HTMLElement };
  }
}
