/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { buildSettingsView, type CostLimitCallbacks } from "../ui/dashboard/settings";

function cbs(): CostLimitCallbacks {
    return { onSaveLimit: vi.fn(), onResetSpend: vi.fn() };
}

const byText = <T extends HTMLElement>(root: HTMLElement, sel: string, text: string): T =>
    [...root.querySelectorAll<T>(sel)].find(b => b.textContent === text)!;

describe("buildSettingsView", () => {
    beforeEach(() => {
        localStorage.clear();
        document.body.innerHTML = "";
    });
    afterEach(() => vi.restoreAllMocks());

    it("renders the spend-limit and Home Assistant sections", () => {
        const el = buildSettingsView(null, cbs());
        const headings = [...el.querySelectorAll(".af-settings-section-heading")].map(h => h.textContent);
        expect(headings).toEqual(["🪙 LLM Spend Limit", "🏠 Home Assistant"]);
        expect(el.querySelector(".af-settings-title")!.textContent).toBe("Settings");
    });

    it("prefills the limit and period from cost info", () => {
        const el = buildSettingsView({ limit_usd: 12.5, period: "weekly", spend_usd: 3 }, cbs());
        expect(el.querySelector<HTMLInputElement>('input[type="number"]')!.value).toBe("12.5");
        expect(el.querySelector<HTMLSelectElement>("select")!.value).toBe("weekly");
        expect(el.querySelector(".af-settings-note")!.textContent).toContain("$3.0000 / $12.50 this week");
    });

    it("shows the no-limit status when no limit is set", () => {
        const el = buildSettingsView({ spend_usd: 1 }, cbs());
        expect(el.querySelector(".af-settings-note")!.textContent).toContain("(no limit set)");
    });

    it("Save limit calls onSaveLimit with the entered value + period", async () => {
        const cb = cbs();
        const el = buildSettingsView(null, cb);
        el.querySelector<HTMLInputElement>('input[type="number"]')!.value = "5";
        el.querySelector<HTMLSelectElement>("select")!.value = "daily";
        byText<HTMLButtonElement>(el, "button", "Save limit").click();
        await vi.waitFor(() => expect(cb.onSaveLimit).toHaveBeenCalledWith(5, "daily"));
    });

    it("Save limit ignores a negative value", async () => {
        const cb = cbs();
        const el = buildSettingsView(null, cb);
        el.querySelector<HTMLInputElement>('input[type="number"]')!.value = "-3";
        byText<HTMLButtonElement>(el, "button", "Save limit").click();
        await Promise.resolve();
        expect(cb.onSaveLimit).not.toHaveBeenCalled();
    });

    it("Reset spend asks for confirmation before calling onResetSpend", async () => {
        const cb = cbs();
        const el = buildSettingsView(null, cb);
        window.confirm = vi.fn(() => false);
        byText<HTMLButtonElement>(el, "button", "Reset spend").click();
        await Promise.resolve();
        expect(cb.onResetSpend).not.toHaveBeenCalled();

        window.confirm = vi.fn(() => true);
        byText<HTMLButtonElement>(el, "button", "Reset spend").click();
        await vi.waitFor(() => expect(cb.onResetSpend).toHaveBeenCalled());
    });

    it("HA fields prefill from localStorage and the reveal toggle flips the token input", () => {
        localStorage.setItem("wactorz-ha-url", "http://ha.local");
        const el = buildSettingsView(null, cbs());
        const url = el.querySelector<HTMLInputElement>('input[placeholder^="http://homeassistant"]')!;
        expect(url.value).toBe("http://ha.local");
        const token = el.querySelector<HTMLInputElement>('input[type="password"]')!;
        expect(token.type).toBe("password");
        el.querySelector<HTMLButtonElement>(".af-settings-eye")!.click();
        expect(token.type).toBe("text");
    });

    it("Save persists non-empty fields to localStorage and clears empty ones", () => {
        localStorage.setItem("wactorz-ha-token", "old");
        const el = buildSettingsView(null, cbs());
        const url = el.querySelector<HTMLInputElement>('input[placeholder^="http://homeassistant"]')!;
        url.value = "http://new";
        const token = el.querySelector<HTMLInputElement>('input[type="password"]')!;
        token.value = ""; // cleared
        byText<HTMLButtonElement>(el, "button", "Save").click();
        expect(localStorage.getItem("wactorz-ha-url")).toBe("http://new");
        expect(localStorage.getItem("wactorz-ha-token")).toBeNull();
    });
});
