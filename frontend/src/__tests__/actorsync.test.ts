/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ActorSync } from "../agents/actorSync";
import { log } from "../io/logger";

function answering(...replies: (() => Promise<Response>)[]): void {
    const fetched = vi.fn();
    replies.forEach(reply => fetched.mockImplementationOnce(reply));
    vi.stubGlobal("fetch", fetched);
}

const listing = (...names: string[]) =>
    Response.json(names.map(name => ({ id: `${name}-id`, name, state: "running" })));

describe("ActorSync", () => {
    const store = { reconcileAgents: vi.fn() };
    const sync = () => new ActorSync(store, () => false);

    beforeEach(() => {
        vi.clearAllMocks();
        delete window.__WACTORZ_INGRESS_PATH;
    });

    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it("reconciles the store with the server's list", async () => {
        answering(async () => listing("weather"));

        await sync().refresh();

        expect(store.reconcileAgents).toHaveBeenCalledTimes(1);
        expect(store.reconcileAgents.mock.calls[0]![0]).toHaveLength(1);
    });

    it("leaves out an agent just deleted", async () => {
        answering(async () => listing("weather", "gone"));

        await new ActorSync(store, id => id === "gone-id").refresh();

        const kept = store.reconcileAgents.mock.calls[0]![0] as { name: string }[];
        expect(kept.map(agent => agent.name)).toEqual(["weather"]);
    });

    it("asks once at a time, and again once the read is done", async () => {
        let finish: (res: Response) => void = () => {};
        const fetched = vi.fn(() => new Promise<Response>(resolve => (finish = resolve)));
        vi.stubGlobal("fetch", fetched);
        const syncing = sync();

        const first = syncing.refresh();
        await syncing.refresh();
        expect(fetched).toHaveBeenCalledTimes(1);

        finish(listing());
        await first;
        const second = syncing.refresh();
        expect(fetched).toHaveBeenCalledTimes(2);
        finish(listing());
        await second;
    });

    it("says an outage out loud once, however long it lasts", async () => {
        const warned = vi.spyOn(log, "warn");
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => new Response("", { status: 500 })),
        );
        const syncing = sync();

        for (let i = 0; i < 5; i++) {
            await syncing.refresh();
        }

        expect(warned).toHaveBeenCalledTimes(1);
        expect(store.reconcileAgents).not.toHaveBeenCalled();
    });

    it("survives a list it cannot read, and reads again next time", async () => {
        answering(
            async () => Response.json(null),
            async () => listing("weather"),
        );
        const syncing = sync();

        await syncing.refresh();
        await syncing.refresh();

        expect(store.reconcileAgents).toHaveBeenCalledTimes(1);
    });

    it("seeds once, however many times the page goes live", async () => {
        const fetched = vi.fn(async () => listing());
        vi.stubGlobal("fetch", fetched);
        const syncing = sync();

        await syncing.seedOnce();
        await syncing.seedOnce();

        expect(fetched).toHaveBeenCalledTimes(1);
    });
});
