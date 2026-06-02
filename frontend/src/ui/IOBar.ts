/**
 * IO bar (bottom of screen).
 *
 * - Mic button: tap-to-toggle recording (click once to start, click again to stop)
 * - Textarea input: sends messages; Enter sends, Shift+Enter inserts newline
 * - Up/Down arrows: navigate message history (last 50 sent)
 * - Send button: morphs to spinner while awaiting response
 *
 * Coordinates with {@link ChatPanel} via DOM events to know which agent
 * is currently active.
 */

import type { AgentInfo } from "../types/agent";
import type { VoiceInput } from "../io/VoiceInput";
import type { IOManager } from "../io/IOManager";

const HISTORY_LIMIT = 50;
const LS_WAKE_ACTIVE = "wactorz.wakeActive";
const LS_WAKE_WORD   = "wactorz.wakeWordText";

export class IOBar {
  private micBtn:  HTMLButtonElement;
  private wakeBtn: HTMLButtonElement;
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
    this.ioManager  = ioManager;

    this.micBtn    = document.getElementById("mic-btn")    as HTMLButtonElement;
    this.wakeBtn   = document.getElementById("wake-btn")   as HTMLButtonElement;
    this.textInput = document.getElementById("text-input") as HTMLTextAreaElement;
    this.sendBtn   = document.getElementById("send-btn")   as HTMLButtonElement;

    this.bindEvents();

    // Always hide the wake button (deferred past 0.5)
    this.wakeBtn.style.display = "none";

    // Hide mic button if the API isn't available
    if (!this.voiceInput.isAvailable) {
      this.micBtn.style.display = "none";
      (document.body as any).__voiceUnavailable = true;
    }
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
      if (!["ArrowUp", "ArrowDown"].includes(e.key)) this.histIdx = -1;
    });

    this.textInput.addEventListener("input", () => this.autoGrow());

    this.sendBtn.addEventListener("click", () => void this.send());

    // Tap-to-toggle: click once to start recording, click again to stop
    this.micBtn.addEventListener("click", () => {
      if (this.voiceInput.isRecording) {
        this.stopMic();
      } else {
        void this.startMic();
      }
    });

    // Wake-word toggle
    this.wakeBtn.addEventListener("click", () => this._toggleWake());

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

    // PTT transcript → fill the active input → auto-send on final
    this.voiceInput.onTranscript = (text, final) => {
      const cdInput = document.getElementById("af-iobar-input") as HTMLInputElement | null;
      if (cdInput) {
        cdInput.value = text;
        if (final) {
          const sel = document.getElementById("af-target-select") as HTMLSelectElement | null;
          document.dispatchEvent(new CustomEvent("af-send-message", {
            detail: { content: text, target: sel?.value ?? "main" },
          }));
          cdInput.value = "";
        }
      } else {
        this.textInput.value = text;
        this.autoGrow();
        if (final) void this.send();
      }
    };

    // Sync mic button when recording ends
    this.voiceInput.onStop = () => {
      this.micBtn.classList.remove("recording");
      this.micBtn.title = "Tap to speak";
      const cdMic = document.getElementById("af-mic-btn-cd");
      if (cdMic) { cdMic.classList.remove("recording"); cdMic.title = "Tap to speak"; }
    };

    this.voiceInput.onError = (message) => {
      this.micBtn.classList.remove("recording");
      this.micBtn.title = message;
      if (!this.voiceInput.isAvailable) {
        this.micBtn.style.display  = "none";
        this.wakeBtn.style.display = "none";
        const cdMic  = document.getElementById("af-mic-btn-cd");
        const cdWake = document.getElementById("af-wake-btn-cd");
        if (cdMic)  cdMic.style.display  = "none";
        if (cdWake) cdWake.style.display = "none";
      } else {
        setTimeout(() => { this.micBtn.title = "Voice input"; }, 5000);
      }
    };

    // Wake-word detected: fill + send the active input, or open PTT
    this.voiceInput.onWakeWord = (textAfter) => {
      this._syncWakeTriggered();
      const cdInput = document.getElementById("af-iobar-input") as HTMLInputElement | null;
      if (textAfter) {
        if (cdInput) {
          const sel = document.getElementById("af-target-select") as HTMLSelectElement | null;
          document.dispatchEvent(new CustomEvent("af-send-message", {
            detail: { content: textAfter, target: sel?.value ?? "main" },
          }));
        } else {
          this.textInput.value = textAfter;
          this.autoGrow();
          void this.send();
        }
      } else {
        void this.startMic();
      }
    };

    // Ambient listening stopped permanently (e.g. HTTPS required)
    this.voiceInput.onAmbientStop = () => {
      localStorage.setItem(LS_WAKE_ACTIVE, "0");
      this.wakeBtn.classList.remove("ambient");
      this.wakeBtn.title = "Wake word (unavailable — requires HTTPS)";
      this.wakeBtn.style.display = "none";
      const cdWake = document.getElementById("af-wake-btn-cd");
      if (cdWake) cdWake.style.display = "none";
    };
  }

  toggleWake(): void { this._toggleWake(); }

  private _syncWakeTriggered(): void {
    this.wakeBtn.classList.add("triggered");
    setTimeout(() => this.wakeBtn.classList.remove("triggered"), 700);
    const cdWake = document.getElementById("af-wake-btn-cd");
    if (cdWake) {
      cdWake.classList.add("triggered");
      setTimeout(() => cdWake.classList.remove("triggered"), 700);
    }
  }

  private _toggleWake(): void {
    if (this.voiceInput.isAmbient) {
      this.voiceInput.stopAmbient();
      localStorage.setItem(LS_WAKE_ACTIVE, "0");
      this.wakeBtn.classList.remove("ambient");
      this.wakeBtn.title = "Wake word — click to enable";
      const cdWake = document.getElementById("af-wake-btn-cd");
      if (cdWake) { cdWake.classList.remove("ambient"); cdWake.title = "Wake word — click to enable"; }
    } else {
      const word = localStorage.getItem(LS_WAKE_WORD) ?? "computer";
      if (this.voiceInput.startAmbient(word)) {
        localStorage.setItem(LS_WAKE_ACTIVE, "1");
        this.wakeBtn.classList.add("ambient");
        this.wakeBtn.title = `Listening for "${word}" — click to disable`;
        const cdWake = document.getElementById("af-wake-btn-cd");
        if (cdWake) { cdWake.classList.add("ambient"); cdWake.title = `Listening for "${word}" — click to disable`; }
      } else {
        localStorage.setItem(LS_WAKE_ACTIVE, "0");
        this.wakeBtn.title = "Wake word unavailable (requires HTTPS + Chrome/Edge)";
        setTimeout(() => { this.wakeBtn.title = "Wake word — click to enable"; }, 4000);
      }
    }
  }

  private autoGrow(): void {
    const el = this.textInput;
    el.style.height = "1px";
    const h = Math.min(el.scrollHeight, 120);
    el.style.height = h + "px";
    el.style.overflowY = h >= 120 ? "auto" : "hidden";
  }

  private historyUp(): void {
    if (this.history.length === 0) return;
    if (this.histIdx === -1) this.draftText = this.textInput.value;
    this.histIdx = Math.min(this.histIdx + 1, this.history.length - 1);
    this.textInput.value = this.history[this.histIdx] ?? "";
    this.autoGrow();
    const len = this.textInput.value.length;
    this.textInput.setSelectionRange(len, len);
  }

  private historyDown(): void {
    if (this.histIdx === -1) return;
    this.histIdx--;
    this.textInput.value = this.histIdx === -1
      ? this.draftText
      : (this.history[this.histIdx] ?? "");
    this.autoGrow();
    const len = this.textInput.value.length;
    this.textInput.setSelectionRange(len, len);
  }

  private async send(): Promise<void> {
    const text = this.textInput.value.trim();
    if (!text || this.isSending) return;

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

  async startMic(): Promise<void> {
    if (this.voiceInput.isRecording) return;
    const started = await this.voiceInput.start();
    if (started) {
      this.micBtn.classList.add("recording");
      this.micBtn.title = "Tap to stop";
    }
  }

  stopMic(): void {
    if (!this.voiceInput.isRecording) return;
    this.voiceInput.stop();
  }
}
