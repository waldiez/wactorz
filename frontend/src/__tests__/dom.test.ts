/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { button, el, externalLink, iconButton, named, option } from "../ui/dom";

describe("el", () => {
    it("builds the tag with its class and text", () => {
        const span = el("span", "af-feed-agent", "flic");

        expect(span.tagName).toBe("SPAN");
        expect(span.className).toBe("af-feed-agent");
        expect(span.textContent).toBe("flic");
    });

    it("sets text as text, so markup in it is not parsed", () => {
        const div = el("div", "", "<img src=x onerror=alert(1)>");

        expect(div.children).toHaveLength(0);
        expect(div.textContent).toBe("<img src=x onerror=alert(1)>");
    });

    it("leaves out a class it was not given", () => {
        expect(el("div").hasAttribute("class")).toBe(false);
    });
});

describe("button", () => {
    it("never submits a form", () => {
        const form = document.createElement("form");
        const btn = button("af-mini-btn", "Save");
        form.appendChild(btn);

        expect(btn.type).toBe("button");
        expect(btn.textContent).toBe("Save");
    });
});

describe("iconButton", () => {
    it("names itself with its label, for the tooltip and for screen readers", () => {
        const btn = iconButton("af-mic-btn", "Voice input", "<svg></svg>");

        expect(btn.title).toBe("Voice input");
        expect(btn.getAttribute("aria-label")).toBe("Voice input");
        expect(btn.querySelector("svg")).not.toBeNull();
        expect(btn.type).toBe("button");
    });
});

describe("option", () => {
    it("carries its value and its text separately", () => {
        const opt = option("daily", "Daily");

        expect(opt.value).toBe("daily");
        expect(opt.textContent).toBe("Daily");
    });
});

describe("externalLink", () => {
    it("opens in a new tab without handing that tab this page", () => {
        const a = externalLink("https://example.org/", "af-link");

        expect(a.href).toBe("https://example.org/");
        expect(a.target).toBe("_blank");
        expect(a.rel).toBe("noopener noreferrer");
        expect(a.className).toBe("af-link");
    });

    it("carries no address until it is given one", () => {
        expect(externalLink().hasAttribute("href")).toBe(false);
    });
});

describe("named", () => {
    it("gives a control its name, its accessible name and its id", () => {
        const select = named(
            el("select", "af-cfg-input"),
            "cost-period",
            "Cost limit period",
            "af-cost-period",
        );

        expect(select.getAttribute("name")).toBe("cost-period");
        expect(select.getAttribute("aria-label")).toBe("Cost limit period");
        expect(select.id).toBe("af-cost-period");
    });

    it("leaves the id alone when none is given", () => {
        const input = named(el("input"), "ambient-volume", "Ambient volume");

        expect(input.hasAttribute("id")).toBe(false);
    });
});
