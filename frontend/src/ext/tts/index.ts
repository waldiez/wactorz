/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * TTS extension — frontend module.
 *
 * Mirrors ``wactorz/ext/tts/`` on the backend: one folder, self-contained
 * feature. Import ``tts`` (the singleton) and call ``register()`` once at
 * startup; the rest of the app talks to it via the event bus.
 */

import { emit } from "../../events";
import { registerConfigEntry } from "../../config/serverConfig";
import { safeStorage } from "../../safeStorage";
import { tts } from "./TTSManager";

export { tts, TTSManager } from "./TTSManager";
export type { TTSVoice } from "./types";

/** Where the seeded branch is kept. */
export const TTS_KEY = "wactorz-tts-mode";

/** Where the voice the deployment speaks in is kept. */
export const TTS_VOICE_KEY = "wactorz-tts-voice";

// This extension's /api/config fields (namespaced under "tts" by the backend
// seam) — registered at module load so seedServerConfig() picks them up.
//
// The seeded voice is the one the *server* will speak in, which is a fact about
// the deployment: on `host` it is what a room hears, and nobody is at a browser
// to have an opinion. It is not the `browser` branch's voice — those come from
// speechSynthesis, differ per browser, and stay in the TTSManager's own key.
registerConfigEntry("wactorz-tts-available", c =>
    (c.tts as Record<string, unknown> | undefined)?.available ? "1" : "0",
);
registerConfigEntry(TTS_KEY, c => (c.tts as Record<string, unknown> | undefined)?.mode as string | undefined);
registerConfigEntry(
    TTS_VOICE_KEY,
    c => (c.tts as Record<string, unknown> | undefined)?.voice as string | undefined,
);

/** How this deployment speaks, if it does. */
export type TtsMode = "off" | "browser" | "server" | "host";

/**
 * The branch this deployment speaks through.
 *
 * An unrecognised value is treated as `server`, which is what an unset one
 * means: speak if the deployment can, and fall back to this browser's own voice
 * when it cannot.
 */
export function ttsMode(): TtsMode {
    const stored = safeStorage.get(TTS_KEY);
    return stored === "off" || stored === "browser" || stored === "host" ? stored : "server";
}

/** The voice this deployment speaks in, empty when the service chooses. */
export function ttsVoice(): string {
    return safeStorage.get(TTS_VOICE_KEY) ?? "";
}

/** Whether the server will make speech for this browser to play. */
export function ttsAvailable(): boolean {
    return safeStorage.get("wactorz-tts-available") === "1";
}

/** Extension config passed to register() once at startup (see main.ts). */
export interface TTSConfig {
    apiBase: string;
    available: boolean;
}

/**
 * Bootstrap the TTS extension. Called once from main.ts during startup.
 * The extension self-wires — no other file needs to know about TTSManager.
 */
export function register(config: TTSConfig): void {
    const mode = ttsMode();
    tts.setApiBase(config.apiBase);
    tts.setMode(mode);
    // Announced because the interface is built before the server has answered:
    // controls for speech would otherwise be drawn on the assumption that this
    // deployment speaks, and stay drawn once it turns out not to.
    emit("tts-mode-known", { speaks: mode !== "off" && mode !== "host" });
    // Told rather than guessed: whether the server speaks is something it
    // reports, and a synthesiser with one fixed voice offers no list to infer it
    // from. `init` still runs when it does not, to find this browser's voices.
    tts.setServerAvailable(config.available);
    void tts.init();
}
