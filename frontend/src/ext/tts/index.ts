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

import { tts } from "./TTSManager";

export { tts, TTSManager } from "./TTSManager";
export type { TTSVoice } from "./types";

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
