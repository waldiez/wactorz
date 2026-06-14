/**
 * TTSManager — notification sound + TTS for incoming agent messages.
 *
 * Two modes, independently toggled:
 *   beep  — short AudioContext tone on each incoming message
 *   tts   — speech synthesis (server edge-tts when available, Web Speech API fallback)
 *
 * Server TTS: GET /api/tts?text=...&voice=...  returns audio/mpeg.
 * If the endpoint returns 503 (edge-tts not installed) the manager falls back
 * to window.speechSynthesis for the rest of the session.
 *
 * Persistence: toggle state and selected voice are stored in localStorage.
 */

const LS_BEEP  = "wactorz.beep";
const LS_TTS   = "wactorz.tts";
const LS_VOICE = "wactorz.ttsVoice";

export interface TTSVoice {
  name: string;
  locale: string;
  gender: string;
}

export class TTSManager {
  private _beepEnabled: boolean;
  private _ttsEnabled: boolean;
  private _audioCtx: AudioContext | null = null;
  /** null = unknown, true = server responded ok, false = unavailable (503/network) */
  private _serverAvailable: boolean | null = null;
  private _voices: TTSVoice[] = [];
  /** API base — empty for plain web, ingress prefix behind HA, localhost in Tauri.
   *  Must be set (main.ts) before init(); bare "/api/…" escapes the ingress prefix. */
  private _apiBase = "";

  constructor() {
    this._beepEnabled = localStorage.getItem(LS_BEEP) !== "0";
    this._ttsEnabled  = localStorage.getItem(LS_TTS)  === "1";
  }

  /** Set the API base (ingress prefix / Tauri host). Call before init(). */
  setApiBase(base: string): void {
    this._apiBase = base;
  }

  /**
   * Probe the server for edge-tts availability and load the voice list.
   * Falls back to browser voices if the server has none.
   * Call once after the page loads — non-blocking.
   */
  async init(): Promise<void> {
    const serverOk = await this._checkServer();
    if (!serverOk) await this._loadBrowserVoices();
  }

  private async _checkServer(): Promise<boolean> {
    try {
      const res = await fetch(`${this._apiBase}/api/tts/voices`);
      if (res.ok) {
        const data = await res.json();
        if (Array.isArray(data) && data.length > 0) {
          this._voices = data as TTSVoice[];
          this._serverAvailable = true;
          this._emitVoices();
          return true;
        }
      }
    } catch { /* network error */ }
    this._serverAvailable = false;
    return false;
  }

  private _loadBrowserVoices(): Promise<void> {
    return new Promise(resolve => {
      const synth = window.speechSynthesis;
      if (!synth) { resolve(); return; }

      const populate = (): boolean => {
        const voices = synth.getVoices();
        if (!voices.length) return false;
        this._voices = voices.map(v => ({ name: v.name, locale: v.lang, gender: "" }));
        this._emitVoices();
        resolve();
        return true;
      };

      if (!populate()) {
        synth.addEventListener("voiceschanged", () => populate(), { once: true });
        setTimeout(resolve, 2000); // give up gracefully if event never fires
      }
    });
  }

  private _emitVoices(): void {
    document.dispatchEvent(
      new CustomEvent("tts-voices-loaded", { detail: { voices: this._voices } }),
    );
  }

  get beepEnabled():    boolean    { return this._beepEnabled; }
  get ttsEnabled():     boolean    { return this._ttsEnabled; }
  get serverAvailable(): boolean   { return this._serverAvailable === true; }
  get voices():          TTSVoice[] { return this._voices; }

  get selectedVoice(): string {
    return localStorage.getItem(LS_VOICE) ?? "";
  }

  setVoice(name: string): void {
    localStorage.setItem(LS_VOICE, name);
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
    // TTS off means no speech — full stop. There is deliberately no keyword
    // "intent" override; speaking only ever happens when the toggle is on.
    if (this._ttsEnabled) {
      this._speak(text);
    }
  }

  // ── Private ──────────────────────────────────────────────────────────────────

  private _ctx(): AudioContext | null {
    if (!this._audioCtx) {
      try { this._audioCtx = new AudioContext(); } catch { return null; }
    }
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
      vol.gain.setTargetAtTime(0, t + durationMs * 0.001 * 0.6, 0.01);
      osc.stop(t + durationMs * 0.001 + 0.05);
    } catch {
      // AudioContext blocked — silently ignore
    }
  }

  private _speak(text: string): void {
    const excerpt = text.replace(/```[\s\S]*?```/g, "code block").slice(0, 300);
    if (this._serverAvailable !== false) {
      this._speakServer(excerpt);
    } else {
      this._speakBrowser(excerpt);
    }
  }

  private _speakServer(text: string): void {
    // duck ambient while server audio plays
    import("./AmbientManager").then(({ ambient }) => ambient.duck(true)).catch(() => {});
    const params = new URLSearchParams({ text });
    const voice = this.selectedVoice;
    if (voice) params.set("voice", voice);

    fetch(`${this._apiBase}/api/tts?${params}`)
      .then(res => {
        if (res.status === 503 || res.status === 404) {
          this._serverAvailable = false;
          this._speakBrowser(text);
          return null;
        }
        if (!res.ok) return null;
        this._serverAvailable = true;
        return res.arrayBuffer();
      })
      .then(buf => {
        if (!buf) return;
        // Decode + play through AudioContext (already unlocked by beep gesture)
        const ctx = this._ctx();
        if (!ctx) { this._speakBrowser(text); return; }
        ctx.decodeAudioData(buf).then(decoded => {
          const src = ctx.createBufferSource();
          src.buffer = decoded;
          src.connect(ctx.destination);
          src.onended = () => {
            import("./AmbientManager").then(({ ambient }) => ambient.duck(false)).catch(() => {});
          };
          src.start();
        }).catch(() => {
          import("./AmbientManager").then(({ ambient }) => ambient.duck(false)).catch(() => {});
          this._speakBrowser(text);
        });
      })
      .catch(() => {
        this._serverAvailable = false;
        this._speakBrowser(text);
      });
  }

  private _speakBrowser(text: string): void {
    const synth = window.speechSynthesis;
    if (!synth) return;
    const utt = new SpeechSynthesisUtterance(text);
    utt.rate   = 1.1;
    utt.pitch  = 1.0;
    utt.volume = 0.9;
    const selected = this.selectedVoice;
    if (selected) {
      const match = synth.getVoices().find(v => v.name === selected);
      if (match) utt.voice = match;
    }
    synth.speak(utt);
  }
}

export const tts = new TTSManager();
