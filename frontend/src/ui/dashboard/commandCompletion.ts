/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Slash-command completion for the chat box: typing `/` lists the commands the
 * chat can send, narrowed as more is typed, each with how to type it and what
 * it does.
 *
 * The commands come from `GET /api/commands`, which asks the orchestrator that
 * answers the chat, so what is offered is what will be answered. Fetched once,
 * on the first `/`; a failed fetch is asked again on the next.
 *
 * The list is an ARIA listbox the textarea controls: the textarea keeps its
 * own role and focus, says `aria-autocomplete="list"`, and points at the
 * highlighted option with `aria-activedescendant`, so a screen reader follows
 * the highlight while the caret stays in the text.
 */

import { el } from "../dom";

/** One way to type a command, as the server lists it. */
export interface CommandForm {
    /** What is typed, arguments in `<…>` and optional parts in `[…]`. */
    usage: string;
    /** What is typed before the first argument: what accepting a form puts in the box. */
    typed: string;
    /** What it does; empty for an orchestrator that does not say. */
    summary: string;
}

/** The most options shown at once: past this, typing more narrows faster than scrolling. */
export const MAX_SHOWN = 12;

/** The forms that complete ``text``, the start of a command, in the order they were given. */
export function matchingForms(forms: readonly CommandForm[], text: string): CommandForm[] {
    if (!text.startsWith("/") || text.includes("\n")) {
        return [];
    }
    const typed = text.toLowerCase();
    // Once arguments are being typed (the text runs past what a form starts
    // with) the list has nothing more to offer for that form.
    return forms.filter(form => form.typed.toLowerCase().startsWith(typed)).slice(0, MAX_SHOWN);
}

/** The commands the chat can send, fetched when first needed. */
export class CommandCatalog {
    private forms: CommandForm[] | null = null;
    private pending: Promise<CommandForm[]> | null = null;

    constructor(private readonly fetchForms: () => Promise<CommandForm[]> = fetchFromServer) {}

    /** Whether the forms have arrived. Asks for nothing. */
    get ready(): boolean {
        return this.forms !== null;
    }

    /** The forms if they have arrived, else null; asks for them when nothing is on the way. */
    current(): CommandForm[] | null {
        if (this.forms === null && this.pending === null) {
            void this.load();
        }
        return this.forms;
    }

    /** The forms, fetched once; an empty list, asked again later, when the fetch fails. */
    async load(): Promise<CommandForm[]> {
        if (this.forms !== null) {
            return this.forms;
        }
        this.pending ??= this.fetchForms()
            .then(forms => {
                this.forms = forms;
                return forms;
            })
            .catch(() => [] as CommandForm[])
            .finally(() => {
                this.pending = null;
            });
        return this.pending;
    }
}

async function fetchFromServer(): Promise<CommandForm[]> {
    const base = window.__WACTORZ_INGRESS_PATH ?? "";
    const response = await fetch(`${base}/api/commands`);
    if (!response.ok) {
        throw new Error(`/api/commands answered ${response.status}`);
    }
    const body = (await response.json()) as { commands?: unknown };
    return Array.isArray(body.commands) ? body.commands.filter(isForm) : [];
}

function isForm(value: unknown): value is CommandForm {
    const v = value as Partial<CommandForm> | null;
    return typeof v?.usage === "string" && typeof v.typed === "string" && typeof v.summary === "string";
}

/** The listbox under the chat box, and the keys that drive it. */
export class CommandCompletion {
    /** The listbox; placed by the iobar, beside the mention panel. */
    readonly element: HTMLUListElement;
    private shown: CommandForm[] = [];
    private active = -1;
    private input: HTMLTextAreaElement | null = null;

    constructor(private readonly catalog = new CommandCatalog()) {
        this.element = el("ul", "af-command-list");
        this.element.id = "af-command-list";
        this.element.setAttribute("role", "listbox");
        this.element.setAttribute("aria-label", "Commands");
        this.element.hidden = true;
    }

    /** Tie the list to the textarea it completes. */
    attach(input: HTMLTextAreaElement): void {
        this.input = input;
        input.setAttribute("aria-autocomplete", "list");
        input.setAttribute("aria-controls", this.element.id);
    }

    /** Whether the list is showing. */
    get open(): boolean {
        return !this.element.hidden;
    }

    /**
     * Show the forms that complete what is in the box, or close the list.
     * Returns whether it is open. Before the commands have arrived it opens
     * as soon as they do, if the box still asks for them.
     */
    update(input: HTMLTextAreaElement): boolean {
        const forms = this.catalog.current();
        if (forms === null) {
            void this.catalog.load().then(() => {
                // Only when they did arrive: after a failed fetch, updating
                // again would fetch again at once, and go on for as long as
                // the fetch fails. The next keystroke asks again instead.
                if (this.catalog.ready && input.value.startsWith("/")) {
                    this.update(input);
                }
            });
            this.close();
            return false;
        }
        const matches = matchingForms(forms, input.value);
        if (matches.length === 0) {
            this.close();
            return false;
        }
        this.shown = matches;
        this.active = 0;
        this.render();
        this.element.hidden = false;
        return true;
    }

    /** Handle a key while the list is open. Returns true when the key was the list's. */
    onKeydown(e: KeyboardEvent, input: HTMLTextAreaElement): boolean {
        if (!this.open) {
            return false;
        }
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            this.move(e.key === "ArrowDown" ? 1 : -1);
            return true;
        }
        if ((e.key === "Tab" || e.key === "Enter") && !e.shiftKey) {
            return this.complete(e, input);
        }
        if (e.key === "Escape") {
            e.preventDefault();
            this.close();
            return true;
        }
        return false;
    }

    /** Move the highlight one step, wrapping at the ends. */
    private move(step: number): void {
        this.active = (this.active + step + this.shown.length) % this.shown.length;
        this.render();
    }

    /**
     * Complete from the highlighted form. Enter on a command already typed out
     * in full sends it, as it would without the list: only completing
     * something is the list's, so the key goes on to the box then.
     */
    private complete(e: KeyboardEvent, input: HTMLTextAreaElement): boolean {
        const form = this.shown[this.active];
        if (!form || (e.key === "Enter" && form.typed === input.value.trim())) {
            this.close();
            return false;
        }
        e.preventDefault();
        this.accept(form, input);
        return true;
    }

    /** Close the list and let the textarea point at nothing. */
    close(): void {
        this.shown = [];
        this.active = -1;
        this.element.hidden = true;
        this.element.textContent = "";
        this.input?.removeAttribute("aria-activedescendant");
    }

    private accept(form: CommandForm, input: HTMLTextAreaElement): void {
        // A space after a command that takes arguments, so typing goes on at once.
        input.value = form.usage === form.typed ? form.typed : `${form.typed} `;
        input.setSelectionRange(input.value.length, input.value.length);
        this.close();
        input.focus();
        input.dispatchEvent(new Event("input"));
    }

    private render(): void {
        this.element.textContent = "";
        this.shown.forEach((form, i) => {
            const item = el("li", "af-command-option" + (i === this.active ? " active" : ""));
            item.id = `af-command-option-${i}`;
            item.setAttribute("role", "option");
            item.setAttribute("aria-selected", String(i === this.active));
            item.append(el("code", "af-command-usage", form.usage));
            if (form.summary) {
                item.append(el("span", "af-command-summary", form.summary));
            }
            // mousedown, not click: the textarea would lose focus first, and
            // its blur closes the list before a click could land.
            item.addEventListener("mousedown", e => {
                e.preventDefault();
                if (this.input) {
                    this.accept(form, this.input);
                }
            });
            this.element.appendChild(item);
        });
        const activeItem = this.element.children[this.active];
        if (activeItem && this.input) {
            this.input.setAttribute("aria-activedescendant", activeItem.id);
            activeItem.scrollIntoView?.({ block: "nearest" });
        }
    }
}
