/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Which engine turns the dashboard microphone into text.
 *
 * - `browser`: the browser's own Web Speech recognition (Chrome, Edge). No
 *   server setup, but the audio goes to the browser vendor's service.
 * - `server`: record here, transcribe through `/api/stt` with the recognizer the
 *   server is configured for (Deepgram, or faster-whisper on the machine).
 *
 * The person picks `auto`, one of the two, or `off`; `auto` prefers the server
 * when it is configured, because then the deployment chose where speech goes.
 * Reachy's own microphone is separate and configured on the server.
 */
import { safeStorage } from "../safeStorage";
import { emit } from "../events";
import { WebSpeech } from "./WebSpeech";
import { SpeechToText } from "./SpeechToText";

/** What the person chose in the audio settings. */
export type VoiceMode = "auto" | "browser" | "server" | "off";

/** The engine a click on the mic actually uses. */
export type VoiceEngine = "browser" | "server";

/** Where the choice is kept. */
export const VOICE_MODE_KEY = "wactorz-voice-input";

/** Where the server's answer to "can you transcribe?" is seeded from `/api/config`. */
export const STT_AVAILABLE_KEY = "wactorz-stt-available";

/** Every mode, with the label the settings show for it. */
export const VOICE_MODES: readonly { mode: VoiceMode; label: string }[] = [
    { mode: "auto", label: "Auto" },
    { mode: "browser", label: "Browser (Chrome, Edge)" },
    { mode: "server", label: "Server (Deepgram or Whisper)" },
    { mode: "off", label: "Off" },
];

/** What this browser and server can each do. */
export interface VoiceSupport {
    /** The server's recognizer is configured and installed. */
    server: boolean;
    /** The browser has Web Speech recognition. */
    browser: boolean;
    /** The browser can record audio to upload. */
    recorder: boolean;
}

function isMode(value: string | null): value is VoiceMode {
    return VOICE_MODES.some(entry => entry.mode === value);
}

/** The saved choice, `auto` when nothing valid is saved. */
export function voiceMode(): VoiceMode {
    const saved = safeStorage.get(VOICE_MODE_KEY);
    return isMode(saved) ? saved : "auto";
}

/** Save the choice and tell the composer, so the mic appears or goes at once. */
export function setVoiceMode(mode: VoiceMode): void {
    safeStorage.set(VOICE_MODE_KEY, mode);
    emit("af-voice-mode", { mode });
}

/** What this browser and the server can do right now. */
export function currentSupport(): VoiceSupport {
    return {
        server: safeStorage.get(STT_AVAILABLE_KEY) === "1",
        browser: WebSpeech.isSupported(),
        recorder: SpeechToText.isSupported(),
    };
}

/** The engine a mode resolves to, or null when it cannot work here. */
export function resolveVoiceEngine(mode: VoiceMode, support: VoiceSupport): VoiceEngine | null {
    const server = support.server && support.recorder;
    switch (mode) {
        case "off":
            return null;
        case "browser":
            return support.browser ? "browser" : null;
        case "server":
            return server ? "server" : null;
        default:
            return server ? "server" : support.browser ? "browser" : null;
    }
}

/** Whether to show the mic at all: some engine could work and it is not off. */
export function micOffered(mode: VoiceMode, support: VoiceSupport): boolean {
    return mode !== "off" && (support.browser || support.recorder);
}

/** Why a mode cannot be used here, in words the person can act on. */
export function unavailableReason(mode: VoiceMode, support: VoiceSupport): string {
    if (mode === "browser" || (mode === "auto" && !support.recorder)) {
        return "This browser has no speech recognition. Use Chrome or Edge, opened as localhost or over HTTPS, or switch voice input to Server.";
    }
    if (!support.recorder) {
        return "This browser cannot record audio. Open the dashboard as localhost or over HTTPS.";
    }
    return "The server has no speech recognizer configured. Set DEEPGRAM_API_KEY (or REACHY_STT_BACKEND=faster-whisper) and restart Wactorz, or switch voice input to Browser.";
}
