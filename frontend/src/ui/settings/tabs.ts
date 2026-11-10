/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Settings → Tabs: which tabs the header and the phone's bar show in this browser.
 *
 * The ones the dashboard cannot do without are listed too, ticked and fixed, so
 * the list is the whole header and nobody looks for a switch that is not there.
 */
import { canHide, DEVICES_TAB, hiddenTabs, setTabHidden } from "../../config/hiddenTabs";
import { el, named } from "../dom";
import type { SettingsSection } from "./section";

/** A tab the section lists. */
export interface TabEntry {
    /** Its view key, or `DEVICES_TAB` for the link to Home Assistant. */
    key: string;
    /** What the tab says. */
    label: string;
}

/** What the Tabs section needs from its owner. */
export interface TabsSectionHost {
    /** Every tab the header can show, in its order. */
    tabs(): TabEntry[];
    /** Whether Home Assistant is set up, so the Devices link appears at all. */
    haConfigured(): boolean;
    /** A tab was hidden or shown: draw the header again. */
    changed(): void;
}

/** Why a tab's switch is fixed, or when it shows, or nothing. */
function tabNote(key: string, haConfigured: boolean): string {
    if (!canHide(key)) {
        return "always shown";
    }
    return key === DEVICES_TAB && !haConfigured ? "shown once Home Assistant is set up" : "";
}

/** One tab's switch: ticked when it shows. */
function tabRow(entry: TabEntry, hidden: ReadonlySet<string>, host: TabsSectionHost): HTMLElement {
    const row = el("label", "af-settings-check");
    const box = named(el("input"), `tab-${entry.key}`, `Show the ${entry.label} tab`);
    box.type = "checkbox";
    box.checked = !hidden.has(entry.key);
    box.disabled = !canHide(entry.key);
    box.addEventListener("change", () => {
        setTabHidden(entry.key, !box.checked);
        host.changed();
    });
    row.append(box, el("span", "af-settings-check-label", entry.label));
    const note = tabNote(entry.key, host.haConfigured());
    if (note) {
        row.appendChild(el("span", "af-settings-check-note", note));
    }
    return row;
}

/** The Tabs section's content: a switch per tab, and what hiding one means. */
export function buildTabsSection(host: TabsSectionHost): HTMLElement {
    const body = el("div", "af-settings-body");
    const hidden = hiddenTabs();
    const list = el("div", "af-settings-checks");
    host.tabs().forEach(entry => list.appendChild(tabRow(entry, hidden, host)));
    body.append(
        list,
        el(
            "p",
            "af-settings-hint",
            "A hidden tab still opens from a link to it; hiding only keeps it out of the way.",
        ),
    );
    return body;
}

/** The Tabs section of Settings. */
export function tabsSection(host: TabsSectionHost): SettingsSection {
    return {
        key: "tabs",
        label: "Tabs",
        line: "Which tabs the header shows",
        scope: "browser",
        build: () => buildTabsSection(host),
    };
}
