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
import { tts } from "./TTSManager";

export { tts, TTSManager } from "./TTSManager";
export type { TTSVoice } from "./types";

// This extension's /api/config fields (namespaced under "tts" by the backend
// seam) — registered at module load so seedServerConfig() picks them up.
// NB: no voice key is seeded — speechSynthesis voices are browser-specific,
// so a server-provided default would be meaningless here (voice choice lives
// in the TTSManager's own storage key).
registerConfigEntry("wactorz-tts-available", c =>
    (c.tts as Record<string, unknown> | undefined)?.available ? "1" : "0",
);

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
    if (config.available) {
        void tts.init();
    }
}
