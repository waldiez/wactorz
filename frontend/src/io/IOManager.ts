/**
 * IO Manager: routes user input to the appropriate agent.
 *
 * All messages go through the IOAgent via the fixed `io/chat` topic.
 * The `@agent-name` prefix is preserved in the content so IOAgent can route it.
 *
 * If the chat panel has a selected agent, a `@name` prefix is prepended
 * automatically (unless the user already typed one).
 *
 * Also appends messages to the {@link ChatPanel} for display and ensures
 * the panel is visible when the user sends or receives a message.
 */

import { HLCWidGen } from "@waldiez/wid";
import type { AgentInfo, ChatMessage } from "../types/agent";
import type { MQTTClient } from "../mqtt/MQTTClient";
import type { ChatPanel } from "../ui/ChatPanel";
import type { WSChatClient } from "./WSChatClient";
import { tts } from "./TTSManager";

const _widGen = new HLCWidGen({ node: "browser", W: 4 });

export class IOManager {
  /** Tracks the last typing key so we can clear it when any reply arrives. */
  private _lastTypingKey = "";
  private _ws: WSChatClient | null = null;

  constructor(
    private readonly mqtt: MQTTClient,
    private readonly chatPanel: ChatPanel,
  ) {}

  /** Wire in a WSChatClient so send() can use direct WebSocket when available. */
  setWSClient(ws: WSChatClient): void {
    this._ws = ws;

    ws.onStreamChunk((chunk, from) => {
      this.chatPanel.streamChunk(chunk, from);
    });

    ws.onStreamEnd(() => {
      this.chatPanel.hideTyping(this._lastTypingKey);
      this.chatPanel.finalizeStream();
      // Notify after finalizeStream so _streamText is still accessible via the stored msg
      const thread = this.chatPanel.lastStreamedText;
      if (thread) tts.notify(thread);
    });
  }

  /**
   * Send `text` to the appropriate agent via `io/chat`.
   *
   * Opens the chat panel if it isn't already visible so the user immediately
   * sees their message and the typing indicator.
   */
  async send(text: string, agent: AgentInfo | null): Promise<void> {
    let content = text;

    // Prepend @name if a specific agent is selected and no prefix given
    if (agent && !text.startsWith("@")) {
      content = `@${agent.name} ${text}`;
    }

    const msg: ChatMessage = {
      id: _widGen.next(),
      from: "user",
      to: agent?.name ?? "main-actor",
      content: text, // show original (without @-prefix) in panel
      timestampMs: Date.now(),
    };

    // Make the panel visible before appending so the user sees the message
    this.chatPanel.ensureOpen(agent?.name ?? "main-actor");

    // Show message immediately
    this.chatPanel.appendMessage(msg);

    // Show typing indicator and remember the key so we can clear it on reply
    const typingKey = agent?.name ?? "main-actor";
    this._lastTypingKey = typingKey;
    this.chatPanel.showTyping(typingKey, typingKey);

    // direct_ws mode: send over WebSocket only — never fall back to MQTT.
    // Falling back would let IOAgent pick up the message and double-handle it.
    if (this._ws?.chatMode === "direct_ws") {
      const sent = this._ws.send(content);
      if (!sent) {
        setTimeout(() => {
          this.chatPanel.hideTyping(typingKey);
          this.chatPanel.appendMessage({
            id: _widGen.next(),
            from: "system",
            to: "user",
            content: "⚠ WebSocket disconnected — reconnecting, please retry.",
            timestampMs: Date.now(),
          });
        }, 300);
      }
      return;
    }

    // mqtt mode (legacy / no registry): publish to io/chat.
    const published = this.mqtt.publish("io/chat", {
      id: msg.id,
      from: "user",
      to: msg.to,
      content,
      timestampMs: msg.timestampMs,
    });

    if (!published) {
      setTimeout(() => {
        this.chatPanel.hideTyping(typingKey);
        this.chatPanel.appendMessage({
          id: _widGen.next(),
          from: "system",
          to: "user",
          content: "⚠ Not connected — start the backend:\n  docker compose up -d  &&  agentflow",
          timestampMs: Date.now(),
        });
      }, 800);
    }
  }

  /** Route an incoming agent→user chat message to the panel. */
  receiveAgentMessage(msg: ChatMessage): void {
    // Ignore agent↔agent background chatter — only handle user-directed replies.
    // Allow empty/missing `to` (older agents omit it) but drop explicit non-user targets.
    if (msg.to && msg.to !== "user") return;

    // Clear typing indicators: by responder name AND by the key we showed
    // (they differ when Python's io-agent replies to a "main-actor" request)
    if (msg.from) this.chatPanel.hideTyping(msg.from);
    if (this._lastTypingKey && this._lastTypingKey !== msg.from) {
      this.chatPanel.hideTyping(this._lastTypingKey);
    }
    this.chatPanel.appendMessage(msg);
    tts.notify(msg.content, msg.from);
  }
}
