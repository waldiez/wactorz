/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Settings: a list of sections beside the one open, each at its own address
 * (`#/settings/tabs`, `#/settings/sound`); `#/settings` opens the first.
 *
 * The list is links, so choosing a section goes through the address like any
 * other move, and Back returns to the section before. On a phone the list is a
 * row above the section, as the Inbox's lists are.
 *
 * Sections are handed in, and more can be added (`register`): what a section
 * holds, and where it keeps it, is the section's own.
 */
import { formatRoute } from "../../router";
import { el, labelled } from "../dom";
import { SCOPE_NOTE, type SettingsSection } from "./section";

/** Where a section is. */
function sectionHref(key: string): string {
    return formatRoute({ view: "settings", path: [key] });
}

export class SettingsView {
    private readonly _root = el("div", "af-settings");
    private readonly _sections: SettingsSection[];
    /** The section the address names; "" for the first. */
    private _open = "";
    /** The section drawn last, to release before another is drawn. */
    private _drawn: SettingsSection | null = null;
    /** Where the drawn section's content is on the page, to draw it again in place. */
    private _shown: HTMLElement | null = null;

    constructor(sections: SettingsSection[]) {
        this._sections = [...sections];
    }

    /** The section open, for the address: "" when it is the first, opened by `#/settings` alone. */
    get section(): string {
        return this._open;
    }

    /** Open the section the address asks for; one there is not opens the first. */
    show(key: string): void {
        this._open = this._sections.some(s => s.key === key) ? key : "";
    }

    /** Add `section` at the end, or in place of the one with its key. */
    register(section: SettingsSection): void {
        const at = this._sections.findIndex(s => s.key === section.key);
        if (at >= 0) {
            this._sections[at] = section;
        } else {
            this._sections.push(section);
        }
    }

    /** Draw the list and the open section from the values as they are now. */
    build(): HTMLElement {
        const current = this._current();
        this._drawn?.release?.();
        this._drawn = current;

        const nav = labelled(el("nav", "af-settings-nav"), "Settings sections");
        this._sections.forEach(section => nav.appendChild(this._link(section, section === current)));
        const side = el("aside", "af-settings-side");
        side.appendChild(nav);

        this._shown = current ? this._content(current) : null;
        const columns = el("div", "af-settings-columns");
        columns.append(side, this._shown ?? el("div"));
        this._root.replaceChildren(el("h1", "af-settings-title", "Settings"), columns);
        return this._root;
    }

    /**
     * Draw the section `key` again in place, if it is the one open: what it
     * shows changed. The list and the controls of every other section are left
     * alone, so a value arriving does not take the focus from someone using one.
     */
    redraw(key: string): void {
        const drawn = this._drawn;
        if (drawn?.key !== key || !this._shown) {
            return;
        }
        drawn.release?.();
        const fresh = this._content(drawn);
        this._shown.replaceWith(fresh);
        this._shown = fresh;
    }

    /** Stop what the drawn section follows: Settings is left, or the dashboard goes. */
    release(): void {
        this._drawn?.release?.();
        this._drawn = null;
        this._shown = null;
    }

    private _current(): SettingsSection | null {
        return this._sections.find(s => s.key === this._open) ?? this._sections[0] ?? null;
    }

    private _link(section: SettingsSection, current: boolean): HTMLAnchorElement {
        const link = el("a", `af-model-row${current ? " open" : ""}`);
        link.href = sectionHref(section.key);
        if (current) {
            link.setAttribute("aria-current", "page");
        }
        link.append(
            el("span", "af-model-row-name", section.label),
            el("span", "af-model-row-line", section.line),
        );
        return link;
    }

    private _content(section: SettingsSection): HTMLElement {
        const content = labelled(el("section", "af-settings-section"), section.label);
        const heading = el("h2", "af-settings-section-heading", section.label);
        content.append(heading, el("p", "af-settings-scope", SCOPE_NOTE[section.scope]), section.build());
        return content;
    }
}
