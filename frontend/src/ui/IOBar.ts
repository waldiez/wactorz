/**
 * IO bar (bottom of screen).
 *
 * - Textarea input: sends messages; Enter sends, Shift+Enter inserts newline
 * - Up/Down arrows: navigate message history (last 50 sent)
 * - Mic button: toggles voice recognition via {@link VoiceInput}
 * - Send button: morphs to spinner while awaiting response
 *
 * Coordinates with {@link ChatPanel} via DOM events to know which agent
 * is currently active.
 */

import type { AgentInfo } from "../types/agent";
import type { VoiceInput } from "../io/VoiceInput";
import type { IOManager } from "../io/IOManager";

const HISTORY_LIMIT = 50;

export class IOBar {
  private micBtn: HTMLButtonElement;
  private textInput: HTMLTextAreaElement;
  private sendBtn: HTMLButtonElement;

  private activeAgent: AgentInfo | null = null;
  private isSending = false;
  private voiceInput: VoiceInput;
  private ioManager: IOManager;

  private history: string[] = [];
  private histIdx = -1;
  /** Saved draft while browsing history */
  private draftText = "";

  constructor(voiceInput: VoiceInput, ioManager: IOManager) {
    this.voiceInput = voiceInput;
    this.ioManager = ioManager;

    this.micBtn = document.getElementById("mic-btn") as HTMLButtonElement;
    this.textInput = document.getElementById("text-input") as HTMLTextAreaElement;
    this.sendBtn = document.getElementById("send-btn") as HTMLButtonElement;

    this.bindEvents();
  }

  private bindEvents(): void {
    this.textInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        void this.send();
        return;
      }
      if (e.key === "ArrowUp" && !e.shiftKey) {
        e.preventDefault();
        this.historyUp();
        return;
      }
      if (e.key === "ArrowDown" && !e.shiftKey) {
        e.preventDefault();
        this.historyDown();
        return;
      }
      // Any other key resets history navigation
      if (!["ArrowUp", "ArrowDown"].includes(e.key)) {
        this.histIdx = -1;
      }
    });

    this.textInput.addEventListener("input", () => {
      this.autoGrow();
    });

    this.sendBtn.addEventListener("click", () => void this.send());

    // Mic toggle
    this.micBtn.addEventListener("click", () => void this.toggleMic());

    // Update placeholder + activeAgent when chat panel opens/closes
    document.addEventListener("panel-opened", (e) => {
      const evt = e as CustomEvent<{ agent: AgentInfo }>;
      this.activeAgent = evt.detail.agent;
      this.textInput.placeholder = `Talk to @${evt.detail.agent.name}…`;
    });
    document.addEventListener("panel-closed", () => {
      this.activeAgent = null;
      this.textInput.placeholder = "Talk to io-agent… (type @name to target)";
    });

    // Voice transcript → text input
    this.voiceInput.onTranscript = (text, final) => {
      this.textInput.value = text;
      this.autoGrow();
      if (final) void this.send();
    };

    // Sync mic button state when recognition ends for any reason
    this.voiceInput.onStop = () => {
      this.micBtn.classList.remove("recording");
      this.micBtn.title = "Voice input";
    };

    this.voiceInput.onError = (message) => {
      this.micBtn.classList.remove("recording");
      this.micBtn.title = message;
      // Reset title after a few seconds so it doesn't linger
      setTimeout(() => {
        this.micBtn.title = "Voice input";
      }, 5000);
    };
  }

  private autoGrow(): void {
    const el = this.textInput;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  }

  private historyUp(): void {
    if (this.history.length === 0) return;
    if (this.histIdx === -1) {
      this.draftText = this.textInput.value;
    }
    this.histIdx = Math.min(this.histIdx + 1, this.history.length - 1);
    this.textInput.value = this.history[this.histIdx] ?? "";
    this.autoGrow();
    // Move cursor to end
    const len = this.textInput.value.length;
    this.textInput.setSelectionRange(len, len);
  }

  private historyDown(): void {
    if (this.histIdx === -1) return;
    this.histIdx--;
    if (this.histIdx === -1) {
      this.textInput.value = this.draftText;
    } else {
      this.textInput.value = this.history[this.histIdx] ?? "";
    }
    this.autoGrow();
    const len = this.textInput.value.length;
    this.textInput.setSelectionRange(len, len);
  }

  private async send(): Promise<void> {
    const text = this.textInput.value.trim();
    if (!text || this.isSending) return;

    // Record in history (most recent first)
    this.history.unshift(text);
    if (this.history.length > HISTORY_LIMIT) this.history.pop();
    this.histIdx = -1;
    this.draftText = "";

    this.isSending = true;
    this.sendBtn.classList.add("sending");
    this.textInput.value = "";
    this.autoGrow();

    try {
      await this.ioManager.send(text, this.activeAgent);
    } finally {
      this.isSending = false;
      this.sendBtn.classList.remove("sending");
    }
  }

  private async toggleMic(): Promise<void> {
    if (this.voiceInput.isRecording) {
      this.voiceInput.stop();
      this.micBtn.classList.remove("recording");
      this.micBtn.title = "Voice input";
    } else {
      const started = await this.voiceInput.start();
      if (started) {
        this.micBtn.classList.add("recording");
        this.micBtn.title = "Stop recording";
      }
    }
  }
}
