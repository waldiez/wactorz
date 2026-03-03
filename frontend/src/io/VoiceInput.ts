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
    };

    this.recognition.onerror = (event: { error: string }) => {
      console.warn("[VoiceInput] Recognition error:", event.error);
      this._isRecording = false;
    };
  }

  /** Start recording. Returns `false` if the API is not available. */
  start(): boolean {
    if (!this.recognition || this._isRecording) return false;
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
