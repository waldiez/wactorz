/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { buildHAView } from "../ui/dashboard/haView";

function fillForm(el: HTMLElement, host: string, token: string, tls = false): void {
    el.querySelector<HTMLInputElement>("#ha-cfg-url")!.value = host;
    el.querySelector<HTMLInputElement>("#ha-cfg-token")!.value = token;
    el.querySelector<HTMLInputElement>("#ha-cfg-tls")!.checked = tls;
}

describe("buildHAView", () => {
    beforeEach(() => {
        localStorage.clear();
        vi.useFakeTimers();
    });
    afterEach(() => {
        vi.useRealTimers();
    });

    it("shows the config form when credentials are missing", () => {
        const el = buildHAView(null, null, { onApply: vi.fn() });
        expect(el.querySelector("#ha-cfg-url")).not.toBeNull();
        expect(el.querySelector("#ha-devices-container")).toBeNull();
        // No saved host yet → no Reset button.
        expect(el.querySelector("#ha-cfg-clear")).toBeNull();
    });

    it("shows the device panel when configured, linking to the HA url", () => {
        const el = buildHAView("http://ha.local", "tok", { onApply: vi.fn() });
        expect(el.querySelector("#ha-devices-container")).not.toBeNull();
        expect(el.querySelector<HTMLAnchorElement>("#ha-open-link")!.getAttribute("href")).toBe(
            "http://ha.local",
        );
    });

    it("saves credentials (http by default) and calls onApply after the delay", () => {
        const onApply = vi.fn();
        const el = buildHAView(null, null, { onApply });
        fillForm(el, "192.168.1.2:8123", "tok");
        el.querySelector<HTMLButtonElement>("#ha-cfg-save")!.click();

        expect(localStorage.getItem("wactorz-ha-url")).toBe("http://192.168.1.2:8123");
        expect(localStorage.getItem("wactorz-ha-token")).toBe("tok");
        expect(onApply).not.toHaveBeenCalled(); // deferred
        vi.advanceTimersByTime(600);
        expect(onApply).toHaveBeenCalledTimes(1);
    });

    it("uses https when the TLS checkbox is ticked", () => {
        const el = buildHAView(null, null, { onApply: vi.fn() });
        fillForm(el, "ha.local", "tok", true);
        el.querySelector<HTMLButtonElement>("#ha-cfg-save")!.click();
        expect(localStorage.getItem("wactorz-ha-url")).toBe("https://ha.local");
    });

    it("honours an explicit protocol prefix over the checkbox", () => {
        const el = buildHAView(null, null, { onApply: vi.fn() });
        // https in the URL but checkbox unticked → https wins
        fillForm(el, "https://secure.ha", "tok", false);
        el.querySelector<HTMLButtonElement>("#ha-cfg-save")!.click();
        expect(localStorage.getItem("wactorz-ha-url")).toBe("https://secure.ha");
    });

    it("rejects empty fields with a message and does not save", () => {
        const onApply = vi.fn();
        const el = buildHAView(null, null, { onApply });
        el.querySelector<HTMLButtonElement>("#ha-cfg-save")!.click();
        expect(el.querySelector("#ha-cfg-msg")!.textContent).toBe("Both fields required.");
        expect(localStorage.getItem("wactorz-ha-url")).toBeNull();
        vi.advanceTimersByTime(600);
        expect(onApply).not.toHaveBeenCalled();
    });

    it("reconfigure → Reset clears stored credentials and re-applies", () => {
        const onApply = vi.fn();
        const el = buildHAView("http://ha.local", "tok", { onApply });
        // open the config form from the device panel
        el.querySelector<HTMLButtonElement>("#ha-reconfigure-btn")!.click();
        const clear = el.querySelector<HTMLButtonElement>("#ha-cfg-clear")!;
        expect(clear).not.toBeNull(); // shown because a host is stored
        localStorage.setItem("wactorz-ha-url", "http://ha.local");
        localStorage.setItem("wactorz-ha-token", "tok");
        clear.click();
        expect(localStorage.getItem("wactorz-ha-url")).toBeNull();
        expect(localStorage.getItem("wactorz-ha-token")).toBeNull();
        expect(onApply).toHaveBeenCalled();
    });
});
