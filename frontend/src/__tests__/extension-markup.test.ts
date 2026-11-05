/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Extensions supply two strings that reach `innerHTML`: an icon's SVG markup
 * (`registerIcon`) and a nav button's label (`extraViews`). Either would
 * otherwise run script in the header, the nav, and every card drawing an icon.
 *
 * The label is escaped. The icon cannot be — rendering markup is the whole
 * feature — so it is reduced to allow-listed shapes instead.
 */
import { describe, it, expect } from "vitest";

import { registerIcon, registerIconFile, iconMarkup } from "../ui/shell/icons";
import { buildHeader } from "../ui/shell/header";

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

describe("registerIconFile takes a whole SVG file", () => {
    const FILE =
        '<?xml version="1.0" encoding="UTF-8"?>\n' +
        '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48" stroke="red">' +
        '<path d="M9 2v5"/><circle cx="12" cy="12" r="3" onload="window.__pwned = 1"/><script>window.__pwned = 1;</script>' +
        "</svg>\n";

    it("keeps the shapes inside its svg, drawn on the dashboard's own grid", () => {
        registerIconFile("t-file", FILE);
        const markup = iconMarkup("t-file");

        expect(markup).toContain('d="M9 2v5"');
        expect(markup).toContain('cx="12"');
        expect(markup).toContain('viewBox="0 0 24 24"');
        expect(markup).not.toContain("48");
        expect(markup).not.toContain("red");
    });

    it("drops what is not a shape, as registerIcon does", () => {
        registerIconFile("t-file-script", FILE);
        const markup = iconMarkup("t-file-script");

        expect(markup).not.toContain("script");
        expect(markup).not.toContain("onload");
        expect(markup).not.toContain("xml");
    });

    it("draws nothing for a file with no svg in it", () => {
        registerIconFile("t-file-none", "<p>not a drawing</p>");

        expect(iconMarkup("t-file-none")).toBe("");
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
