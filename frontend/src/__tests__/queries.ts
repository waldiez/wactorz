/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Finding elements in a unit test the way the end-to-end suite finds them in
 * the browser: by role and accessible name, or by test id where there is none.
 *
 * A small subset of the rules a browser applies, enough for the markup this
 * dashboard builds: an explicit `role`, or the implicit role of the few tags it
 * uses; a name from `aria-label`, else from the text inside.
 */
import type { TestId } from "../ui/testIds";

const IMPLICIT_ROLES: Record<string, string> = {
    BUTTON: "button",
    NAV: "navigation",
    MAIN: "main",
    SELECT: "combobox",
    TEXTAREA: "textbox",
    A: "link",
};

/** The element's role: its `role` attribute, else what its tag implies. */
export function roleOf(node: Element): string {
    return node.getAttribute("role") ?? IMPLICIT_ROLES[node.tagName] ?? "";
}

/** The element's accessible name: its `aria-label`, else its text. */
export function nameOf(node: Element): string {
    return (node.getAttribute("aria-label") ?? node.textContent ?? "").trim();
}

/** Every element under `root` with `role`, and with exactly `name` when one is given. */
export function allByRole(root: ParentNode, role: string, name?: string): HTMLElement[] {
    return [...root.querySelectorAll<HTMLElement>("*")].filter(
        node => roleOf(node) === role && (name === undefined || nameOf(node) === name),
    );
}

/** The one element under `root` with `role` and `name`; fails the test unless there is exactly one. */
export function byRole(root: ParentNode, role: string, name?: string): HTMLElement {
    const found = allByRole(root, role, name);
    if (found.length !== 1) {
        throw new Error(
            `expected one ${role}${name === undefined ? "" : ` named ${name}`}, found ${found.length}`,
        );
    }
    return found[0]!;
}

/** Every element under `root` marked with test id `id`. */
export function allByTestId(root: ParentNode, id: TestId): HTMLElement[] {
    return [...root.querySelectorAll<HTMLElement>(`[data-testid="${id}"]`)];
}

/** The one element under `root` marked `id`; fails the test unless there is exactly one. */
export function byTestId(root: ParentNode, id: TestId): HTMLElement {
    const found = allByTestId(root, id);
    if (found.length !== 1) {
        throw new Error(`expected one element marked ${id}, found ${found.length}`);
    }
    return found[0]!;
}
