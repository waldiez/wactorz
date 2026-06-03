/**
 * WSChatClient — lightweight wrapper around the monitor server's /ws endpoint.
 *
 * On connect the server sends:
 *   {"type":"config","chat_mode":"direct_ws"|"mqtt"}
 *
 * When chat_mode is "direct_ws" the browser should send chat messages here
 * instead of publishing to MQTT io/chat.  The server streams replies back as:
 *   {"type":"chat","from":"io-gateway","content":"...","timestamp":...}
 */

export type ChatHandler = (
  content: string,
  from: string,
  timestampMs: number,
) => void;
export type StreamChunkHandler = (
  chunk: string,
  from: string,
  timestampMs: number,
) => void;
export type StreamEndHandler = (from: string) => void;
export type ModeHandler = (mode: "direct_ws" | "mqtt") => void;

/** One agent entry as the server includes it in state-patch messages. */
export type StatePatchAgent = {
  agent_id: string;
  name?: string;
  state?: string;
  status?: string;
  protected?: boolean;
  messages_processed?: number;
  cost_usd?: number;
  uptime?: number;
  cpu?: number;
  mem?: number;
  task?: string;
  agent_type?: string;
};

/** Snapshot-level totals computed by the backend (includes historical/deleted agents). */
export type SnapshotStats = {
  totalCostUsd?: number;
  totalMessages?: number;
};

/**
 * Called whenever the server broadcasts a state patch over the WebSocket.
 * `deletedId` is set when the server explicitly deletes an agent.
 * `stats` carries backend-computed totals that include deleted agents.
 */
export type StatePatchHandler = (
  agents: StatePatchAgent[],
  deletedId?: string,
  stats?: SnapshotStats,
) => void;

/** One MQTT-derived event entry from the server's in-memory log_feed. */
export interface LogFeedItem {
  type: string;
  agent_id?: string;
  name?: string;
  agentName?: string;
  message?: string;
  text?: string;
  timestamp?: number;
  status?: Record<string, unknown>;
  severity?: string;
  agentType?: string;
  agent_type?: string;
  command?: string;
}

export type LogFeedHandler = (items: LogFeedItem[]) => void;

export class WSChatClient {
  private ws: WebSocket | null = null;
  private _chatMode: "direct_ws" | "mqtt" = "mqtt";
  private _onChat: ChatHandler | null = null;
  private _onStreamChunk: StreamChunkHandler | null = null;
  private _onStreamEnd: StreamEndHandler | null = null;
  private _onMode: ModeHandler | null = null;
  private _onStatePatch: StatePatchHandler | null = null;
  private _onLogFeed: LogFeedHandler | null = null;
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private _reconnectDelay = 1_000;
  private _url = "";
  private _closed = false;

  get chatMode(): "direct_ws" | "mqtt" {
    return this._chatMode;
  }

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  /** Complete (non-streaming) message — slash command replies, errors, etc. */
  onChat(fn: ChatHandler): void {
    this._onChat = fn;
  }

  /** One streaming chunk from the LLM. */
  onStreamChunk(fn: StreamChunkHandler): void {
    this._onStreamChunk = fn;
  }

  /** Stream finished — render final markdown, clear typing indicator. */
  onStreamEnd(fn: StreamEndHandler): void {
    this._onStreamEnd = fn;
  }

  /** Server announced which chat mode is active. */
  onMode(fn: ModeHandler): void {
    this._onMode = fn;
  }

  /** Server broadcast a state patch (agent list updated, or agent deleted). */
  onStatePatch(fn: StatePatchHandler): void {
    this._onStatePatch = fn;
  }

  /** Server broadcast new MQTT-derived log_feed entries inside a state patch. */
  onLogFeed(fn: LogFeedHandler): void {
    this._onLogFeed = fn;
  }

  connect(url: string): void {
    this._url = url;
    this._closed = false;
    this._reconnectDelay = 1_000;
    this._open();
  }

  disconnect(): void {
    this._closed = true;
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }

  /**
   * Send a chat message over the WebSocket.
   * Returns false when the socket is not open (caller can fall back to MQTT).
   */
  send(content: string, agentName = "main-actor"): boolean {
    if (!this.connected) return false;
    this.ws!.send(
      JSON.stringify({ type: "chat", content, agent_name: agentName }),
    );
    return true;
  }

  /**
   * Send any raw JSON object over the WebSocket (e.g. agent commands).
   * Returns false when the socket is not open.
   */
  sendRaw(msg: object): boolean {
    if (!this.connected) return false;
    this.ws!.send(JSON.stringify(msg));
    return true;
  }

  // ── Private ──────────────────────────────────────────────────────────────────

  private _open(): void {
    try {
      this.ws = new WebSocket(this._url);
    } catch (err) {
      console.warn("[WSChat] Cannot open WebSocket:", err);
      this._scheduleReconnect();
      return;
    }

    this.ws.addEventListener("open", () => {
      console.info("[WSChat] connected →", this._url);
      this._reconnectDelay = 1_000;
    });

    this.ws.addEventListener("message", (ev: MessageEvent) => {
      let data: Record<string, unknown>;
      try {
        data = JSON.parse(ev.data as string) as Record<string, unknown>;
      } catch {
        return;
      }

      if (data["type"] === "config") {
        const mode =
          (data["chat_mode"] as string) === "direct_ws" ? "direct_ws" : "mqtt";
        this._chatMode = mode;
        console.info("[WSChat] chat_mode =", mode);
        this._onMode?.(mode);
        return;
      }

      // State reset broadcast — apply state patch then clear UI as needed
      if (data["type"] === "reset") {
        const scope = String(data["scope"] ?? "");
        if (scope === "all") {
          document.dispatchEvent(new CustomEvent("af-wipe-all"));
          return;
        }
        const patch = data["state"] as
          | { agents?: StatePatchAgent[]; total_cost_usd?: number; total_messages?: number; log_feed?: LogFeedItem[] }
          | undefined;
        const stats: SnapshotStats = {};
        if (patch?.total_cost_usd !== undefined) stats.totalCostUsd = patch.total_cost_usd;
        if (patch?.total_messages !== undefined) stats.totalMessages = patch.total_messages;
        this._onStatePatch?.(patch?.agents ?? [], undefined, stats);
        if (patch?.log_feed?.length) this._onLogFeed?.(patch.log_feed);
        if (scope === "chat") {
          document.dispatchEvent(new CustomEvent("af-reset-chat", {
            detail: { agent: data["agent"] ?? null },
          }));
        }
        return;
      }

      // Server explicitly deleted an agent — remove it and apply rest of patch
      if (data["type"] === "delete_agent") {
        const patch = data["state"] as
          | { agents?: StatePatchAgent[]; total_cost_usd?: number; total_messages?: number; log_feed?: LogFeedItem[] }
          | undefined;
        const stats: SnapshotStats = {};
        if (patch?.total_cost_usd !== undefined) stats.totalCostUsd = patch.total_cost_usd;
        if (patch?.total_messages !== undefined) stats.totalMessages = patch.total_messages;
        this._onStatePatch?.(patch?.agents ?? [], String(data["agent_id"] ?? ""), stats);
        if (patch?.log_feed?.length) this._onLogFeed?.(patch.log_feed);
        return;
      }

      // Any message with a "state" field is a state patch broadcast
      if (data["state"]) {
        const patch = data["state"] as { agents?: StatePatchAgent[]; total_cost_usd?: number; total_messages?: number; log_feed?: LogFeedItem[] };
        const stats: SnapshotStats = {};
        if (patch.total_cost_usd !== undefined) stats.totalCostUsd = patch.total_cost_usd;
        if (patch.total_messages !== undefined) stats.totalMessages = patch.total_messages;
        this._onStatePatch?.(patch.agents ?? [], undefined, stats);
        if (patch.log_feed?.length) this._onLogFeed?.(patch.log_feed);
        // don't return — message may also carry chat/stream content
      }

      const from = String(data["from"] ?? "io-gateway");
      const rawTs = data["timestamp"] as number | undefined;
      const ts = rawTs ? (rawTs < 1e10 ? rawTs * 1000 : rawTs) : Date.now();

      if (data["type"] === "chat") {
        this._onChat?.(String(data["content"] ?? ""), from, ts);
      } else if (data["type"] === "stream_chunk") {
        this._onStreamChunk?.(String(data["content"] ?? ""), from, ts);
      } else if (data["type"] === "stream_end") {
        this._onStreamEnd?.(from);
      }
    });

    this.ws.addEventListener("close", () => {
      if (!this._closed) this._scheduleReconnect();
    });

    this.ws.addEventListener("error", () => {
      // "close" follows "error" — reconnect happens there
    });
  }

  private _scheduleReconnect(): void {
    if (this._closed || this._reconnectTimer !== null) return;
    const delay = this._reconnectDelay;
    this._reconnectDelay = Math.min(this._reconnectDelay * 2, 30_000);
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      this._open();
    }, delay);
  }
}
