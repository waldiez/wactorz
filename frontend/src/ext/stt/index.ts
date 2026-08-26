/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * STT extension — frontend module.
 *
 * Mirrors ``wactorz/ext/stt/`` on the backend. Two separate questions decide
 * whether a microphone appears, and both are the server's to answer: which
 * branch this deployment offers, and — for the branches that need one — whether
 * a recogniser is actually reachable. A deployment configured for recognition
 * whose recogniser is missing offers no microphone, rather than one that fails
 * on every use.
 */

import { registerConfigEntry } from "../../config/serverConfig";
import { safeStorage } from "../../safeStorage";
import { SpeechToText } from "./Recorder";

export { SpeechToText } from "./Recorder";
export { toWav, encodeWav, resample, toMono, TARGET_RATE } from "./wav";

/** Which speech-to-text branch the deployment offers. Mirrors `STT_MODES`. */
export type SttMode = "off" | "browser" | "server" | "host";

/** Where the seeded branch is kept. */
export const STT_KEY = "wactorz-stt-mode";

/** Where the seeded recogniser reachability is kept. */
export const STT_AVAILABLE_KEY = "wactorz-stt-available";

// Registered at module load so seedServerConfig() picks them up. The branch is
// core's field and reachability is this extension's, but both arrive under the
// key named after the extension, which is why the backend merges rather than
// replaces there.
registerConfigEntry(STT_KEY, c => (c.stt as Record<string, unknown> | undefined)?.mode as string | undefined);
registerConfigEntry(STT_AVAILABLE_KEY, c =>
    (c.stt as Record<string, unknown> | undefined)?.available ? "1" : "0",
);

/**
 * The branch this deployment offers.
 *
 * An unrecognised value is treated as `off`, so a browser that has not been
 * taught a newer branch offers nothing rather than a control it cannot drive.
 */
export function sttMode(): SttMode {
    const stored = safeStorage.get(STT_KEY);
    return stored === "browser" || stored === "server" || stored === "host" ? stored : "off";
}

/** Whether the server can actually reach a recogniser. */
export function sttAvailable(): boolean {
    return safeStorage.get(STT_AVAILABLE_KEY) === "1";
}

/**
 * Whether the composer's microphone button can be offered.
 *
 * `server` is the one branch this button drives: the recorder captures here and
 * sends the clip to the recogniser, so it needs a browser that can record and a
 * recogniser the server can reach. The other branches are named by config and
 * supplied by whatever implements them -- `browser` transcribes on the client
 * and has no transcriber yet, and `host` records server-side and is driven by a
 * control message rather than by this button. Offering it for either would send
 * audio somewhere the chosen branch says it should not go.
 */
export function micOffered(): boolean {
    return sttMode() === "server" && sttAvailable() && SpeechToText.isSupported();
}
