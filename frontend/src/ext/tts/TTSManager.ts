/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * TTSManager — notification sound + TTS for incoming agent messages.
 *
 * Two modes, independently toggled:
 *   beep  — short AudioContext tone on each incoming message
 *   tts   — speech synthesis (server edge-tts when available, Web Speech API fallback)
 *
 * Server TTS: POST /api/tts {text, voice} → audio/mpeg.
 * If the endpoint returns 503 (edge-tts not installed) the manager falls back
 * to window.speechSynthesis for the rest of the session.
 *
 * Persistence: toggle state and selected voice are stored in localStorage.
 *
 * One reply is heard at a time. New speech stops whatever is playing, and a
 * reply whose audio arrives after newer speech started, or after a stop, is
 * dropped rather than played over it.
 */

import { safeStorage } from "../../safeStorage";
import { emit } from "../../events";
import { posting, requestBytes, requestJson } from "../../io/api";
import type { TTSVoice } from "./types";

/** How long synthesis may take: the server waits on a remote voice service. */
const TTS_TIMEOUT_MS = 60_000;

const LS_BEEP = "wactorz.beep";
const LS_TTS = "wactorz.tts";
const LS_VOICE = "wactorz.ttsVoice";

export class TTSManager {
    private _beepEnabled: boolean;
    private _ttsEnabled: boolean;
    private _audioCtx: AudioContext | null = null;
    /** null = unknown, true = server responded ok, false = unavailable (503/network) */
    private _serverAvailable: boolean | null = null;
    private _voices: TTSVoice[] = [];
    /** What is playing from the server, while something is. */
    private _source: AudioBufferSourceNode | null = null;
    /** Moves on whenever speech starts or stops, so audio fetched for older speech
     *  can tell it has been overtaken by the time it arrives. */
    private _generation = 0;
    /** Whether the ambient sound has been told to duck and not yet told to stop. */
    private _ducked = false;
    /** API base — empty for plain web, ingress prefix behind HA.
     *  Must be set (app.ts) before init(); bare "/api/…" escapes the ingress prefix. */
    private _apiBase = "";

    constructor() {
        this._beepEnabled = safeStorage.get(LS_BEEP) !== "0";
        this._ttsEnabled = safeStorage.get(LS_TTS) === "1";
    }

    /** Set the API base (plain-relative or ingress prefix). Call before init(). */
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
        if (!serverOk) {
            await this._loadBrowserVoices();
        }
    }

    private async _checkServer(): Promise<boolean> {
        const got = await requestJson<unknown>(`${this._apiBase}/api/tts/voices`);
        if (got.ok && Array.isArray(got.value) && got.value.length > 0) {
            this._voices = got.value as TTSVoice[];
            this._serverAvailable = true;
            this._emitVoices();
            return true;
        }
        this._serverAvailable = false;
        return false;
    }

    private _loadBrowserVoices(): Promise<void> {
        return new Promise(resolve => {
            const synth = window.speechSynthesis;
            if (!synth) {
                resolve();
                return;
            }

            const populate = (): boolean => {
                const voices = synth.getVoices();
                if (!voices.length) {
                    return false;
                }
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
        emit("tts-voices-loaded", { voices: this._voices });
    }

    /** Whether the notification beep is on. */
    get beepEnabled(): boolean {
        return this._beepEnabled;
    }
    /** Whether spoken TTS is on. */
    get ttsEnabled(): boolean {
        return this._ttsEnabled;
    }
    /** True once the server edge-tts endpoint has been confirmed available. */
    get serverAvailable(): boolean {
        return this._serverAvailable === true;
    }
    /** Available voices (server edge-tts list, or browser voices as a fallback). */
    get voices(): TTSVoice[] {
        return this._voices;
    }

    /** The persisted selected voice name, or "" for the default. */
    get selectedVoice(): string {
        return safeStorage.get(LS_VOICE) ?? "";
    }

    /** Persist the selected voice name. */
    setVoice(name: string): void {
        safeStorage.set(LS_VOICE, name);
    }

    /** Toggle the notification beep (persisted); returns the new state. */
    toggleBeep(): boolean {
        this._beepEnabled = !this._beepEnabled;
        safeStorage.set(LS_BEEP, this._beepEnabled ? "1" : "0");
        return this._beepEnabled;
    }

    /** Toggle spoken TTS (persisted), stopping any speech when turning off; returns the new state. */
    toggleTTS(): boolean {
        this._ttsEnabled = !this._ttsEnabled;
        safeStorage.set(LS_TTS, this._ttsEnabled ? "1" : "0");
        if (!this._ttsEnabled) {
            this.stopSpeaking();
        }
        return this._ttsEnabled;
    }

    /**
     * Stop whatever is being said, from either voice, and anything still on its
     * way from the server. For speech turned off, and for a turn someone stopped:
     * the interface says it stopped, and a page still talking disagrees with it.
     */
    stopSpeaking(): void {
        this._halt();
        this._unduck(this._generation);
    }

    /** Stop what is playing and overtake what is in flight, leaving the ambient sound as it is. */
    private _halt(): void {
        this._generation += 1;
        const playing = this._source;
        this._source = null;
        try {
            playing?.stop();
        } catch {
            // Already ended: nothing left to stop.
        }
        window.speechSynthesis?.cancel();
    }

    private _duck(): void {
        if (!this._ducked) {
            this._ducked = true;
            emit("tts-audio-start");
        }
    }

    /** Let the ambient sound back up, if the speech that ducked it is still the latest. */
    private _unduck(generation: number): void {
        if (generation === this._generation && this._ducked) {
            this._ducked = false;
            emit("tts-audio-end");
        }
    }

    /** Call on incoming agent message. Beeps and/or speaks depending on settings. */
    notify(text: string, _from?: string): void {
        if (this._beepEnabled) {
            this._beep();
        }
        // TTS off means no speech — full stop. There is deliberately no keyword
        // "intent" override; speaking only ever happens when the toggle is on.
        if (this._ttsEnabled) {
            this._speak(text);
        }
    }

    private _ctx(): AudioContext | null {
        if (!this._audioCtx) {
            try {
                this._audioCtx = new AudioContext();
            } catch {
                return null;
            }
        }
        if (this._audioCtx.state === "suspended") {
            this._audioCtx.resume().catch(() => {});
        }
        return this._audioCtx;
    }

    private _beep(freq = 880, durationMs = 80, gain = 0.08): void {
        const ctx = this._ctx();
        if (!ctx) {
            return;
        }
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
        // Stopped, not queued behind: a reply that waits for the one before it
        // to finish is heard long after the message it belongs to.
        this._halt();
        const excerpt = text.replace(/```[\s\S]*?```/g, "code block").slice(0, 300);
        if (this._serverAvailable !== false) {
            this._speakServer(excerpt);
        } else {
            this._speakBrowser(excerpt);
        }
    }

    private _speakServer(text: string): void {
        // Duck the ambient sound; every return path must unduck, or it stays
        // down. Unducking answers only for the latest speech, so a reply that
        // was overtaken cannot bring the sound back up under the one after it.
        this._duck();
        const mine = this._generation;
        const unduck = () => this._unduck(mine);
        const overtaken = () => mine !== this._generation;
        const voice = this.selectedVoice;

        // POST, not GET: synthesis is work, not a read. A GET can be fired
        // cross-origin with no Origin header at all (`<img src>`), which is the
        // assumption the server's Origin check rests on.
        requestBytes(`${this._apiBase}/api/tts`, posting(voice ? { text, voice } : { text }), TTS_TIMEOUT_MS)
            .then(got => {
                if (got.ok) {
                    this._serverAvailable = true;
                    return got.value;
                }
                // Only an answer that says so rules the server out for the session:
                // one slow reply is no reason to change the voice for good.
                const gone =
                    !got.timedOut && (got.status === undefined || got.status === 503 || got.status === 404);
                if (gone) {
                    this._serverAvailable = false;
                }
                unduck();
                if (gone || got.timedOut) {
                    this._fallBack(text, mine);
                }
                return null;
            })
            .then(buf => {
                if (!buf || overtaken()) {
                    return;
                }
                // Decode + play through AudioContext (already unlocked by beep gesture)
                const ctx = this._ctx();
                if (!ctx) {
                    unduck();
                    this._fallBack(text, mine);
                    return;
                }
                ctx.decodeAudioData(buf)
                    .then(decoded => {
                        if (overtaken()) {
                            return;
                        }
                        const src = ctx.createBufferSource();
                        src.buffer = decoded;
                        src.connect(ctx.destination);
                        src.onended = () => {
                            if (this._source === src) {
                                this._source = null;
                            }
                            unduck();
                        };
                        this._source = src;
                        src.start();
                    })
                    .catch(() => {
                        unduck();
                        this._fallBack(text, mine);
                    });
            })
            .catch(() => {
                this._serverAvailable = false;
                unduck();
                this._fallBack(text, mine);
            });
    }

    /** Say it in the browser's own voice instead, unless newer speech or a stop came first. */
    private _fallBack(text: string, generation: number): void {
        if (generation === this._generation) {
            this._speakBrowser(text);
        }
    }

    private _speakBrowser(text: string): void {
        const synth = window.speechSynthesis;
        if (!synth) {
            return;
        }
        const utt = new SpeechSynthesisUtterance(text);
        utt.rate = 1.1;
        utt.pitch = 1.0;
        utt.volume = 0.9;
        const selected = this.selectedVoice;
        if (selected) {
            const match = synth.getVoices().find(v => v.name === selected);
            if (match) {
                utt.voice = match;
            }
        }
        synth.speak(utt);
    }
}

/** Shared TTSManager singleton. */
export const tts = new TTSManager();
