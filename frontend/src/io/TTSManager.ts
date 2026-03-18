/**
 * TTSManager — notification sound + browser TTS for incoming agent messages.
 *
 * Two modes, independently toggled:
 *   beep  — short AudioContext tone on each incoming message
 *   tts   — Web Speech API reads the message content aloud
 *
 * Both require a prior user gesture (browser autoplay policy) and HTTPS (or
 * localhost).  The manager degrades silently when neither is available.
 *
 * Persistence: mute state is stored in localStorage so it survives page reloads.
 */

const LS_BEEP = "wactorz.beep";
const LS_TTS = "wactorz.tts";

/** Patterns that indicate the user wants the reply spoken aloud. */
const SPEAK_REQUEST =
  /\b(speak|read|say|tell me|voice|out ?loud|aloud|read ?(it|that|this) ?out)\b/i;

export class TTSManager {
  private _beepEnabled: boolean;
  private _ttsEnabled: boolean;
  private _forceNext = false; // speak the very next reply regardless of toggle
  private _audioCtx: AudioContext | null = null;

  constructor() {
    this._beepEnabled = localStorage.getItem(LS_BEEP) !== "0";
    this._ttsEnabled = localStorage.getItem(LS_TTS) === "1";
  }

  /**
   * Call with the user's outgoing message text.
   * If it contains a speech request, the next reply will be spoken once
   * even if the TTS toggle is off.
   */
  checkUserIntent(text: string): void {
    if (SPEAK_REQUEST.test(text)) this._forceNext = true;
  }

  get beepEnabled(): boolean {
    return this._beepEnabled;
  }
  get ttsEnabled(): boolean {
    return this._ttsEnabled;
  }

  toggleBeep(): boolean {
    this._beepEnabled = !this._beepEnabled;
    localStorage.setItem(LS_BEEP, this._beepEnabled ? "1" : "0");
    return this._beepEnabled;
  }

  toggleTTS(): boolean {
    this._ttsEnabled = !this._ttsEnabled;
    localStorage.setItem(LS_TTS, this._ttsEnabled ? "1" : "0");
    if (!this._ttsEnabled) window.speechSynthesis?.cancel();
    return this._ttsEnabled;
  }

  /** Call on incoming agent message. Beeps and/or speaks depending on settings. */
  notify(text: string, _from?: string): void {
    if (this._beepEnabled) this._beep();
    if (this._ttsEnabled || this._forceNext) {
      this._forceNext = false;
      this._speak(text);
    }
  }

  // ── Private ──────────────────────────────────────────────────────────────────

  private _ctx(): AudioContext | null {
    if (!this._audioCtx) {
      try {
        this._audioCtx = new AudioContext();
      } catch {
        return null;
      }
    }
    // Resume if suspended (autoplay policy)
    if (this._audioCtx.state === "suspended") {
      this._audioCtx.resume().catch(() => {});
    }
    return this._audioCtx;
  }

  private _beep(freq = 880, durationMs = 80, gain = 0.08): void {
    const ctx = this._ctx();
    if (!ctx) return;
    try {
      const osc = ctx.createOscillator();
      const vol = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      vol.gain.value = gain;
      osc.connect(vol);
      vol.connect(ctx.destination);
      const t = ctx.currentTime;
      osc.start(t);
      // Fade out to avoid a click
      vol.gain.setTargetAtTime(0, t + durationMs * 0.001 * 0.6, 0.01);
      osc.stop(t + durationMs * 0.001 + 0.05);
    } catch {
      // AudioContext blocked — silently ignore
    }
  }

  private _speak(text: string): void {
    const synth = window.speechSynthesis;
    if (!synth) return;
    // Truncate very long messages to avoid reading walls of text
    const excerpt = text.replace(/```[\s\S]*?```/g, "code block").slice(0, 300);
    const utt = new SpeechSynthesisUtterance(excerpt);
    utt.rate = 1.1;
    utt.pitch = 1.0;
    utt.volume = 0.9;
    synth.speak(utt);
  }
}

export const tts = new TTSManager();
