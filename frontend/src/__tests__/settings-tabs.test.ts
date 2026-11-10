/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** Which tabs this browser hides, and Settings → Tabs, where they are chosen. */
import { describe, it, expect, vi, beforeEach } from "vitest";

import { canHide, DEVICES_TAB, hiddenTabs, setTabHidden } from "../config/hiddenTabs";
import { HIDDEN_TABS_KEY } from "../config/keys";
import { buildTabsSection, tabsSection, type TabsSectionHost } from "../ui/settings/tabs";
import { byRole } from "./queries";

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
    return byRole(root, "checkbox", `Show the ${label} tab`) as HTMLInputElement;
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
        expect(root.textContent).toContain("always shown");
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
        expect(box(root, "Devices").closest("label")!.textContent).toContain(
            "shown once Home Assistant is set up",
        );
        const configured = buildTabsSection(host());
        expect(box(configured, "Devices").closest("label")!.textContent).not.toContain("Home Assistant");
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
