/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * A question's every answer fits in its toast.
 *
 * The toast is a fixed width and hides what spills out of it, and a question may
 * offer up to ten answers of up to sixty characters. The page lays nothing out
 * here, so what is checked is the stylesheet's rules that keep the buttons
 * inside: the row wraps, and a button keeps its width up to the toast's, its
 * label wrapping beyond that. A real browser measuring the buttons is the
 * manual check beside this.
 */
import { describe, it, expect } from "vitest";

// `?raw` rather than node:fs, as the service worker's test reads its script.
import css from "../styles/toast.css?raw";

/** The declarations of the first rule whose selector is exactly `selector`. */
function rule(selector: string): Record<string, string> {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const found = new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`, "m").exec(css);
    expect(found, `no rule for ${selector}`).not.toBeNull();
    const declarations: Record<string, string> = {};
    for (const part of (found?.[1] ?? "").split(";")) {
        const [name, ...value] = part.split(":");
        if (name?.trim() && value.length) {
            declarations[name.trim()] = value.join(":").trim();
        }
    }
    return declarations;
}

describe("the answers of a question in its toast", () => {
    it("go on as many rows as they need", () => {
        expect(rule(".wz-toast__actions")["flex-wrap"]).toBe("wrap");
    });

    it("keep their width, up to the toast's, and wrap a longer label inside", () => {
        const button = rule(".wz-toast__btn");

        expect(button["flex"]).toBe("none");
        expect(button["max-width"]).toBe("100%");
        expect(button["overflow-wrap"]).toBe("anywhere");
    });
});
