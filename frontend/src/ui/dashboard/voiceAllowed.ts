/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Which voice branches this deployment lets Settings choose.
 *
 * The environment is the ceiling: `WACTORZ_STT`, `WACTORZ_TTS` and
 * `WACTORZ_WAKE` are the most each may be set to from a browser. The server
 * says which branches that leaves in `/api/config`, so the page offers what it
 * will accept rather than working the order out for itself.
 */

import { registerConfigEntry } from "../../config/serverConfig";
import { safeStorage } from "../../safeStorage";

/** The settings a ceiling applies to. */
export type BranchSetting = "listening" | "speaking" | "waking";

/** Where each setting's allowed branches are kept, comma-separated. */
export const ALLOWED_KEYS: Record<BranchSetting, string> = {
    listening: "wactorz-voice-allowed-listening",
    speaking: "wactorz-voice-allowed-speaking",
    waking: "wactorz-voice-allowed-waking",
};

/** The variable that sets each ceiling, named so the reader knows what to change. */
const VARIABLES: Record<BranchSetting, string> = {
    listening: "WACTORZ_STT",
    speaking: "WACTORZ_TTS",
    waking: "WACTORZ_WAKE",
};

const allowedIn = (c: Record<string, unknown>, setting: BranchSetting): string | undefined => {
    const voice = c["voice"] as Record<string, unknown> | undefined;
    const list = (voice?.["allowed"] as Record<string, unknown> | undefined)?.[setting];
    return Array.isArray(list) ? list.join(",") : undefined;
};

// Cleared when absent: only the server sets these, and a server that does not
// send them sets no ceiling to show.
for (const setting of Object.keys(ALLOWED_KEYS) as BranchSetting[]) {
    registerConfigEntry(ALLOWED_KEYS[setting], c => allowedIn(c, setting), { clearWhenAbsent: true });
}

/** The branches `setting` may be chosen as, in order, or null when the server has not said. */
export function allowedBranches(setting: BranchSetting): readonly string[] | null {
    const held = safeStorage.get(ALLOWED_KEYS[setting]);
    return held === null || held === "" ? null : held.split(",");
}

/** Whether `branch` may be chosen for `setting`. Anything, when the server has not said. */
export function allows(setting: BranchSetting, branch: string): boolean {
    const allowed = allowedBranches(setting);
    return allowed === null || allowed.includes(branch);
}

/**
 * Why some of `branches` are out of reach, or empty when none are.
 *
 * Said under the picker rather than only on the disabled options, which most
 * browsers show greyed with no way to ask why.
 */
export function ceilingSays(setting: BranchSetting, branches: readonly string[]): string {
    const allowed = allowedBranches(setting);
    if (allowed === null || branches.every(branch => allowed.includes(branch))) {
        return "";
    }
    const most = allowed[allowed.length - 1];
    return `This deployment allows up to "${most}"; ${VARIABLES[setting]} on the machine sets how far.`;
}

/**
 * Whether there is nothing to choose for listening or speaking.
 *
 * Decided by the ceilings rather than by what is in force: a deployment turned
 * down to "off" from this page can still be turned back up, and hiding the
 * section would hide the way back. Without ceilings, by what is in force.
 */
export function nothingToChoose(current: Record<"listening" | "speaking", string>): boolean {
    return (["listening", "speaking"] as const).every(setting => {
        const allowed = allowedBranches(setting);
        return allowed === null ? current[setting] === "off" : allowed.join(",") === "off";
    });
}
