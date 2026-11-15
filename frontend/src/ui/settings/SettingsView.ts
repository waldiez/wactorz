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
 * Sections are handed in, and more can be added (`register`), by an extension
 * among others: what a section holds, and where it keeps it, is the section's
 * own. An extension registers when it loads, which can be after the address
 * asking for its section was read; the address keeps naming it meanwhile, the
 * first section is drawn, and the one asked for opens when it arrives, as a
 * view an extension adds does (`ViewRoutes.open`).
 */
import { formatRoute } from "../../router";
import type { SettingsSection } from "../../types/settings";
import { el, labelled } from "../dom";
import { iconMarkup } from "../shell/icons";
import { SCOPE_NOTE } from "./layout";

/** Where a section is. */
function sectionHref(key: string): string {
    return formatRoute({ view: "settings", path: [key] });
}

/** The icon a section shows when it names none. */
const DEFAULT_ICON = "settings";

export class SettingsView {
    private readonly _root = el("div", "af-settings");
    private readonly _sections: SettingsSection[];
    /** The section the address names; "" for the first. It may not be registered yet. */
    private _asked = "";
    /** The section drawn last, to release before another is drawn. */
    private _drawn: SettingsSection | null = null;
    /** Where the drawn section's content is on the page, to draw it again in place. */
    private _shown: HTMLElement | null = null;
    /** The list of sections as drawn, to draw again when one is added. */
    private _nav: HTMLElement | null = null;

    constructor(sections: SettingsSection[]) {
        this._sections = [...sections];
    }

    /**
     * The section the address names: "" for `#/settings` alone, which opens
     * the first. One asked for that has not registered yet is named too, so
     * the address keeps asking for it.
     */
    get section(): string {
        return this._asked;
    }

    /** Open the section the address asks for; while there is none by that key, the first is drawn. */
    show(key: string): void {
        this._asked = key;
    }

    /**
     * Add `section` at the end, or in place of the one with its key. Settings
     * open draws its list again, and the section itself when it is the one the
     * address asked for.
     */
    register(section: SettingsSection): void {
        const at = this._sections.findIndex(s => s.key === section.key);
        if (at >= 0) {
            this._sections[at] = section;
        } else {
            this._sections.push(section);
        }
        if (!this._drawn) {
            return;
        }
        if (section.key === this._asked || section.key === this._drawn.key) {
            this.build();
        } else {
            this._drawNav();
        }
    }

    /** Draw the list and the open section from the values as they are now. */
    build(): HTMLElement {
        const current = this._current();
        this._drawn?.release?.();
        this._drawn = current;

        this._nav = labelled(el("nav", "af-settings-nav"), "Settings sections");
        this._drawNav();
        const side = el("aside", "af-settings-side");
        side.appendChild(this._nav);

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
        this._nav = null;
    }

    private _current(): SettingsSection | null {
        return this._sections.find(s => s.key === this._asked) ?? this._sections[0] ?? null;
    }

    private _drawNav(): void {
        const current = this._drawn;
        this._nav?.replaceChildren(...this._sections.map(s => this._link(s, s === current)));
    }

    private _link(section: SettingsSection, current: boolean): HTMLAnchorElement {
        const link = el("a", `af-settings-link${current ? " open" : ""}`);
        link.href = sectionHref(section.key);
        if (current) {
            link.setAttribute("aria-current", "page");
        }
        const icon = el("span", "af-settings-link-icon");
        icon.setAttribute("aria-hidden", "true");
        // Markup from the icon registry, never from a section's own text.
        icon.innerHTML = iconMarkup(section.icon ?? DEFAULT_ICON, 16) || iconMarkup(DEFAULT_ICON, 16);
        const words = el("span", "af-settings-link-words");
        words.append(
            el("span", "af-settings-link-name", section.label),
            el("span", "af-settings-link-line", section.line),
        );
        link.append(icon, words);
        return link;
    }

    private _content(section: SettingsSection): HTMLElement {
        const content = labelled(el("section", "af-settings-section"), section.label);
        const head = el("div", "af-settings-section-head");
        head.append(
            el("h2", "af-settings-section-heading", section.label),
            el("span", `af-settings-scope ${section.scope}`, SCOPE_NOTE[section.scope]),
        );
        content.append(head, el("p", "af-settings-section-line", section.line), section.build());
        return content;
    }
}
