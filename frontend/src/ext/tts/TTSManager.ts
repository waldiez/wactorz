/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * TTSManager — notification sound + TTS for incoming agent messages.
 *
 * Two modes, independently toggled:
 *   beep  — short AudioContext tone on each incoming message
 *   tts   — replies read aloud, by the server or by this browser
 *
 * Which of those speaks follows the branch the deployment names, and the server
 * is asked with POST /api/tts {text, voice}. The audio comes back in whatever
 * form made it, which is decoded by sniffing rather than by its type. A 503 says
 * this deployment will not speak, and window.speechSynthesis covers the rest of
 * the session.
 *
 * Persistence: toggle state and selected voice are stored in localStorage.
 */

import { safeStorage } from "../../safeStorage";
import { emit } from "../../events";
import type { TTSVoice } from "./types";

const LS_BEEP = "wactorz.beep";
const LS_TTS = "wactorz.tts";
const LS_VOICE = "wactorz.ttsVoice";

export class TTSManager {
    private _beepEnabled: boolean;
    private _ttsEnabled: boolean;
    private _audioCtx: AudioContext | null = null;
    /** null = unknown, true = server responded ok, false = unavailable (503/network) */
    private _serverAvailable: boolean | null = null;
    private _mode: "off" | "browser" | "server" | "host" = "server";
    private _voices: TTSVoice[] = [];
    /** API base — empty for plain web, ingress prefix behind HA.
     *  Must be set (main.ts) before init(); bare "/api/…" escapes the ingress prefix. */
    private _apiBase = "";

    constructor() {
        this._beepEnabled = safeStorage.get(LS_BEEP) !== "0";
        this._ttsEnabled = safeStorage.get(LS_TTS) === "1";
    }

    /** Set the API base (plain-relative or ingress prefix). Call before init(). */
    setApiBase(base: string): void {
        this._apiBase = base;
    }

    /** Which branch this deployment speaks through. */
    setMode(mode: "off" | "browser" | "server" | "host"): void {
        this._mode = mode;
    }

    /** Whether the server will make speech for this browser to play. */
    setServerAvailable(available: boolean): void {
        this._serverAvailable = available;
    }

    /**
     * Load whatever voices there are to choose between.
     *
     * Which list that is follows the branch, not what happens to answer: a
     * deployment speaking through its own service has voices that are the
     * service's, and offering this browser's instead would send it a name it
     * has never heard of.
     *
     * Call once after the page loads -- non-blocking.
     */
    async init(): Promise<void> {
        if (this._mode === "off" || this._mode === "host") {
            return;
        }
        // Also when the server cannot speak: this browser covers for it then,
        // and it needs its own voices to do that with.
        if (this._mode === "browser" || this._serverAvailable === false) {
            await this._loadBrowserVoices();
            return;
        }
        if (!(await this._loadServerVoices())) {
            await this._loadBrowserVoices();
        }
    }

    /**
     * Take the voice list the server offers, which may be empty.
     *
     * An empty list means there is no choice to make -- a named synthesiser
     * speaks in whatever it is configured for -- and is not the same as a server
     * that cannot speak. Reading it as the latter would hand the words to this
     * browser while the service sat there working.
     */
    private async _loadServerVoices(): Promise<boolean> {
        try {
            const res = await fetch(`${this._apiBase}/api/tts/voices`);
            if (res.ok) {
                const data: unknown = await res.json();
                if (Array.isArray(data)) {
                    this._voices = data as TTSVoice[];
                    this._emitVoices();
                    return true;
                }
            }
        } catch {
            /* network error */
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
                return true;
            };

            // Listened to however many times it fires, and whether or not there
            // is a list already: the first answer is often just the voices
            // installed on the machine, with the rest arriving a moment later.
            // Taking the first answer as the whole list loses everything after it.
            synth.addEventListener("voiceschanged", () => {
                if (populate()) {
                    resolve();
                }
            });
            if (populate()) {
                resolve();
            } else {
                setTimeout(resolve, 2000); // give up gracefully if it never fires
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
    /** Whether the server will make speech for this browser to play. */
    get serverAvailable(): boolean {
        return this._serverAvailable === true;
    }
    /** The voices there are to choose between, which may be none. */
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

    /** Toggle spoken TTS (persisted), cancelling any in-progress speech when turning off; returns the new state. */
    toggleTTS(): boolean {
        this._ttsEnabled = !this._ttsEnabled;
        safeStorage.set(LS_TTS, this._ttsEnabled ? "1" : "0");
        if (!this._ttsEnabled) {
            window.speechSynthesis?.cancel();
        }
        return this._ttsEnabled;
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
        // `off` wants silence and `host` is answered through the server's own
        // speakers, so in both this browser saying it too is one voice too many.
        if (this._mode === "off" || this._mode === "host") {
            return;
        }
        const excerpt = text.replace(/```[\s\S]*?```/g, "code block").slice(0, 300);
        // `browser` is a deployment saying the words stay on this machine, so it
        // is never a fallback here -- it is the whole instruction.
        if (this._mode === "browser") {
            this._speakBrowser(excerpt);
            return;
        }
        if (this._serverAvailable !== false) {
            this._speakServer(excerpt);
        } else {
            this._speakBrowser(excerpt);
        }
    }

    private _speakServer(text: string): void {
        // Emit audio-start so AmbientManager ducks; every return path must emit
        // audio-end or ambient stays ducked.
        emit("tts-audio-start");
        const unduck = () => emit("tts-audio-end");
        // Only when there is a list it came from: with a named synthesiser the
        // stored name is one this browser was offered by some other deployment,
        // and sending it asks for a voice the service has never heard of.
        const voice = this._voices.length ? this.selectedVoice : "";

        // POST, not GET: synthesis is work, not a read. A GET can be fired
        // cross-origin with no Origin header at all (`<img src>`), which is the
        // assumption the server's Origin check rests on.
        fetch(`${this._apiBase}/api/tts`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(voice ? { text, voice } : { text }),
        })
            .then(res => {
                if (res.status === 503 || res.status === 404) {
                    this._serverAvailable = false;
                    unduck();
                    this._speakBrowser(text);
                    return null;
                }
                if (!res.ok) {
                    unduck();
                    return null;
                }
                this._serverAvailable = true;
                return res.arrayBuffer();
            })
            .then(buf => {
                if (!buf) {
                    return;
                }
                // Decode + play through AudioContext (already unlocked by beep gesture)
                const ctx = this._ctx();
                if (!ctx) {
                    unduck();
                    this._speakBrowser(text);
                    return;
                }
                ctx.decodeAudioData(buf)
                    .then(decoded => {
                        const src = ctx.createBufferSource();
                        src.buffer = decoded;
                        src.connect(ctx.destination);
                        src.onended = unduck;
                        src.start();
                    })
                    .catch(() => {
                        unduck();
                        this._speakBrowser(text);
                    });
            })
            .catch(() => {
                this._serverAvailable = false;
                unduck();
                this._speakBrowser(text);
            });
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
