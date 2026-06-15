/**
 * Web Speech API wrapper.
 *
 * Two modes:
 *   PTT (push-to-talk) — start()/stop(), fires onTranscript/onStop/onError
 *   Ambient (wake word) — startAmbient(word)/stopAmbient(), fires onWakeWord
 *
 * Only one recognition session runs at a time. PTT pauses ambient and resumes
 * it automatically when PTT ends.
 *
 * Browser support: Chrome, Edge (full); Firefox (partial with flag).
 * Falls back gracefully when the API is unavailable.
 */

/** A function called with each transcript result. */
export type TranscriptCallback = (text: string, isFinal: boolean) => void;

// ── Minimal Speech Recognition type shims ────────────────────────────────────

interface SpeechRecognitionResultItem {
  readonly transcript: string;
  readonly confidence: number;
}

interface SpeechRecognitionResult {
  readonly isFinal: boolean;
  readonly length: number;
  item(index: number): SpeechRecognitionResultItem;
  [index: number]: SpeechRecognitionResultItem | undefined;
}

interface SpeechRecognitionResultList {
  readonly length: number;
  item(index: number): SpeechRecognitionResult;
  [index: number]: SpeechRecognitionResult | undefined;
}

interface SpeechRecognitionEventLike extends Event {
  readonly resultIndex: number;
  readonly results: SpeechRecognitionResultList;
}

interface SpeechRecognitionInstance extends EventTarget {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((e: SpeechRecognitionEventLike) => void) | null;
  onend: (() => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  start(): void;
  stop(): void;
}

type SpeechRecognitionConstructor = new () => SpeechRecognitionInstance;

// ── Class ─────────────────────────────────────────────────────────────────────

export class VoiceInput {
  private _API: SpeechRecognitionConstructor | undefined;
  private recognition: SpeechRecognitionInstance | null = null;
  private _isRecording = false;

  // Ambient / wake-word state
  private _ambientRec: SpeechRecognitionInstance | null = null;
  private _isAmbient = false;
  private _wakeWord = "wactorz";
  private _ambientTimer: ReturnType<typeof setTimeout> | null = null;

  /** Called whenever a PTT transcript (partial or final) is available. */
  onTranscript: TranscriptCallback | null = null;

  /** Called when PTT recording stops for any reason. */
  onStop: (() => void) | null = null;

  /** Called when a user-visible PTT error occurs. */
  onError: ((message: string) => void) | null = null;

  /** Called when the wake word is detected. `textAfter` is any speech that
   *  followed the keyword in the same utterance (may be empty). */
  onWakeWord: ((textAfter: string) => void) | null = null;

  /** Called when ambient listening stops permanently (permission denied, no mic). */
  onAmbientStop: (() => void) | null = null;

  /** Why voice is unavailable (for UI messaging); "" when available. */
  unavailableReason: "" | "no-api" | "insecure" = "";

  constructor() {
    const win = window as unknown as Record<string, unknown>;
    const API = (win["SpeechRecognition"] ?? win["webkitSpeechRecognition"]) as
      | SpeechRecognitionConstructor
      | undefined;

    // getUserMedia + SpeechRecognition only work in a secure context (HTTPS or
    // localhost). The HA add-on serves the UI over HTTP via ingress, so
    // navigator.mediaDevices is undefined and the mic can never start — treat
    // that as unavailable rather than showing a button that silently fails.
    const secure =
      window.isSecureContext && !!navigator.mediaDevices?.getUserMedia;

    if (!API) {
      this.unavailableReason = "no-api";
      console.warn("[VoiceInput] Web Speech API not available in this browser.");
      return;
    }
    if (!secure) {
      this.unavailableReason = "insecure";
      console.warn(
        "[VoiceInput] mic unavailable: insecure context (needs HTTPS or localhost).",
      );
      return;
    }

    this._API = API;
    this.recognition = new API();
    this.recognition.continuous = false;
    this.recognition.interimResults = true;
    this.recognition.lang = "en-US";

    this.recognition.onresult = (event: SpeechRecognitionEventLike) => {
      let interim = "";
      let final = "";

      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (!result) continue;
        const transcript = result[0]?.transcript ?? "";
        if (result.isFinal) final += transcript;
        else interim += transcript;
      }

      if (final) this.onTranscript?.(final.trim(), true);
      else if (interim) this.onTranscript?.(interim.trim(), false);
    };

    this.recognition.onend = () => {
      this._isRecording = false;
      this.onStop?.();
      // Resume ambient after PTT session ends
      if (this._isAmbient) this._scheduleAmbient();
    };

    this.recognition.onerror = (event: { error: string }) => {
      const permanent = new Set([
        "service-not-allowed",
        "not-allowed",
        "audio-capture",
      ]);
      const userMessages: Record<string, string> = {
        "not-allowed":
          "Microphone access denied. Check your browser/OS permissions.",
        "service-not-allowed":
          "Speech recognition requires HTTPS. Mic unavailable over HTTP.",
        "audio-capture": "No microphone detected.",
        network: "Network error during speech recognition.",
      };
      const msg = userMessages[event.error];
      if (msg) this.onError?.(msg);
      else if (event.error !== "no-speech" && event.error !== "aborted") {
        console.warn("[VoiceInput] Recognition error:", event.error);
      }
      this._isRecording = false;
      if (permanent.has(event.error)) {
        this.recognition = null;
      }
      this.onStop?.();
    };
  }

  /**
   * Start PTT recording.
   * Pauses ambient listening while recording; ambient resumes automatically
   * when recording ends.
   */
  async start(): Promise<boolean> {
    if (!this.recognition || this._isRecording) return false;

    // Suspend ambient — browser only allows one active session at a time
    this._stopAmbientSession();

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((t) => t.stop());
    } catch {
      this.onError?.(
        "Microphone access denied. Check your browser/OS permissions.",
      );
      // Restore ambient since PTT failed to start
      if (this._isAmbient) this._scheduleAmbient();
      return false;
    }

    this.recognition.start();
    this._isRecording = true;
    return true;
  }

  /** Stop PTT recording. */
  stop(): void {
    if (!this.recognition || !this._isRecording) return;
    this.recognition.stop();
    this._isRecording = false;
  }

  /**
   * Start always-on wake-word listening.
   * Returns false if the API is unavailable or already in ambient mode.
   */
  startAmbient(wakeWord = "computer"): boolean {
    if (!this._API || this._isAmbient) return false;
    this._wakeWord = wakeWord.toLowerCase().trim();
    this._isAmbient = true;
    this._launchAmbient();
    return true;
  }

  /** Stop wake-word listening. */
  stopAmbient(): void {
    this._isAmbient = false;
    this._stopAmbientSession();
  }

  get isRecording(): boolean { return this._isRecording; }
  get isAvailable(): boolean { return this.recognition !== null; }
  get isAmbient():   boolean { return this._isAmbient; }

  // ── Private ──────────────────────────────────────────────────────────────────

  private _stopAmbientSession(): void {
    if (this._ambientTimer !== null) {
      clearTimeout(this._ambientTimer);
      this._ambientTimer = null;
    }
    if (this._ambientRec) {
      try { this._ambientRec.stop(); } catch { /* already stopped */ }
      this._ambientRec = null;
    }
  }

  private _scheduleAmbient(): void {
    if (!this._isAmbient || this._isRecording) return;
    this._ambientTimer = setTimeout(() => {
      this._ambientTimer = null;
      this._launchAmbient();
    }, 300);
  }

  private _launchAmbient(): void {
    if (!this._isAmbient || this._isRecording || !this._API) return;

    const rec = new this._API();
    rec.continuous = true;
    rec.interimResults = false; // only final results for wake-word matching
    rec.lang = "en-US";

    rec.onresult = (event: SpeechRecognitionEventLike) => {
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (!result?.isFinal) continue;
        const text = (result[0]?.transcript ?? "").toLowerCase().trim();
        const idx = text.indexOf(this._wakeWord);
        if (idx === -1) continue;
        const after = text.slice(idx + this._wakeWord.length).trim();
        this.onWakeWord?.(after);
      }
    };

    rec.onend = () => {
      if (this._ambientRec === rec) this._ambientRec = null;
      this._scheduleAmbient();
    };

    rec.onerror = (e: { error: string }) => {
      const permanent = new Set(["not-allowed", "service-not-allowed", "audio-capture"]);
      if (permanent.has(e.error)) {
        this._isAmbient = false;
        this._ambientRec = null;
        this.onAmbientStop?.();
      }
      // Non-permanent errors: onend will schedule restart
    };

    this._ambientRec = rec;
    try {
      rec.start();
    } catch (err) {
      console.warn("[VoiceInput] Failed to start ambient session:", err);
      this._ambientRec = null;
      this._scheduleAmbient();
    }
  }
}
