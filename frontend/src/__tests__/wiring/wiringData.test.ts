/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * When the wiring is read, and what a read that fails leaves: the newest
 * request wins, a failure keeps what was read and says it is stale, a 503 is
 * said as no registry, and nothing is read while the view is not showing.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Answer } from "../../io/api";
import type { Wiring } from "../../types/wiring";
import { REFRESH_MS, SETTLE_MS, WiringData, type WiringState } from "../../ui/wiring/wiringData";
import { wiring } from "./fixtures";

const ok = (value: Wiring = wiring()): Answer<Wiring> => ({ ok: true, value });

function controller(answers: Answer<Wiring>[], showing = true) {
    const read = vi.fn(async () => answers.shift() ?? ok());
    const host = { showing };
    const data = new WiringData({ isShowing: () => host.showing }, read);
    const seen: WiringState[] = [];
    data.subscribe(state => seen.push(state));
    return { data, read, host, seen };
}

describe("the wiring's data", () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it("is read when the view opens, and is then ready", async () => {
        const { data, seen } = controller([ok()]);

        data.opened();
        await vi.runOnlyPendingTimersAsync();

        expect(seen.map(s => s.status)).toEqual(["idle", "loading", "ready"]);
        expect(data.state.wiring?.agents).toHaveLength(5);
        expect(data.state.readAt).not.toBeNull();
    });

    it("keeps what was read when a later read fails, and says why", async () => {
        const { data } = controller([ok(), { ok: false, error: "the server went away", status: 502 }]);

        await data.refresh();
        await data.refresh();

        expect(data.state).toMatchObject({ status: "ready", error: "the server went away", reading: false });
        expect(data.state.wiring).not.toBeNull();
    });

    it("is an error when nothing was read yet", async () => {
        const { data } = controller([{ ok: false, error: "timed out", timedOut: true }]);

        await data.refresh();

        expect(data.state).toMatchObject({ status: "error", error: "timed out", wiring: null });
    });

    it("says a 503 as the server having no registry to read", async () => {
        const { data } = controller([ok(), { ok: false, error: "no actor system", status: 503 }]);

        await data.refresh();
        await data.refresh();

        expect(data.state).toMatchObject({ status: "unavailable", error: "no actor system", wiring: null });
    });

    it("drops an answer to a request asked before a newer one", async () => {
        let release: (answer: Answer<Wiring>) => void = () => undefined;
        const slow = new Promise<Answer<Wiring>>(resolve => (release = resolve));
        const newer = { ...wiring(), agents: [] };
        const read = vi.fn().mockReturnValueOnce(slow).mockResolvedValueOnce(ok(newer));
        const data = new WiringData({ isShowing: () => true }, read);

        const first = data.refresh();
        await data.refresh();
        release(ok());
        await first;

        expect(data.state.wiring?.agents).toEqual([]);
    });

    it("waits out a burst of changes and reads once, only while showing", async () => {
        const { data, read, host } = controller([]);

        data.changed();
        data.changed();
        data.changed();
        await vi.advanceTimersByTimeAsync(SETTLE_MS);
        expect(read).toHaveBeenCalledTimes(1);

        host.showing = false;
        data.changed();
        await vi.advanceTimersByTimeAsync(SETTLE_MS);
        expect(read).toHaveBeenCalledTimes(1);
    });

    it("reads on a timer while showing, and again when the page comes back", async () => {
        const { data, read, host } = controller([]);
        data.start();

        await vi.advanceTimersByTimeAsync(REFRESH_MS);
        expect(read).toHaveBeenCalledTimes(1);
        host.showing = false;
        await vi.advanceTimersByTimeAsync(REFRESH_MS);
        expect(read).toHaveBeenCalledTimes(1);

        host.showing = true;
        document.dispatchEvent(new Event("visibilitychange"));
        await vi.advanceTimersByTimeAsync(0);
        expect(read).toHaveBeenCalledTimes(2);

        data.stop();
        await vi.advanceTimersByTimeAsync(REFRESH_MS * 2);
        document.dispatchEvent(new Event("visibilitychange"));
        expect(read).toHaveBeenCalledTimes(2);
    });

    it("drops a read under way when stopped", async () => {
        let release: (answer: Answer<Wiring>) => void = () => undefined;
        const read = vi.fn(() => new Promise<Answer<Wiring>>(resolve => (release = resolve)));
        const data = new WiringData({ isShowing: () => true }, read);

        const pending = data.refresh();
        data.stop();
        release(ok());
        await pending;

        expect(data.state).toMatchObject({ status: "loading", wiring: null, reading: false });
    });

    it("stops telling a listener that unsubscribed", () => {
        const { data, seen } = controller([]);
        const listener = vi.fn();
        const stop = data.subscribe(listener);

        stop();
        void data.refresh();

        expect(listener).toHaveBeenCalledTimes(1);
        expect(seen.length).toBeGreaterThan(1);
    });
});
