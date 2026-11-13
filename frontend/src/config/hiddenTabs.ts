/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The tabs this browser leaves out of the header and the phone's bar.
 *
 * A preference of the person at this screen, so it is kept in the browser and
 * never sent anywhere. Hiding a tab is not access control: the view still opens
 * by its address, and the server serves it as before.
 *
 * What is kept is the hidden ones, not the shown ones, so a tab that is new —
 * an extension installed later — shows until someone hides it.
 */
import { safeStorage } from "../safeStorage";
import { HIDDEN_TABS_KEY } from "./keys";

/** The key the Devices link is hidden by: it is a link to Home Assistant, not a view. */
export const DEVICES_TAB = "devices";

/**
 * The tabs that cannot be hidden: where the dashboard opens, the ways to talk to
 * the agents and to answer what they wait on, and Settings, the only way back.
 */
export const ALWAYS_SHOWN: ReadonlySet<string> = new Set(["overview", "chat", "inbox", "settings"]);

/** Whether the tab `key` may be hidden. */
export function canHide(key: string): boolean {
    return !ALWAYS_SHOWN.has(key);
}

/** The tabs this browser hides; never one of `ALWAYS_SHOWN`, whatever was stored. */
export function hiddenTabs(): Set<string> {
    let stored: unknown;
    try {
        stored = JSON.parse(safeStorage.get(HIDDEN_TABS_KEY) ?? "[]");
    } catch {
        // Edited by hand, or by another version: show everything rather than fail.
        return new Set();
    }
    if (!Array.isArray(stored)) {
        return new Set();
    }
    return new Set(stored.filter((key): key is string => typeof key === "string" && canHide(key)));
}

/** Hide the tab `key` in this browser, or show it again; a tab that cannot be hidden is left shown. */
export function setTabHidden(key: string, hidden: boolean): void {
    const tabs = hiddenTabs();
    if (hidden && canHide(key)) {
        tabs.add(key);
    } else {
        tabs.delete(key);
    }
    if (tabs.size) {
        safeStorage.set(HIDDEN_TABS_KEY, JSON.stringify([...tabs].sort()));
    } else {
        safeStorage.remove(HIDDEN_TABS_KEY);
    }
}
