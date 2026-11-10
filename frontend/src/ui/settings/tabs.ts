/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Settings → Tabs: which tabs the header and the phone's bar show in this
 * browser, and in what order.
 *
 * The ones the dashboard cannot do without are listed too, ticked and fixed, so
 * the list is the whole header and nobody looks for a switch that is not there.
 *
 * A tab moves one place at a time, by its own buttons rather than by dragging,
 * so it moves the same way from a keyboard and a screen reader; the focus stays
 * on the button pressed, so pressing it again carries on.
 */
import { canHide, DEVICES_TAB, hiddenTabs, setTabHidden } from "../../config/hiddenTabs";
import { canMove, inTabOrder, moveTab, resetTabOrder, tabOrder } from "../../config/tabOrder";
import { button, el, iconButton, named } from "../dom";
import { iconMarkup } from "../shell/icons";
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
    /** Every tab the header can show, in the dashboard's own order. */
    tabs(): TabEntry[];
    /** Whether Home Assistant is set up, so the Devices link appears at all. */
    haConfigured(): boolean;
    /** A tab was hidden, shown or moved: draw the header again. */
    changed(): void;
}

/** A tab's two move buttons, to give the focus back after the list is drawn again. */
interface MoveButtons {
    up: HTMLButtonElement;
    down: HTMLButtonElement;
}

/** What a tab's row says beside its name: why it is fixed, where it stays, or when it shows. */
function tabNote(key: string, haConfigured: boolean): string {
    if (key === "settings") {
        return "always shown, always last";
    }
    if (!canHide(key)) {
        return "always shown";
    }
    if (key === DEVICES_TAB) {
        return haConfigured ? "after the tabs" : "after the tabs, once Home Assistant is set up";
    }
    return "";
}

/** A tab's switch, ticked when it shows, with its name and note. */
function tabSwitch(entry: TabEntry, hidden: ReadonlySet<string>, host: TabsSectionHost): HTMLElement {
    const label = el("label", "af-settings-check");
    const box = named(el("input"), `tab-${entry.key}`, `Show the ${entry.label} tab`);
    box.type = "checkbox";
    box.checked = !hidden.has(entry.key);
    box.disabled = !canHide(entry.key);
    box.addEventListener("change", () => {
        setTabHidden(entry.key, !box.checked);
        host.changed();
    });
    label.append(box, el("span", "af-settings-check-label", entry.label));
    const note = tabNote(entry.key, host.haConfigured());
    if (note) {
        label.appendChild(el("span", "af-settings-check-note", note));
    }
    return label;
}

/** A move button: one place towards the start (`-1`) or the end (`1`). */
function moveButton(entry: TabEntry, by: -1 | 1, atEnd: boolean, move: () => void): HTMLButtonElement {
    const label = `Move ${entry.label} ${by < 0 ? "up" : "down"}`;
    const btn = iconButton(
        "af-mini-btn af-settings-move",
        label,
        iconMarkup(by < 0 ? "chevron-up" : "chevron-down", 14),
    );
    btn.disabled = atEnd;
    btn.addEventListener("click", move);
    return btn;
}

/** The Tabs section's content: a switch per tab, the order of those that move, and what hiding one means. */
export function buildTabsSection(host: TabsSectionHost): HTMLElement {
    const body = el("div", "af-settings-body");
    const list = el("div", "af-settings-checks");
    const reset = button("af-mini-btn", "Default order");
    const moves = new Map<string, MoveButtons>();

    /** Draw the list from what is stored now; then focus `after`'s button, if a move asked for it. */
    const draw = (after?: { key: string; by: -1 | 1 }): void => {
        const hidden = hiddenTabs();
        const all = host.tabs();
        const ordered = inTabOrder(
            all.filter(t => canMove(t.key)),
            tabOrder(),
        );
        const shown = ordered.map(t => t.key);
        moves.clear();
        const rows = ordered.map((entry, at) => {
            const step = (by: -1 | 1) => (): void => {
                moveTab(shown, entry.key, by);
                host.changed();
                draw({ key: entry.key, by });
            };
            const pair = {
                up: moveButton(entry, -1, at === 0, step(-1)),
                down: moveButton(entry, 1, at === ordered.length - 1, step(1)),
            };
            moves.set(entry.key, pair);
            const row = el("div", "af-settings-tab-row");
            const arrows = el("span", "af-settings-moves");
            arrows.append(pair.up, pair.down);
            row.append(tabSwitch(entry, hidden, host), arrows);
            return row;
        });
        const fixed = all
            .filter(t => !canMove(t.key))
            .map(entry => {
                const row = el("div", "af-settings-tab-row");
                row.appendChild(tabSwitch(entry, hidden, host));
                return row;
            });
        list.replaceChildren(...rows, ...fixed);
        reset.hidden = tabOrder().length === 0;
        if (after) {
            const pair = moves.get(after.key);
            const pressed = after.by < 0 ? pair?.up : pair?.down;
            // At an end the button pressed is disabled now: its partner keeps the focus in the row.
            (pressed?.disabled ? (after.by < 0 ? pair?.down : pair?.up) : pressed)?.focus();
        }
    };

    reset.addEventListener("click", () => {
        resetTabOrder();
        host.changed();
        draw();
        list.querySelector<HTMLElement>("input:not(:disabled), button:not(:disabled)")?.focus();
    });
    draw();

    const actions = el("div", "af-settings-actions");
    actions.appendChild(reset);
    body.append(
        list,
        actions,
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
        line: "Which tabs the header shows, and their order",
        scope: "browser",
        build: () => buildTabsSection(host),
    };
}
