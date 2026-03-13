/**
 * Web Speech API wrapper.
 *
 * Provides a clean start/stop interface over `SpeechRecognition`.
 * Interim results are surfaced in real-time via `onTranscript`.
 *
 * Browser support: Chrome, Edge (full); Firefox (partial with flag).
 * Falls back gracefully when the API is unavailable.
 */

/** A function called with each transcript result. */
export type TranscriptCallback = (text: string, isFinal: boolean) => void;

// ── Minimal Speech Recognition type shims ────────────────────────────────────
// The Web Speech API is not yet in all TypeScript DOM lib versions.
// We declare just enough types here to use it safely.

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
  private recognition: SpeechRecognitionInstance | null = null;
  private _isRecording = false;

  /** Called whenever a transcript (partial or final) is available. */
  onTranscript: TranscriptCallback | null = null;

  /** Called when recording stops for any reason (manual stop, natural end, or error). */
  onStop: (() => void) | null = null;

  /** Called when a user-visible error occurs (e.g. permission denied, no microphone). */
  onError: ((message: string) => void) | null = null;

  constructor() {
    const win = window as unknown as Record<string, unknown>;
    const API = (win["SpeechRecognition"] ?? win["webkitSpeechRecognition"]) as
      | SpeechRecognitionConstructor
      | undefined;

    if (!API) {
      console.warn("[VoiceInput] Web Speech API not available in this browser.");
      return;
    }

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
        if (result.isFinal) {
          final += transcript;
        } else {
          interim += transcript;
        }
      }

      if (final) {
        this.onTranscript?.(final.trim(), true);
      } else if (interim) {
        this.onTranscript?.(interim.trim(), false);
      }
    };

    this.recognition.onend = () => {
      this._isRecording = false;
      this.onStop?.();
    };

    this.recognition.onerror = (event: { error: string }) => {
      // Permanent failures: null out recognition so isAvailable → false
      // and the mic button hides itself.
      const permanent = new Set(["service-not-allowed", "not-allowed", "audio-capture"]);
      const userMessages: Record<string, string> = {
        "not-allowed":        "Microphone access denied. Check your browser/OS permissions.",
        "service-not-allowed": "Speech recognition requires HTTPS. Mic unavailable over HTTP.",
        "audio-capture":      "No microphone detected.",
        "network":            "Network error during speech recognition.",
      };
      const msg = userMessages[event.error];
      if (msg) {
        this.onError?.(msg);
      } else if (event.error !== "no-speech" && event.error !== "aborted") {
        console.warn("[VoiceInput] Recognition error:", event.error);
      }
      this._isRecording = false;
      if (permanent.has(event.error)) {
        this.recognition = null;  // disables isAvailable; IOBar will hide the button
      }
      this.onStop?.();
    };
  }

  /**
   * Start recording.
   * Explicitly requests microphone permission first (required on macOS/Safari).
   * Returns `false` if the API is unavailable or permission is denied.
   */
  async start(): Promise<boolean> {
    if (!this.recognition || this._isRecording) return false;

    // Explicitly trigger the permission dialog before starting recognition.
    // On macOS, SpeechRecognition alone may fail silently without this.
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      // SpeechRecognition manages its own audio pipeline; stop the test stream.
      stream.getTracks().forEach((t) => t.stop());
    } catch {
      this.onError?.("Microphone access denied. Check your browser/OS permissions.");
      return false;
    }

    this.recognition.start();
    this._isRecording = true;
    return true;
  }

  /** Stop recording. */
  stop(): void {
    if (!this.recognition || !this._isRecording) return;
    this.recognition.stop();
    this._isRecording = false;
  }

  get isRecording(): boolean {
    return this._isRecording;
  }

  get isAvailable(): boolean {
    return this.recognition !== null;
  }
}
