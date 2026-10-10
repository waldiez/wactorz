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

    it("holds no Home Assistant fields — the URL comes from /api/config", () => {
        const el = buildSettingsView(null, cbs());
        const headings = [...el.querySelectorAll(".af-settings-section-heading")].map(h => h.textContent);
        expect(headings).toContain("🪙 LLM Spend Limit");
        expect(el.querySelector(".af-settings-title")!.textContent).toBe("Settings");
        // No HA token field should exist anywhere in settings.
        expect(el.querySelector('input[type="password"]')).toBeNull();
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

    it("names the period the spend is counted over", () => {
        const el = buildSettingsView({ limit_usd: 20, period: "monthly", spend_usd: 4 }, cbs());
        expect(el.querySelector(".af-settings-note")!.textContent).toContain("this month");
    });

    it("reads as nothing spent when there is no cost information at all", () => {
        const el = buildSettingsView(null, cbs());
        expect(el.querySelector(".af-settings-note")!.textContent).toContain("$0.0000");
    });

    it("counts a daily limit against today", () => {
        const el = buildSettingsView({ limit_usd: 5, period: "daily", spend_usd: 1 }, cbs());
        expect(el.querySelector(".af-settings-note")!.textContent).toContain("today");
    });

    it("leaves out the voice section when this deployment neither listens nor speaks", () => {
        localStorage.setItem("wactorz-stt-mode", "off");
        localStorage.setItem("wactorz-tts-mode", "off");
        const headings = [
            ...buildSettingsView(null, cbs()).querySelectorAll(".af-settings-section-heading"),
        ].map(h => h.textContent);

        expect(headings).not.toContain("Voice");
    });

    it("hands the voice section the prefix and the callback it was given", () => {
        localStorage.setItem("wactorz-stt-mode", "host");
        const onVoiceChanged = vi.fn();
        const asked: string[] = [];
        vi.stubGlobal(
            "fetch",
            vi.fn((url: string) => {
                asked.push(url);
                return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
            }),
        );

        const el = buildSettingsView(null, cbs(), "/ha", onVoiceChanged);
        byText<HTMLButtonElement>(el, "button", "Test microphone").click();

        expect(asked).toEqual(["/ha/api/stt/listen"]);
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
});
