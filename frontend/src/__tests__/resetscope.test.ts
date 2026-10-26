/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import { buildResetPopover, type ResetPopover } from "../ui/dashboard/popovers";
import { nodesSay, openAgent } from "../ui/dashboard/resetScope";
import { emit } from "../events";
import { toast } from "../ui/ToastManager";
import { blockingFindings } from "./axe";
import { allByRole, byRole } from "./queries";

function rows(pop: HTMLElement): string[] {
    return allByRole(pop, "button").map(b => b.textContent ?? "");
}

/** Arm and fire the row named `label`, and return the body it posted. */
async function fire(pop: HTMLElement, label: string): Promise<Record<string, unknown>> {
    const row = allByRole(pop, "button").find(b => b.textContent === label)!;
    row.click();
    row.click();
    await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
    const [, init] = vi.mocked(fetch).mock.calls[0]!;
    return JSON.parse((init as RequestInit).body as string) as Record<string, unknown>;
}

function answering(nodes: Record<string, string>): void {
    vi.stubGlobal(
        "fetch",
        vi.fn(async () => ({ ok: true, json: async () => ({ status: "ok", nodes }) })),
    );
}

describe("whom a reset is for", () => {
    let pop: ResetPopover | null = null;

    beforeEach(() => {
        document.body.innerHTML = "";
        vi.clearAllMocks();
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "";
        location.hash = "#/chat/counter";
    });

    afterEach(() => {
        pop?._release();
        pop = null;
        vi.unstubAllGlobals();
        location.hash = "";
    });

    it("reads the open agent from the chat's address, and no other view's", () => {
        expect(openAgent({ view: "chat", path: ["counter"] })).toBe("counter");
        expect(openAgent({ view: "chat", path: [] })).toBeNull();
        expect(openAgent({ view: "feed", path: ["counter"] })).toBeNull();
        expect(openAgent(null)).toBeNull();
    });

    it("offers the open agent first, with only what can be cleared for one", () => {
        pop = buildResetPopover();

        expect((byRole(pop, "radio", "@counter") as HTMLInputElement).checked).toBe(true);
        // Log files are every agent's, and wiping one agent is deleting it.
        expect(rows(pop)).toEqual([
            "Chat history & files",
            "Metrics & costs",
            "Spawn registry",
            "Agent state (restarts it)",
        ]);
    });

    it("clears for that agent, and says so", async () => {
        answering({ edge: "cleared" });
        pop = buildResetPopover();

        const body = await fire(pop, "Chat history & files");

        expect(body).toEqual({ scope: "chat", agent: "counter" });
        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(
                expect.objectContaining({ message: "Chat history & files cleared for @counter." }),
            ),
        );
    });

    it("clears for every agent once that is picked, with every row back", async () => {
        answering({});
        pop = buildResetPopover();

        const all = byRole(pop, "radio", "All agents") as HTMLInputElement;
        all.checked = true;
        all.dispatchEvent(new Event("change"));

        expect(rows(pop)).toHaveLength(6);
        expect((byRole(pop, "radio", "All agents") as HTMLInputElement).checked).toBe(true);
        expect(await fire(pop, "Metrics & costs")).toEqual({ scope: "metrics" });
    });

    it("names the nodes that did not clear their copy", async () => {
        answering({ edge: "cleared", pi: "no answer", old: "older release" });
        pop = buildResetPopover();

        await fire(pop, "Agent state (restarts it)");

        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(
                expect.objectContaining({
                    type: "alert-warning",
                    message: expect.stringContaining(
                        "Nodes not cleared: pi did not answer; old runs an older release; deploy it again.",
                    ),
                }),
            ),
        );
        expect(nodesSay({ edge: "cleared" })).toBe("");
        expect(nodesSay(undefined)).toBe("");
    });

    it("follows the open conversation, and stops once released", () => {
        pop = buildResetPopover();

        emit("af-route", { view: "chat", path: ["lamp"] });
        expect(byRole(pop, "radio", "@lamp")).toBeDefined();

        emit("af-view-changed", { view: "overview", path: [] });
        expect(allByRole(pop, "radio")).toEqual([]);
        expect(rows(pop)).toHaveLength(6);

        pop._release();
        emit("af-route", { view: "chat", path: ["lamp"] });
        expect(allByRole(pop, "radio")).toEqual([]);
    });

    it("passes axe with the choice shown", async () => {
        pop = buildResetPopover();
        document.body.appendChild(pop);

        expect(await blockingFindings(pop)).toEqual([]);
    });
});
