/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The order this browser shows the view tabs in, the header's and the phone's
 * More sheet alike.
 *
 * Settings is not in it: it stays last, so the way to change any of this is
 * always at the same end. Nor is the Devices link, which is a link out to Home
 * Assistant drawn after the tabs, not one of them.
 *
 * A tab the stored order does not name (one an extension added since, or one
 * that was never moved) goes after the ones it does, in the order the
 * dashboard gives it; so does every tab until someone moves one.
 */
import { safeStorage } from "../safeStorage";
import { DEVICES_TAB, hiddenTabs } from "./hiddenTabs";
import { TAB_ORDER_KEY } from "./keys";

/** Whether the tab `key` takes part in the order: every view's, but Settings's. */
export function canMove(key: string): boolean {
    return key !== "settings" && key !== DEVICES_TAB;
}

/** The order this browser keeps, as view keys; empty when none was chosen. */
export function tabOrder(): string[] {
    let stored: unknown;
    try {
        stored = JSON.parse(safeStorage.get(TAB_ORDER_KEY) ?? "[]");
    } catch {
        // Edited by hand, or by another version: the dashboard's own order rather than a failure.
        return [];
    }
    if (!Array.isArray(stored)) {
        return [];
    }
    return [...new Set(stored.filter((key): key is string => typeof key === "string" && canMove(key)))];
}

/** `tabs` in `order`: the ones it names first, as it names them, then the rest as they came. */
export function inTabOrder<T extends { key: string }>(tabs: readonly T[], order: readonly string[]): T[] {
    const rank = new Map(order.map((key, at) => [key, at]));
    const place = (key: string, at: number): number => rank.get(key) ?? order.length + at;
    return tabs
        .map((tab, at) => ({ tab, at: place(tab.key, at) }))
        .sort((a, b) => a.at - b.at)
        .map(({ tab }) => tab);
}

/**
 * Move the tab `key` one place in `shown`, the order the tabs are shown in
 * now: `-1` towards the start, `1` towards the end. Keeps the whole order, so
 * the tabs keep their places from then on; a move past either end changes nothing.
 */
export function moveTab(shown: readonly string[], key: string, by: -1 | 1): void {
    const order = shown.filter(canMove);
    const from = order.indexOf(key);
    const to = from + by;
    if (from < 0 || to < 0 || to >= order.length) {
        return;
    }
    [order[from], order[to]] = [order[to]!, order[from]!];
    safeStorage.set(TAB_ORDER_KEY, JSON.stringify(order));
}

/** Go back to the dashboard's own order. */
export function resetTabOrder(): void {
    safeStorage.remove(TAB_ORDER_KEY);
}

/** What this browser chose for its tabs, as the header and the phone's bar take it. */
export function tabChoices(): { hidden: Set<string>; order: string[] } {
    return { hidden: hiddenTabs(), order: tabOrder() };
}
