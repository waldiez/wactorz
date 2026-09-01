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

import { canCaptureLive } from "./liveCapture";
import { canRecognise } from "./webSpeech";
import { registerConfigEntry } from "../../config/serverConfig";
import { safeStorage } from "../../safeStorage";
import { SpeechToText } from "./Recorder";

export { SpeechToText } from "./Recorder";
export { LiveMic, attachLiveSocket, liveMic } from "./LiveMic";
export { WebSpeech, canRecognise } from "./webSpeech";
export type { WebSpeechHandlers } from "./webSpeech";
export type { LiveSocket, LiveMicHandlers } from "./LiveMic";
export { toWav, encodeWav, resample, toMono, TARGET_RATE } from "./wav";

/** Which speech-to-text branch the deployment offers. Mirrors `STT_MODES`. */
export type SttMode = "off" | "browser" | "server" | "host";

/** Where the seeded branch is kept. */
export const STT_KEY = "wactorz-stt-mode";

/** Where the seeded recogniser reachability is kept. */
export const STT_AVAILABLE_KEY = "wactorz-stt-available";

/** Where the seeded recogniser kind is kept. */
export const STT_LIVE_KEY = "wactorz-stt-live";

// Registered at module load so seedServerConfig() picks them up. The branch is
// core's field and reachability is this extension's, but both arrive under the
// key named after the extension, which is why the backend merges rather than
// replaces there.
registerConfigEntry(STT_KEY, c => (c.stt as Record<string, unknown> | undefined)?.mode as string | undefined);
registerConfigEntry(STT_AVAILABLE_KEY, c =>
    (c.stt as Record<string, unknown> | undefined)?.available ? "1" : "0",
);
registerConfigEntry(STT_LIVE_KEY, c => ((c.stt as Record<string, unknown> | undefined)?.live ? "1" : "0"));

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

/** Whether that recogniser returns words while the person is still speaking. */
export function sttLive(): boolean {
    return safeStorage.get(STT_LIVE_KEY) === "1";
}

/**
 * Whether the microphone can show words as they are spoken.
 *
 * Both halves have to agree: a recogniser that streams is no use to a browser
 * that cannot deliver frames, and vice versa. When either says no the button
 * still works -- it records the whole utterance and sends it at the end.
 */
export function liveOffered(): boolean {
    // Not on `browser`: the browser's own recogniser has its own way of
    // returning words as they are said, and does not need audio streamed
    // anywhere to do it.
    return sttMode() === "server" && micOffered() && sttLive() && canCaptureLive();
}

/** Whether this browser does the recognising itself. */
export function recognisesHere(): boolean {
    return sttMode() === "browser" && canRecognise();
}

/**
 * Whether the composer's microphone button can be offered.
 *
 * Two branches drive this button, and each needs something different to be true.
 * `server` captures here and sends the clip on, so it wants a browser that can
 * record and a recogniser the server can reach. `browser` never sends the audio
 * anywhere this deployment can see, so it needs nothing of the server -- only a
 * browser that recognises speech on a page trusted with a microphone, which is
 * Chromium over localhost or TLS and nothing else.
 *
 * `host` records on the machine and is asked to by a control message rather than
 * by this button, so it is offered nothing here.
 */
export function micOffered(): boolean {
    if (sttMode() === "browser") {
        return canRecognise();
    }
    return sttMode() === "server" && sttAvailable() && SpeechToText.isSupported();
}
