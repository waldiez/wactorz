/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** Settings: its sections, each at its own address, and the Budget section. */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { SettingsView } from "../ui/settings/SettingsView";
import type { SettingsSection } from "../types/settings";
import type { CostLimitInfo } from "../io/costApi";
import { buildBudgetSection, type CostLimitCallbacks } from "../ui/settings/budget";
import { allByRole, byRole } from "./queries";

function section(key: string, over: Partial<SettingsSection> = {}): SettingsSection {
    return {
        key,
        label: key.charAt(0).toUpperCase() + key.slice(1),
        line: `all about ${key}`,
        scope: "browser",
        build: () => {
            const body = document.createElement("p");
            body.textContent = `${key} content`;
            return body;
        },
        ...over,
    };
}

describe("SettingsView", () => {
    beforeEach(() => {
        document.body.replaceChildren();
    });

    it("lists every section as a link to its address", () => {
        const view = new SettingsView([section("tabs"), section("sound")]);
        const root = view.build();

        const nav = byRole(root, "navigation", "Settings sections");
        const links = allByRole(nav, "link");
        expect(links.map(a => a.getAttribute("href"))).toEqual(["#/settings/tabs", "#/settings/sound"]);
    });

    it("opens the first section when the address names none, or one there is not", () => {
        const view = new SettingsView([section("tabs"), section("sound")]);
        expect(view.build().textContent).toContain("tabs content");

        view.show("nowhere");
        // The address keeps asking for it, as it does for a view an extension has not registered yet.
        expect(view.section).toBe("nowhere");
        expect(view.build().textContent).toContain("tabs content");
    });

    it("opens a section asked for before it registered, once it does", () => {
        const view = new SettingsView([section("tabs")]);
        view.show("avatars");
        const root = view.build();
        expect(byRole(root, "region", "Tabs")).toBeTruthy();

        view.register(section("avatars"));

        expect(byRole(root, "region", "Avatars").textContent).toContain("avatars content");
        expect(root.querySelector('[aria-current="page"]')!.getAttribute("href")).toBe("#/settings/avatars");
    });

    it("lists a section added while Settings is open, leaving the one open alone", () => {
        const view = new SettingsView([section("tabs")]);
        const root = view.build();
        const shown = byRole(root, "region", "Tabs");

        view.register(section("avatars"));

        expect(allByRole(root, "link").map(a => a.getAttribute("href"))).toEqual([
            "#/settings/tabs",
            "#/settings/avatars",
        ]);
        expect(byRole(root, "region", "Tabs")).toBe(shown);
    });

    it("draws nothing for a section added while Settings is closed", () => {
        const view = new SettingsView([section("tabs")]);
        const root = view.build();
        view.release();
        const before = root.innerHTML;

        view.register(section("avatars"));

        expect(root.innerHTML).toBe(before);
    });

    it("shows each section's icon in the list, and the settings icon for one that names none", () => {
        const view = new SettingsView([
            section("tabs", { icon: "panels" }),
            section("sound", { icon: "nowhere" }),
        ]);
        const icons = [...view.build().querySelectorAll("nav a svg")];
        expect(icons).toHaveLength(2);
    });

    it("opens the section the address names, and marks it in the list", () => {
        const view = new SettingsView([section("tabs"), section("sound")]);
        view.show("sound");
        const root = view.build();

        expect(view.section).toBe("sound");
        expect(byRole(root, "region", "Sound").textContent).toContain("sound content");
        expect(root.querySelector('[aria-current="page"]')!.getAttribute("href")).toBe("#/settings/sound");
    });

    it("says where a section's values are kept", () => {
        const view = new SettingsView([section("tabs"), section("budget", { scope: "server" })]);
        expect(view.build().textContent).toContain("Kept in this browser");
        view.show("budget");
        expect(view.build().textContent).toContain("Kept on the server, for everyone");
    });

    it("releases the section drawn before drawing another, and when left", () => {
        const release = vi.fn();
        const view = new SettingsView([section("sound", { release }), section("tabs")]);
        view.build();
        expect(release).not.toHaveBeenCalled();

        view.show("tabs");
        view.build();
        expect(release).toHaveBeenCalledOnce();

        view.show("sound");
        view.build();
        view.release();
        expect(release).toHaveBeenCalledTimes(2);
        view.release();
        expect(release).toHaveBeenCalledTimes(2);
    });

    it("draws one section again in place, leaving the list alone", () => {
        let drawn = 0;
        const view = new SettingsView([
            section("budget", {
                build: () => {
                    drawn += 1;
                    const body = document.createElement("p");
                    body.textContent = `budget ${drawn}`;
                    return body;
                },
            }),
            section("tabs"),
        ]);
        const root = view.build();
        const link = allByRole(root, "link")[0];

        view.redraw("budget");

        expect(root.textContent).toContain("budget 2");
        expect(root.textContent).not.toContain("budget 1");
        expect(allByRole(root, "link")[0]).toBe(link);
    });

    it("draws nothing again for a section that is not the one open", () => {
        const build = vi.fn(() => document.createElement("p"));
        const view = new SettingsView([section("tabs"), section("budget", { build })]);
        const root = view.build();
        const shown = root.querySelector("section");

        view.redraw("budget");
        view.release();
        view.redraw("tabs");

        expect(build).not.toHaveBeenCalled();
        expect(root.querySelector("section")).toBe(shown);
    });

    it("takes a section added later, or one in place of its key", () => {
        const view = new SettingsView([section("tabs")]);
        view.register(section("avatars"));
        view.register(section("tabs", { label: "Header tabs" }));

        const links = allByRole(view.build(), "link");
        expect(links.map(a => a.textContent)).toEqual([
            "Header tabsall about tabs",
            "Avatarsall about avatars",
        ]);
    });

    it("draws an empty page when it has no sections", () => {
        expect(new SettingsView([]).build().textContent).toBe("Settings");
    });
});

function cbs(): CostLimitCallbacks {
    return { onSaveLimit: vi.fn(), onResetSpend: vi.fn() };
}

const limitField = (root: HTMLElement): HTMLInputElement =>
    byRole(root, "spinbutton", "Spend limit (USD)") as HTMLInputElement;

/** The Budget section's content, as Settings draws it. */
const budget = (info: CostLimitInfo | null, cb: CostLimitCallbacks): HTMLElement =>
    buildBudgetSection(info, cb).body;

const press = (root: HTMLElement, name: string): void =>
    (byRole(root, "button", name) as HTMLButtonElement).click();

/** Answer the confirm dialog that is open. */
function answer(confirm: boolean): void {
    document.querySelector<HTMLButtonElement>(confirm ? ".af-confirm-ok" : ".af-confirm-cancel")!.click();
}

describe("Settings → Budget", () => {
    beforeEach(() => {
        localStorage.clear();
        document.body.replaceChildren();
    });
    afterEach(() => vi.restoreAllMocks());

    it("holds no Home Assistant field: its URL comes from /api/config, and no token reaches the browser", () => {
        const root = budget(null, cbs());
        expect(root.querySelector('input[type="password"]')).toBeNull();
    });

    it("prefills the limit and period from cost info", () => {
        const root = budget({ limit_usd: 12.5, period: "weekly", spend_usd: 3 }, cbs());
        expect(limitField(root).value).toBe("12.5");
        expect((byRole(root, "combobox", "Cost limit period") as HTMLSelectElement).value).toBe("weekly");
        expect(root.textContent).toContain("$3.0000 of $12.50 spent this week");
    });

    it("reads a period it does not know as monthly, in the field and the spend line alike", () => {
        const root = budget({ period: "", spend_usd: 0 }, cbs());
        expect((byRole(root, "combobox", "Cost limit period") as HTMLSelectElement).value).toBe("monthly");
        expect(root.textContent).toContain("spent this month");
    });

    it("shows the no-limit status when no limit is set, and no bar", () => {
        const root = budget({ spend_usd: 1 }, cbs());
        expect(root.textContent).toContain("spent this month, with no limit set");
        expect(byRole(root, "meter", "Share of the limit spent").hidden).toBe(true);
    });

    it("shows the share of the limit spent as a bar, warning near the limit and at it", () => {
        const drawn = buildBudgetSection({ limit_usd: 10, spend_usd: 2, period: "daily" }, cbs());
        const bar = byRole(drawn.body, "meter", "Share of the limit spent");
        expect([bar.hidden, bar.getAttribute("aria-valuenow")]).toEqual([false, "20"]);
        expect(bar.className).toBe("af-budget-bar");

        drawn.showSpend({ limit_usd: 10, spend_usd: 8.5, period: "daily" });
        expect(bar.classList.contains("near")).toBe(true);

        drawn.showSpend({ limit_usd: 10, spend_usd: 14, period: "daily" });
        expect([bar.getAttribute("aria-valuenow"), bar.classList.contains("over")]).toEqual(["100", true]);
        expect(bar.classList.contains("near")).toBe(false);
    });

    it("shows a spend read since without touching the fields, so what is being typed stays", () => {
        const drawn = buildBudgetSection({ limit_usd: 10, spend_usd: 2, period: "monthly" }, cbs());
        document.body.appendChild(drawn.body);
        const field = limitField(drawn.body);
        field.focus();
        field.value = "25";

        drawn.showSpend({ limit_usd: 10, spend_usd: 3, period: "monthly" });

        expect(limitField(drawn.body)).toBe(field);
        expect([field.value, document.activeElement]).toEqual(["25", field]);
        expect(drawn.body.textContent).toContain("$3.0000 of $10.00 spent this month");
    });

    it("says what each field is for, read out with it", () => {
        const root = budget(null, cbs());
        const describedBy = limitField(root).getAttribute("aria-describedby")!;
        expect(root.querySelector(`[id="${describedBy}"]`)!.textContent).toBe(
            "In US dollars; 0 for no limit.",
        );
    });

    it("Save limit calls onSaveLimit with the entered value + period", async () => {
        const cb = cbs();
        const root = budget(null, cb);
        limitField(root).value = "5";
        (byRole(root, "combobox", "Cost limit period") as HTMLSelectElement).value = "daily";
        press(root, "Save limit");
        await vi.waitFor(() => expect(cb.onSaveLimit).toHaveBeenCalledWith(5, "daily"));
    });

    it("Save limit ignores a negative value", async () => {
        const cb = cbs();
        const root = budget(null, cb);
        limitField(root).value = "-3";
        press(root, "Save limit");
        await Promise.resolve();
        expect(cb.onSaveLimit).not.toHaveBeenCalled();
    });

    it("Reset spend asks in the dashboard's own dialog before calling onResetSpend", async () => {
        const cb = cbs();
        const root = budget(null, cb);
        document.body.appendChild(root);

        press(root, "Reset spend");
        expect(byRole(document, "dialog", "Reset the period budget counter?")).toBeTruthy();
        answer(false);
        await vi.waitFor(() => expect(document.querySelector(".af-confirm-backdrop")).toBeNull());
        expect(cb.onResetSpend).not.toHaveBeenCalled();

        await vi.waitFor(() =>
            expect((byRole(root, "button", "Reset spend") as HTMLButtonElement).disabled).toBe(false),
        );
        press(root, "Reset spend");
        answer(true);
        await vi.waitFor(() => expect(cb.onResetSpend).toHaveBeenCalledOnce());
    });
});
