/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The dashboard tells the wiring when what it shows may have changed: an
 * agent came or went, a model's pointer moved, an agent's questions changed.
 * An agent's record changing in place -- a heartbeat -- is not one of them.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { emit } from "../../events";
import { WiringData } from "../../ui/wiring/wiringData";
import { agent, goTo, openDashboard, settled, shownView, type Opened } from "../dashboard";
import { wiring } from "./fixtures";

describe("the dashboard and the wiring", () => {
    let opened: Opened;
    let changed: ReturnType<typeof vi.spyOn>;
    const asked: string[] = [];

    beforeEach(() => {
        asked.length = 0;
        vi.stubGlobal(
            "fetch",
            vi.fn(async (url: string) => {
                asked.push(url);
                return Response.json(url.endsWith("/api/wiring") ? wiring() : {});
            }),
        );
        changed = vi.spyOn(WiringData.prototype, "changed");
        opened = openDashboard([agent("weather")]);
    });

    afterEach(() => {
        opened.dashboard.destroy();
        changed.mockRestore();
        vi.unstubAllGlobals();
    });

    it("has a Wiring view that reads the wiring when it opens", async () => {
        goTo("wiring");

        expect(shownView()).toBe("wiring");
        await vi.waitFor(() => expect(asked.filter(u => u.endsWith("/api/wiring"))).toHaveLength(1));
        await vi.waitFor(() => expect(document.querySelector(".af-wire-node")).not.toBeNull());
    });

    it("is told when an agent comes or goes, but not when one only changes", async () => {
        changed.mockClear();
        opened.store.addOrUpdateAgent(agent("weather", { messagesProcessed: 4 }));
        await settled();
        expect(changed).not.toHaveBeenCalled();

        opened.store.addOrUpdateAgent(agent("flic"));
        await settled();
        opened.store.removeAgent("flic");
        await settled();
        expect(changed).toHaveBeenCalledTimes(2);
    });

    it("is told when a model's pointer moves or an agent's questions change", () => {
        changed.mockClear();

        emit("af-model-pointer", { name: "air-quality", pointer: "shadow" });
        emit("af-question-book", { agentId: "watch", questions: [] });

        expect(changed).toHaveBeenCalledTimes(2);
    });
});
