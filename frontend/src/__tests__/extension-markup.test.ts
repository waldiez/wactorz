/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Extensions supply two strings that reach `innerHTML`: an icon's SVG markup
 * (`registerIcon`) and a nav button's label (`extraViews`). Both were
 * interpolated raw, so an extension could put script into the header, the nav
 * and every card that draws its icon.
 *
 * The icon markup cannot simply be escaped — it *is* markup, and the feature is
 * to render it — so it is reduced to allow-listed shapes instead. The label can
 * be escaped, and is.
 */
import { describe, it, expect } from "vitest";

import { registerIcon, iconMarkup } from "../ui/dashboard/icons";
import { buildHeader } from "../ui/dashboard/header";

describe("registerIcon reduces markup to shapes", () => {
    it("keeps a legitimate icon intact", () => {
        registerIcon("t-ok", '<path d="M1 2 3 4"/><circle cx="5" cy="6" r="7"/>');
        const markup = iconMarkup("t-ok");

        expect(markup).toContain('d="M1 2 3 4"');
        expect(markup).toContain('cx="5"');
    });

    it("drops a script element", () => {
        registerIcon("t-script", '<path d="M1 2"/><script>window.__pwned = 1;</script>');

        expect(iconMarkup("t-script")).not.toContain("script");
        expect(iconMarkup("t-script")).toContain('d="M1 2"');
    });

    it("drops foreignObject, which is how markup smuggles HTML into an svg", () => {
        registerIcon("t-fo", '<foreignObject><img src="x" onerror="window.__pwned = 1"></foreignObject>');

        const markup = iconMarkup("t-fo");
        expect(markup.toLowerCase()).not.toContain("foreignobject");
        expect(markup).not.toContain("onerror");
    });

    it("strips an event handler from an otherwise legitimate shape", () => {
        registerIcon("t-onload", '<path d="M1 2" onload="window.__pwned = 1"/>');

        const markup = iconMarkup("t-onload");
        expect(markup).toContain('d="M1 2"'); // shape kept
        expect(markup).not.toContain("onload"); // handler gone
    });

    it("strips handlers nested inside a group", () => {
        registerIcon("t-nested", '<g><path d="M1 2" onclick="window.__pwned = 1"/></g>');

        expect(iconMarkup("t-nested")).not.toContain("onclick");
    });
});

describe("extension-supplied nav labels", () => {
    it("are escaped, not parsed as markup", () => {
        const header = buildHeader({
            view: "cards",
            connState: "live",
            onSetView: () => {},
            haUrl: null,
            extraViews: [{ key: "cards", label: '<img src=x onerror="window.__pwned = 1">', icon: "grid" }],
        });

        // The whole point: no element is created from the label's markup.
        expect(header.querySelector("img[onerror]")).toBeNull();
        expect(header.textContent).toContain("<img src=x");
    });
});
