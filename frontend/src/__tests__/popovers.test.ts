/**
 * SPDX-License-Identifier: Apache-2.0
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import { buildResetPopover } from "../ui/shell/popovers";
import { RESET_TIMEOUT_MS } from "../io/resetApi";
import { toast } from "../ui/ToastManager";

describe("buildResetPopover", () => {
    const origFetch = globalThis.fetch;
    beforeEach(() => {
        document.body.innerHTML = "";
        vi.clearAllMocks();
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "";
    });
    afterEach(() => {
        globalThis.fetch = origFetch;
        vi.useRealTimers();
    });

    it("renders a button per reset scope", () => {
        const pop = buildResetPopover();
        // 6 scopes
        expect(pop.querySelectorAll("button").length).toBe(6);
    });

    it("requires two clicks to fire, then POSTs the reset and toasts", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ nodes: {} }),
        })) as unknown as typeof fetch;
        const pop = buildResetPopover();
        const btn = pop.querySelector<HTMLButtonElement>("button")!; // first = chat
        btn.click(); // arm
        expect(btn.querySelector("span")!.textContent).toBe("Confirm chat history & files?");
        expect(fetch).not.toHaveBeenCalled();
        btn.click(); // fire
        await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/reset", expect.anything()));
        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ title: "Reset" })),
        );
    });

    it("arming one button disarms the others", () => {
        const pop = buildResetPopover();
        const [first, second] = [...pop.querySelectorAll<HTMLButtonElement>("button")];
        first!.click();
        expect(first!.querySelector("span")!.textContent).toContain("Confirm");
        second!.click();
        // first disarmed back to its label, second now armed
        expect(first!.querySelector("span")!.textContent).toBe("Chat history & files");
        expect(second!.querySelector("span")!.textContent).toContain("Confirm");
    });

    it("auto-disarms after the timeout", () => {
        vi.useFakeTimers();
        const pop = buildResetPopover();
        const btn = pop.querySelector<HTMLButtonElement>("button")!;
        btn.click();
        expect(btn.querySelector("span")!.textContent).toContain("Confirm");
        vi.advanceTimersByTime(3000);
        expect(btn.querySelector("span")!.textContent).toBe("Chat history & files");
    });

    it("toasts an error with the server message when the reset response is not ok", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: false,
            status: 500,
            json: async () => ({ error: "boom" }),
        })) as unknown as typeof fetch;
        const pop = buildResetPopover();
        const btn = pop.querySelector<HTMLButtonElement>("button")!;
        btn.click(); // arm
        btn.click(); // fire
        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(
                expect.objectContaining({ title: "Reset failed", message: "boom" }),
            ),
        );
    });

    it("does not call a reset that timed out failed: the server may be finishing it", async () => {
        vi.useFakeTimers();
        globalThis.fetch = vi.fn(
            (_url: string, init?: RequestInit) =>
                new Promise((_resolve, reject) =>
                    init?.signal?.addEventListener("abort", () => reject(new Error("aborted"))),
                ),
        ) as unknown as typeof fetch;
        const pop = buildResetPopover();
        const btn = pop.querySelector<HTMLButtonElement>("button")!;
        btn.click(); // arm
        btn.click(); // fire
        await vi.advanceTimersByTimeAsync(RESET_TIMEOUT_MS + 1);

        expect(toast.show).toHaveBeenCalledWith(
            expect.objectContaining({ type: "alert-warning", title: "Reset unconfirmed" }),
        );
    });

    it("falls back to the status code when the error body is unparseable", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: false,
            status: 503,
            json: async () => {
                throw new Error("not json");
            },
        })) as unknown as typeof fetch;
        const pop = buildResetPopover();
        const btn = pop.querySelector<HTMLButtonElement>("button")!;
        btn.click(); // arm
        btn.click(); // fire
        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(
                expect.objectContaining({ title: "Reset failed", message: "HTTP 503" }),
            ),
        );
    });

    it("toasts an error when the reset request throws", async () => {
        globalThis.fetch = vi.fn(async () => {
            throw new Error("network down");
        }) as unknown as typeof fetch;
        const pop = buildResetPopover();
        const btn = pop.querySelector<HTMLButtonElement>("button")!;
        btn.click(); // arm
        btn.click(); // fire
        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ title: "Reset failed" })),
        );
    });
});
