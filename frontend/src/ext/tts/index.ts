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

import { registerConfigEntry } from "../../config/serverConfig";
import { safeStorage } from "../../safeStorage";
import { tts } from "./TTSManager";

export { tts, TTSManager } from "./TTSManager";
export type { TTSVoice } from "./types";

/** Where the seeded branch is kept. */
export const TTS_KEY = "wactorz-tts-mode";

// This extension's /api/config fields (namespaced under "tts" by the backend
// seam) — registered at module load so seedServerConfig() picks them up.
// NB: no voice key is seeded — speechSynthesis voices are browser-specific,
// so a server-provided default would be meaningless here (voice choice lives
// in the TTSManager's own storage key).
registerConfigEntry("wactorz-tts-available", c =>
    (c.tts as Record<string, unknown> | undefined)?.available ? "1" : "0",
);
registerConfigEntry(TTS_KEY, c => (c.tts as Record<string, unknown> | undefined)?.mode as string | undefined);

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
    tts.setApiBase(config.apiBase);
    tts.setMode(ttsMode());
    // Told rather than guessed: whether the server speaks is something it
    // reports, and a synthesiser with one fixed voice offers no list to infer it
    // from. `init` still runs when it does not, to find this browser's voices.
    tts.setServerAvailable(config.available);
    void tts.init();
}
