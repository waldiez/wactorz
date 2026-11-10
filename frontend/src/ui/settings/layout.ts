/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The pieces every Settings section is built from, so a section an extension
 * adds looks like the dashboard's own.
 *
 * A section is groups of rows. A row has its name and a line on what it does on
 * the left and its control on the right; on a phone the control goes under the
 * words. The line is read out with the control (`aria-describedby`), so a
 * screen reader hears what the switch does, not only what it is called.
 */
import { uid } from "../../ids";
import type { SettingsScope } from "../../types/settings";
import { el, named } from "../dom";
import { iconMarkup } from "../shell/icons";

/** What each scope says beside a section's heading. */
export const SCOPE_NOTE: Record<SettingsScope, string> = {
    browser: "Kept in this browser",
    server: "Kept on the server, for everyone",
};

/** A choice among a few, as `choiceGroup` draws it. */
export interface Choice {
    /** What is chosen by it. */
    value: string;
    /** What it says. */
    label: string;
}

/** What else a row may show, and which control its hint describes. */
export interface RowOptions {
    /** An icon before the name, by its name in `ui/shell/icons`. */
    icon?: string | undefined;
    /** The control the hint is read out with, when `control` holds several: `control` itself otherwise. */
    describes?: HTMLElement;
}

/**
 * One setting: `label` and `hint` beside `control`. The control carries its own
 * accessible name (`named`, or a group's `aria-label`); the hint describes it.
 */
export function settingRow(
    label: string,
    hint: string,
    control: HTMLElement,
    opts: RowOptions = {},
): HTMLElement {
    const row = el("div", "af-setting-row");
    if (opts.icon) {
        const icon = el("span", "af-setting-icon");
        icon.setAttribute("aria-hidden", "true");
        // Markup from the icon registry, never from a caller's text.
        icon.innerHTML = iconMarkup(opts.icon, 16);
        row.appendChild(icon);
    }
    const words = el("div", "af-setting-words");
    words.appendChild(el("span", "af-setting-name", label));
    if (hint) {
        const line = el("span", "af-setting-hint", hint);
        line.id = uid("af-setting-hint");
        (opts.describes ?? control).setAttribute("aria-describedby", line.id);
        words.appendChild(line);
    }
    const at = el("div", "af-setting-control");
    at.appendChild(control);
    row.append(words, at);
    return row;
}

/** Rows under a heading of their own, drawn as one card. */
export function settingGroup(title: string, ...rows: HTMLElement[]): HTMLElement {
    const group = el("div", "af-setting-group");
    const card = el("div", "af-setting-rows");
    card.append(...rows);
    group.append(el("h3", "af-setting-group-title", title), card);
    return group;
}

/** An on/off switch named `label`, telling `onChange` its new state. */
export function settingSwitch(
    name: string,
    label: string,
    on: boolean,
    onChange: (on: boolean) => void,
): HTMLInputElement {
    const box = named(el("input", "af-switch"), name, label);
    box.type = "checkbox";
    box.setAttribute("role", "switch");
    box.checked = on;
    box.addEventListener("change", () => onChange(box.checked));
    return box;
}

/**
 * A few choices side by side, one of them on: radio buttons drawn as one
 * segmented control, so the arrow keys move between them as they do in any
 * radio group.
 */
export function choiceGroup(
    name: string,
    label: string,
    choices: readonly Choice[],
    current: string,
    onPick: (value: string) => void,
): HTMLElement {
    const group = el("div", "af-segments");
    group.setAttribute("role", "radiogroup");
    group.setAttribute("aria-label", label);
    for (const choice of choices) {
        const segment = el("label", "af-segment");
        const radio = el("input");
        radio.type = "radio";
        radio.name = name;
        radio.value = choice.value;
        radio.checked = choice.value === current;
        radio.addEventListener("change", () => {
            if (radio.checked) {
                onPick(choice.value);
            }
        });
        segment.append(radio, el("span", "", choice.label));
        group.appendChild(segment);
    }
    return group;
}
