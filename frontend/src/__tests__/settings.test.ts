/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** Settings: its sections, each at its own address, and the Budget section. */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { SettingsView } from "../ui/settings/SettingsView";
import type { SettingsSection } from "../ui/settings/section";
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
        expect(view.section).toBe("");
        expect(view.build().textContent).toContain("tabs content");
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
        expect(view.build().textContent).toContain("Kept in this browser only.");
        view.show("budget");
        expect(view.build().textContent).toContain("Kept on the server");
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
    root.querySelector<HTMLInputElement>('input[name="cost-limit"]')!;

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
        const root = buildBudgetSection(null, cbs());
        expect(root.querySelector('input[type="password"]')).toBeNull();
    });

    it("prefills the limit and period from cost info", () => {
        const root = buildBudgetSection({ limit_usd: 12.5, period: "weekly", spend_usd: 3 }, cbs());
        expect(limitField(root).value).toBe("12.5");
        expect((byRole(root, "combobox", "Cost limit period") as HTMLSelectElement).value).toBe("weekly");
        expect(root.textContent).toContain("$3.0000 / $12.50 this week");
    });

    it("shows the no-limit status when no limit is set", () => {
        expect(buildBudgetSection({ spend_usd: 1 }, cbs()).textContent).toContain("(no limit set)");
    });

    it("Save limit calls onSaveLimit with the entered value + period", async () => {
        const cb = cbs();
        const root = buildBudgetSection(null, cb);
        limitField(root).value = "5";
        (byRole(root, "combobox", "Cost limit period") as HTMLSelectElement).value = "daily";
        press(root, "Save limit");
        await vi.waitFor(() => expect(cb.onSaveLimit).toHaveBeenCalledWith(5, "daily"));
    });

    it("Save limit ignores a negative value", async () => {
        const cb = cbs();
        const root = buildBudgetSection(null, cb);
        limitField(root).value = "-3";
        press(root, "Save limit");
        await Promise.resolve();
        expect(cb.onSaveLimit).not.toHaveBeenCalled();
    });

    it("Reset spend asks in the dashboard's own dialog before calling onResetSpend", async () => {
        const cb = cbs();
        const root = buildBudgetSection(null, cb);
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
