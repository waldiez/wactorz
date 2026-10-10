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
 * Nothing here imports from the rest of the UI, so any module can use it.
 */

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
