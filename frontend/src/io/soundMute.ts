/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Whether this browser is muted: silence for now, over whatever Settings →
 * Sound has chosen.
 *
 * Muting changes none of those choices, so unmuting needs to remember nothing:
 * the choices apply again as they are. It is kept in the browser, so a reload
 * in a quiet room does not bring the sound back.
 *
 * What makes sound asks `isMuted()` before it plays, and hears
 * `af-mute-changed` to stop what is playing.
 */
import { emit } from "../events";
import { safeStorage } from "../safeStorage";

/** Where the browser keeps that it is muted. */
export const MUTED_KEY = "wactorz-muted";

/** Whether this browser is muted. */
export function isMuted(): boolean {
    return safeStorage.get(MUTED_KEY) === "1";
}

/** Mute this browser, or unmute it, and say so when that changes anything. */
export function setMuted(muted: boolean): void {
    if (muted === isMuted()) {
        return;
    }
    if (muted) {
        safeStorage.set(MUTED_KEY, "1");
    } else {
        safeStorage.remove(MUTED_KEY);
    }
    emit("af-mute-changed", { muted });
}
