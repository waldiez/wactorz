/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Small builders for the elements the UI makes over and over.
 *
 * Text always goes in as text, never as markup: agent names, labels and
 * messages reach the browser over MQTT and chat, so anything a caller passes is
 * treated as untrusted. The one exception is `iconButton`, whose icon is markup
 * the caller already owns — an entry from the icon registry or an inline SVG —
 * and whose label, the part that might not be, still goes in as text.
 *
 * The rest set what assistive technology reads — a role, a name, a state — in
 * one way everywhere, and mark what tests find by `data-testid`; each returns
 * the element it was given, so it can be built in one expression.
 *
 * Nothing here imports from the rest of the UI, so any module can use it.
 */
import type { TestId } from "./testIds";

/** An element of `tag` with `className` and `text`, the text set as text. */
export function el<K extends keyof HTMLElementTagNameMap>(
    tag: K,
    className = "",
    text = "",
): HTMLElementTagNameMap[K] {
    const node = document.createElement(tag);
    if (className) {
        node.className = className;
    }
    if (text) {
        node.textContent = text;
    }
    return node;
}

/**
 * A button that never submits a form.
 *
 * `<button>` defaults to `type="submit"`, so one that ends up inside a form
 * submits it; every button this UI makes is an action of its own.
 */
export function button(className = "", text = ""): HTMLButtonElement {
    const btn = el("button", className, text);
    btn.type = "button";
    return btn;
}

/**
 * A button that shows only an icon.
 *
 * `label` is both its tooltip and its accessible name, since an icon alone says
 * nothing to a screen reader. `iconHtml` is trusted markup (see the module
 * note); pass `iconMarkup(...)` or an inline SVG, never text from elsewhere.
 */
export function iconButton(className: string, label: string, iconHtml: string): HTMLButtonElement {
    const btn = button(className);
    btn.title = label;
    btn.setAttribute("aria-label", label);
    btn.innerHTML = iconHtml;
    return btn;
}

/** An `<option>` with its value and its visible text. */
export function option(value: string, text: string): HTMLOptionElement {
    const opt = el("option", "", text);
    opt.value = value;
    return opt;
}

/**
 * A link that opens in a new tab without handing the opened page this one.
 *
 * `noopener` keeps the new page from reaching back through `window.opener`;
 * `noreferrer` keeps this page's address out of the request. `href` may be left
 * out for a link whose address is set, or withheld, once it is known.
 */
export function externalLink(href = "", className = ""): HTMLAnchorElement {
    const a = el("a", className);
    if (href) {
        a.href = href;
    }
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    return a;
}

/**
 * Give a form control its `name`, its accessible name, and optionally an `id`.
 *
 * A control without a `<label>` element needs `aria-label` to be announced as
 * anything; the `name` is what autofill and form handling key on. Returns the
 * control so it can be built in one expression.
 */
export function named<T extends HTMLElement>(control: T, name: string, label: string, id = ""): T {
    control.setAttribute("name", name);
    control.setAttribute("aria-label", label);
    if (id) {
        control.id = id;
    }
    return control;
}

/**
 * Mark `node` with a `data-testid` for the tests that find it.
 *
 * Only for what has no role and accessible name to be found by (see
 * `testIds.ts`); the ids are listed there, so a misspelt one does not compile.
 */
export function testId<T extends HTMLElement>(node: T, id: TestId): T {
    node.dataset["testid"] = id;
    return node;
}

/**
 * Make `node` a modal dialog named `label`.
 *
 * A promise to assistive technology that nothing behind it can be reached, so
 * the caller also keeps focus inside it (see `focusables`) and closes it on
 * Escape.
 */
export function dialog<T extends HTMLElement>(node: T, label: string): T {
    node.setAttribute("role", "dialog");
    node.setAttribute("aria-modal", "true");
    node.setAttribute("aria-label", label);
    return node;
}

/**
 * Make `node` a live region, so what is added to it is read out as it arrives.
 *
 * `status` is for a single line that is replaced, such as the connection badge;
 * `log` for a list that grows, such as a conversation. Both are polite: they
 * wait until the reader has finished speaking. A log scrolls, so it also takes
 * focus: otherwise the keyboard cannot scroll one with nothing to focus inside.
 */
export function live<T extends HTMLElement>(node: T, role: "status" | "log", label = ""): T {
    node.setAttribute("role", role);
    node.setAttribute("aria-live", "polite");
    if (role === "log") {
        node.tabIndex = 0;
    }
    if (label) {
        node.setAttribute("aria-label", label);
    }
    return node;
}

/**
 * Let an element that is not a button act as one: by pointer, and by Enter or
 * Space once it has focus.
 *
 * For what has to keep its own tag for layout, such as a thumbnail or a feed
 * row. `label` names it when its content does not, as for an image; pass ""
 * when its text already says what it does.
 */
export function clickable<T extends HTMLElement>(node: T, label: string, onActivate: () => void): T {
    node.setAttribute("role", "button");
    node.tabIndex = 0;
    if (label) {
        node.setAttribute("aria-label", label);
    }
    node.addEventListener("click", onActivate);
    node.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onActivate();
        }
    });
    return node;
}

/**
 * Name `node` for assistive technology, in place of the text it shows.
 *
 * For when the text alone does not say enough out of context, such as a
 * "History" button among many; start the name with that text, so a person who
 * speaks the visible label to control the page still matches it.
 */
export function labelled<T extends HTMLElement>(node: T, label: string): T {
    node.setAttribute("aria-label", label);
    return node;
}

/**
 * Make `node` a list named `label`, for items laid out by the styles rather
 * than as `<ul>`, such as a grid of cards. Its items are marked with `listItem`.
 */
export function list<T extends HTMLElement>(node: T, label: string): T {
    node.setAttribute("role", "list");
    node.setAttribute("aria-label", label);
    return node;
}

/** Make `node` an item of a `list`, named `label` — usually what the item is about. */
export function listItem<T extends HTMLElement>(node: T, label: string): T {
    node.setAttribute("role", "listitem");
    node.setAttribute("aria-label", label);
    return node;
}

/** Say whether the panel `control` opens is open, as `aria-expanded`. */
export function expanded<T extends HTMLElement>(control: T, open: boolean): T {
    control.setAttribute("aria-expanded", String(open));
    return control;
}

/** Say whether a toggle button is on, as `aria-pressed`. */
export function pressed<T extends HTMLElement>(control: T, on: boolean): T {
    control.setAttribute("aria-pressed", String(on));
    return control;
}

/** What can take focus from the Tab key: a control that is not disabled, a link, a `<details>` summary, or anything given a tab stop. */
const FOCUSABLE = [
    "button:not([disabled])",
    "input:not([disabled])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    "[href]",
    "summary",
    "[tabindex]:not([tabindex='-1'])",
].join(", ");

/** The elements inside `root` that can hold focus, in document order: what a
 *  modal dialog keeps the Tab key among. */
export function focusables(root: HTMLElement): HTMLElement[] {
    return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)];
}

/**
 * Keep Tab, and Shift+Tab, inside `root` while it is a modal dialog: past the
 * last thing in it to the first, and back. Focus that is outside it goes to the
 * first going forward, the last going back. Call it from the dialog's keydown
 * handler on Tab; a dialog with nothing to focus leaves the key alone.
 */
export function trapTab(root: HTMLElement, e: KeyboardEvent): void {
    const items = focusables(root);
    if (!items.length) {
        return;
    }
    e.preventDefault();
    const current = items.indexOf(document.activeElement as HTMLElement);
    const step = e.shiftKey ? -1 : 1;
    const next =
        current === -1 ? (e.shiftKey ? items.length - 1 : 0) : (current + step + items.length) % items.length;
    items[next]!.focus();
}
