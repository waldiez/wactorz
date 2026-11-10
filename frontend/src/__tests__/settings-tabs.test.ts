/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** Which tabs this browser hides, and Settings → Tabs, where they are chosen. */
import { describe, it, expect, vi, beforeEach } from "vitest";

import { canHide, DEVICES_TAB, hiddenTabs, setTabHidden } from "../config/hiddenTabs";
import { HIDDEN_TABS_KEY, TAB_ORDER_KEY } from "../config/keys";
import {
    canMove,
    inTabOrder,
    isDefaultOrder,
    moveTab,
    resetTabOrder,
    tabChoices,
    tabOrder,
} from "../config/tabOrder";
import { buildTabsSection, tabsSection, type TabsSectionHost } from "../ui/settings/tabs";
import { allByRole, byRole } from "./queries";

const TABS = [
    { key: "overview", label: "Overview" },
    { key: "feed", label: "Feed" },
    { key: "models", label: "Models" },
    { key: DEVICES_TAB, label: "Devices" },
    { key: "settings", label: "Settings" },
];

function host(over: Partial<TabsSectionHost> = {}): TabsSectionHost {
    return { tabs: () => TABS, haConfigured: () => true, changed: vi.fn(), ...over };
}

function box(root: HTMLElement, label: string): HTMLInputElement {
    return byRole(root, "switch", `Show the ${label} tab`) as HTMLInputElement;
}

/** What is read out with a control as its description. */
function hintOf(root: HTMLElement, control: HTMLElement): string {
    const id = control.getAttribute("aria-describedby");
    return id ? (root.querySelector(`[id="${id}"]`)?.textContent ?? "") : "";
}

describe("the hidden tabs", () => {
    beforeEach(() => localStorage.clear());

    it("hides none until one is hidden", () => {
        expect(hiddenTabs().size).toBe(0);
    });

    it("keeps what is hidden, and forgets it when shown again", () => {
        setTabHidden("feed", true);
        setTabHidden(DEVICES_TAB, true);
        expect([...hiddenTabs()].sort()).toEqual([DEVICES_TAB, "feed"]);

        setTabHidden("feed", false);
        setTabHidden(DEVICES_TAB, false);
        expect(hiddenTabs().size).toBe(0);
        expect(localStorage.getItem(HIDDEN_TABS_KEY)).toBeNull();
    });

    it("never hides a tab the dashboard cannot do without", () => {
        for (const key of ["overview", "chat", "inbox", "settings"]) {
            expect(canHide(key)).toBe(false);
            setTabHidden(key, true);
        }
        expect(hiddenTabs().size).toBe(0);
    });

    it("ignores a stored fixed tab, and a list that is not one", () => {
        localStorage.setItem(HIDDEN_TABS_KEY, JSON.stringify(["settings", "feed", 3]));
        expect([...hiddenTabs()]).toEqual(["feed"]);

        localStorage.setItem(HIDDEN_TABS_KEY, "{not json");
        expect(hiddenTabs().size).toBe(0);

        localStorage.setItem(HIDDEN_TABS_KEY, JSON.stringify({ feed: true }));
        expect(hiddenTabs().size).toBe(0);
    });
});

describe("Settings → Tabs", () => {
    beforeEach(() => localStorage.clear());

    it("ticks every tab that shows, and fixes the ones that always do", () => {
        setTabHidden("models", true);
        const root = buildTabsSection(host());

        expect(box(root, "Feed").checked).toBe(true);
        expect(box(root, "Models").checked).toBe(false);
        expect(box(root, "Overview").disabled).toBe(true);
        expect(box(root, "Settings").disabled).toBe(true);
        expect(box(root, "Feed").disabled).toBe(false);
        expect(hintOf(root, box(root, "Overview"))).toBe("Always shown.");
    });

    it("hides a tab when it is unticked, and says the header changed", () => {
        const h = host();
        const root = buildTabsSection(h);
        const feed = box(root, "Feed");

        feed.checked = false;
        feed.dispatchEvent(new Event("change"));

        expect(hiddenTabs().has("feed")).toBe(true);
        expect(h.changed).toHaveBeenCalledOnce();

        feed.checked = true;
        feed.dispatchEvent(new Event("change"));
        expect(hiddenTabs().has("feed")).toBe(false);
    });

    it("says when the Devices link shows, where Home Assistant is not set up", () => {
        const root = buildTabsSection(host({ haConfigured: () => false }));
        expect(hintOf(root, box(root, "Devices"))).toBe("After the tabs, once Home Assistant is set up.");
        const configured = buildTabsSection(host());
        expect(hintOf(configured, box(configured, "Devices"))).toBe("After the tabs.");
    });

    it("says hiding a tab does not take it away", () => {
        expect(buildTabsSection(host()).textContent).toContain("still opens from a link");
    });

    it("is the section Settings opens at #/settings/tabs, kept in the browser", () => {
        const section = tabsSection(host());
        expect([section.key, section.scope]).toEqual(["tabs", "browser"]);
        expect(box(section.build(), "Feed")).toBeTruthy();
    });
});

describe("the tab order", () => {
    beforeEach(() => localStorage.clear());

    const tabs = (...keys: string[]) => keys.map(key => ({ key }));
    const keysOf = (list: { key: string }[]) => list.map(t => t.key);

    it("is the dashboard's own until a tab is moved", () => {
        expect(tabOrder()).toEqual([]);
        expect(keysOf(inTabOrder(tabs("overview", "feed", "chat"), tabOrder()))).toEqual([
            "overview",
            "feed",
            "chat",
        ]);
    });

    it("moves a tab one place, and keeps the whole order from then on", () => {
        moveTab(["overview", "feed", "chat"], "chat", -1);
        expect(tabOrder()).toEqual(["overview", "chat", "feed"]);
        moveTab(tabOrder(), "overview", 1);
        expect(tabOrder()).toEqual(["chat", "overview", "feed"]);
    });

    it("changes nothing for a move past either end, or of a tab it does not show", () => {
        moveTab(["overview", "feed"], "overview", -1);
        moveTab(["overview", "feed"], "feed", 1);
        moveTab(["overview", "feed"], "models", 1);
        expect(localStorage.getItem(TAB_ORDER_KEY)).toBeNull();
    });

    it("puts a tab the order does not name after those it does, as it came", () => {
        localStorage.setItem(TAB_ORDER_KEY, JSON.stringify(["chat", "overview"]));
        expect(keysOf(inTabOrder(tabs("overview", "feed", "chat", "panel-x"), tabOrder()))).toEqual([
            "chat",
            "overview",
            "feed",
            "panel-x",
        ]);
    });

    it("never orders Settings or the Devices link, which stay where they are", () => {
        expect([canMove("settings"), canMove(DEVICES_TAB), canMove("feed")]).toEqual([false, false, true]);
        moveTab(["feed", "settings"], "feed", 1);
        expect(localStorage.getItem(TAB_ORDER_KEY)).toBeNull();
        localStorage.setItem(TAB_ORDER_KEY, JSON.stringify(["settings", "feed", "feed", 7]));
        expect(tabOrder()).toEqual(["feed"]);
    });

    it("keeps the place of a tab it names that is not shown this time, an extension's not loaded yet", () => {
        localStorage.setItem(TAB_ORDER_KEY, JSON.stringify(["feed", "panels", "overview", "models"]));

        moveTab(["feed", "overview", "models"], "overview", -1);

        expect(tabOrder()).toEqual(["overview", "panels", "feed", "models"]);
    });

    it("tells the dashboard's own order from another, leaving out the tabs that never move", () => {
        const own = ["overview", "feed", "settings"];
        expect(isDefaultOrder(["overview", "feed"], own)).toBe(true);
        expect(isDefaultOrder(["feed", "overview"], own)).toBe(false);
        expect(isDefaultOrder(["overview"], own)).toBe(false);
    });

    it("reads a broken store as no order at all", () => {
        localStorage.setItem(TAB_ORDER_KEY, "{not json");
        expect(tabOrder()).toEqual([]);
        localStorage.setItem(TAB_ORDER_KEY, JSON.stringify({ feed: 1 }));
        expect(tabOrder()).toEqual([]);
    });

    it("goes back to the dashboard's own, and is handed out with the hidden tabs", () => {
        moveTab(["overview", "feed"], "feed", -1);
        setTabHidden("models", true);
        expect(tabChoices()).toEqual({ hidden: new Set(["models"]), order: ["feed", "overview"] });
        resetTabOrder();
        expect(tabChoices().order).toEqual([]);
    });
});

describe("Settings → Tabs, the order", () => {
    beforeEach(() => {
        localStorage.clear();
        document.body.replaceChildren();
    });

    const rowNames = (root: HTMLElement): string[] =>
        allByRole(root, "switch").map(n =>
            (n.getAttribute("aria-label") ?? "").replace(/^Show the (.*) tab$/, "$1"),
        );
    const moveButton = (root: HTMLElement, name: string): HTMLButtonElement =>
        byRole(root, "button", name) as HTMLButtonElement;

    it("lists the tabs that move in their order, then Devices and Settings, which do not", () => {
        localStorage.setItem(TAB_ORDER_KEY, JSON.stringify(["models", "overview"]));
        const root = buildTabsSection(host());

        expect(rowNames(root)).toEqual(["Models", "Overview", "Feed", "Devices", "Settings"]);
        expect(allByRole(root, "button", "Move Settings up")).toHaveLength(0);
        expect(allByRole(root, "button", "Move Devices down")).toHaveLength(0);
        expect(hintOf(root, box(root, "Settings"))).toBe("Always shown, always last.");
        expect(byRole(root, "heading", "In their own place")).toBeTruthy();
    });

    it("cannot move the first tab up or the last one down", () => {
        const root = buildTabsSection(host());
        expect(moveButton(root, "Move Overview up").disabled).toBe(true);
        expect(moveButton(root, "Move Models down").disabled).toBe(true);
        expect(moveButton(root, "Move Feed up").disabled).toBe(false);
    });

    it("moves a tab, says the header changed, and keeps the focus on the button pressed", () => {
        const h = host();
        const root = buildTabsSection(h);
        document.body.appendChild(root);

        moveButton(root, "Move Feed up").click();

        expect(rowNames(root).slice(0, 3)).toEqual(["Feed", "Overview", "Models"]);
        expect(h.changed).toHaveBeenCalledOnce();
        // At the start now: its up button is disabled, so its down button holds the focus.
        expect(document.activeElement).toBe(moveButton(root, "Move Feed down"));

        moveButton(root, "Move Feed down").click();
        expect(rowNames(root).slice(0, 3)).toEqual(["Overview", "Feed", "Models"]);
        expect(document.activeElement).toBe(moveButton(root, "Move Feed down"));
    });

    it("offers the default order only while the order is another", () => {
        const root = buildTabsSection(host());
        const reset = byRole(root, "button", "Default order");

        moveButton(root, "Move Feed up").click();
        expect(reset.hidden).toBe(false);
        moveButton(root, "Move Feed down").click();

        expect(tabOrder()).not.toEqual([]);
        expect(reset.hidden).toBe(true);
    });

    it("offers the default order once the order is changed, and goes back to it", () => {
        const h = host();
        const root = buildTabsSection(h);
        document.body.appendChild(root);
        const reset = byRole(root, "button", "Default order");
        expect(reset.hidden).toBe(true);

        moveButton(root, "Move Models up").click();
        expect(reset.hidden).toBe(false);

        reset.click();
        expect(rowNames(root).slice(0, 3)).toEqual(["Overview", "Feed", "Models"]);
        expect(tabOrder()).toEqual([]);
        expect(reset.hidden).toBe(true);
        expect(h.changed).toHaveBeenCalledTimes(2);
        // The button pressed is hidden now: focus goes to the top of the list, the first control that takes it.
        expect(document.activeElement).toBe(moveButton(root, "Move Overview down"));
    });
});
