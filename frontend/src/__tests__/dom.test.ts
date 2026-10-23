/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi } from "vitest";
import {
    button,
    clickable,
    dialog,
    el,
    expanded,
    externalLink,
    focusables,
    iconButton,
    labelled,
    list,
    listItem,
    live,
    named,
    option,
    pressed,
    testId,
    trapTab,
} from "../ui/dom";

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

describe("testId", () => {
    it("marks the element for tests and hands it back", () => {
        const div = el("div");

        expect(testId(div, "agent-card")).toBe(div);
        expect(div.getAttribute("data-testid")).toBe("agent-card");
    });
});

describe("dialog", () => {
    it("is a modal dialog named by its label", () => {
        const box = dialog(el("div"), "Delete weather?");

        expect(box.getAttribute("role")).toBe("dialog");
        expect(box.getAttribute("aria-modal")).toBe("true");
        expect(box.getAttribute("aria-label")).toBe("Delete weather?");
    });
});

describe("live", () => {
    it("reads out what changes, politely", () => {
        const badge = live(el("span"), "status");

        expect(badge.getAttribute("role")).toBe("status");
        expect(badge.getAttribute("aria-live")).toBe("polite");
        expect(badge.hasAttribute("aria-label")).toBe(false);
    });

    it("is named when given a label", () => {
        expect(live(el("div"), "log", "Conversation").getAttribute("aria-label")).toBe("Conversation");
    });

    it("lets the keyboard reach a log, which scrolls, and leaves a status line out of the tab order", () => {
        expect(live(el("div"), "log").tabIndex).toBe(0);
        expect(live(el("span"), "status").hasAttribute("tabindex")).toBe(false);
    });
});

describe("clickable", () => {
    it("acts on a click, Enter and Space, and on no other key", () => {
        const onActivate = vi.fn();
        const img = clickable(el("img"), "Open preview: a.png", onActivate);

        img.click();
        for (const key of ["Enter", " ", "a", "Escape"]) {
            img.dispatchEvent(new KeyboardEvent("keydown", { key }));
        }

        expect(onActivate).toHaveBeenCalledTimes(3);
        expect(img.getAttribute("role")).toBe("button");
        expect(img.tabIndex).toBe(0);
        expect(img.getAttribute("aria-label")).toBe("Open preview: a.png");
    });

    it("keeps the page from scrolling on Space", () => {
        const row = clickable(el("div", "", "a row"), "", vi.fn());
        const space = new KeyboardEvent("keydown", { key: " ", cancelable: true });

        row.dispatchEvent(space);

        expect(space.defaultPrevented).toBe(true);
    });

    it("leaves the name to the content when it has no label", () => {
        expect(clickable(el("div", "", "a row"), "", vi.fn()).hasAttribute("aria-label")).toBe(false);
    });
});

describe("expanded and pressed", () => {
    it("say the state as the strings ARIA reads", () => {
        const btn = button();

        expect(expanded(btn, true).getAttribute("aria-expanded")).toBe("true");
        expect(expanded(btn, false).getAttribute("aria-expanded")).toBe("false");
        expect(pressed(btn, true).getAttribute("aria-pressed")).toBe("true");
        expect(pressed(btn, false).getAttribute("aria-pressed")).toBe("false");
    });
});

describe("labelled", () => {
    it("names the element in place of its text", () => {
        const btn = labelled(button("", "History"), "History of weather");

        expect(btn.getAttribute("aria-label")).toBe("History of weather");
        expect(btn.textContent).toBe("History");
    });
});

describe("list and listItem", () => {
    it("name a list and its items", () => {
        const grid = list(el("div"), "Agents");
        const card = listItem(el("div"), "weather");

        expect([grid.getAttribute("role"), grid.getAttribute("aria-label")]).toEqual(["list", "Agents"]);
        expect([card.getAttribute("role"), card.getAttribute("aria-label")]).toEqual(["listitem", "weather"]);
    });
});

describe("focusables", () => {
    it("finds what can hold focus, in document order, and skips what cannot", () => {
        const root = el("div");
        const first = button();
        const link = el("a");
        link.href = "https://example.com";
        const row = el("div");
        row.tabIndex = 0;
        const skipped = el("div");
        skipped.tabIndex = -1;
        const field = el("input");
        const summary = el("summary");
        root.append(first, el("span"), link, skipped, row, field, summary);

        expect(focusables(root)).toEqual([first, link, row, field, summary]);
    });

    it("skips a disabled control, which cannot take focus", () => {
        const root = el("div");
        const off = button();
        off.disabled = true;
        const field = el("textarea");
        field.disabled = true;
        root.append(off, field);

        expect(focusables(root)).toEqual([]);
    });
});

describe("trapTab", () => {
    function dialogWith(count: number): { root: HTMLElement; items: HTMLButtonElement[] } {
        const root = el("div");
        const items = Array.from({ length: count }, () => button());
        root.append(...items);
        document.body.appendChild(root);
        return { root, items };
    }

    function tab(root: HTMLElement, shiftKey = false): KeyboardEvent {
        const e = new KeyboardEvent("keydown", { key: "Tab", shiftKey, cancelable: true });
        trapTab(root, e);
        return e;
    }

    it("goes from the last to the first, and back with Shift", () => {
        const { root, items } = dialogWith(3);
        items[2]!.focus();

        expect(tab(root).defaultPrevented).toBe(true);
        expect(document.activeElement).toBe(items[0]);
        tab(root, true);
        expect(document.activeElement).toBe(items[2]);
        root.remove();
    });

    it("brings focus from outside in: to the first going forward, the last going back", () => {
        const { root, items } = dialogWith(2);
        const outside = document.body.appendChild(button());
        outside.focus();

        tab(root);
        expect(document.activeElement).toBe(items[0]);
        outside.focus();
        tab(root, true);
        expect(document.activeElement).toBe(items[1]);
        root.remove();
        outside.remove();
    });

    it("leaves the key alone in a dialog with nothing to focus", () => {
        const { root } = dialogWith(0);

        expect(tab(root).defaultPrevented).toBe(false);
        root.remove();
    });
});
