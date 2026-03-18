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

export type ChatHandler        = (content: string, from: string, timestampMs: number) => void;
export type StreamChunkHandler = (chunk: string,   from: string, timestampMs: number) => void;
export type StreamEndHandler   = (from: string) => void;
export type ModeHandler        = (mode: "direct_ws" | "mqtt") => void;

export class WSChatClient {
  private ws: WebSocket | null = null;
  private _chatMode: "direct_ws" | "mqtt" = "mqtt";
  private _onChat:        ChatHandler        | null = null;
  private _onStreamChunk: StreamChunkHandler | null = null;
  private _onStreamEnd:   StreamEndHandler   | null = null;
  private _onMode:        ModeHandler        | null = null;
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private _url = "";
  private _closed = false;

  get chatMode(): "direct_ws" | "mqtt" {
    return this._chatMode;
  }

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  /** Complete (non-streaming) message — slash command replies, errors, etc. */
  onChat(fn: ChatHandler): void { this._onChat = fn; }

  /** One streaming chunk from the LLM. */
  onStreamChunk(fn: StreamChunkHandler): void { this._onStreamChunk = fn; }

  /** Stream finished — render final markdown, clear typing indicator. */
  onStreamEnd(fn: StreamEndHandler): void { this._onStreamEnd = fn; }

  /** Server announced which chat mode is active. */
  onMode(fn: ModeHandler): void { this._onMode = fn; }

  connect(url: string): void {
    this._url = url;
    this._closed = false;
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
  send(content: string): boolean {
    if (!this.connected) return false;
    this.ws!.send(JSON.stringify({ type: "chat", content }));
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
    });

    this.ws.addEventListener("message", (ev: MessageEvent) => {
      let data: Record<string, unknown>;
      try {
        data = JSON.parse(ev.data as string) as Record<string, unknown>;
      } catch {
        return;
      }

      if (data["type"] === "config") {
        const mode = (data["chat_mode"] as string) === "direct_ws" ? "direct_ws" : "mqtt";
        this._chatMode = mode;
        console.info("[WSChat] chat_mode =", mode);
        this._onMode?.(mode);
        return;
      }

      const from  = String(data["from"]      ?? "io-gateway");
      const rawTs = data["timestamp"] as number | undefined;
      const ts    = rawTs ? (rawTs < 1e10 ? rawTs * 1000 : rawTs) : Date.now();

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
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      this._open();
    }, 3000);
  }
}
