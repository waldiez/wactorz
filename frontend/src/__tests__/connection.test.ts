/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Connection } from "../io/connection";

describe("Connection", () => {
    const heard: unknown[] = [];
    const onStatus = (e: Event) => heard.push(["status", (e as CustomEvent).detail.status]);
    const onSocket = (e: Event) => heard.push(["socket", (e as CustomEvent).detail.open]);
    const wentLive = vi.fn(() => heard.push("went live"));

    beforeEach(() => {
        heard.length = 0;
        wentLive.mockClear();
        document.addEventListener("af-connection-status", onStatus);
        document.addEventListener("af-socket", onSocket);
    });

    afterEach(() => {
        document.removeEventListener("af-connection-status", onStatus);
        document.removeEventListener("af-socket", onSocket);
    });

    it("is live only with the socket open and the broker up, in either order", () => {
        const first = new Connection(wentLive);
        first.socket(true);
        expect(first.live).toBe(false);
        first.broker(true);
        expect(first.live).toBe(true);

        const second = new Connection(wentLive);
        second.broker(true);
        expect(second.live).toBe(false);
        second.socket(true);
        expect(second.live).toBe(true);
    });

    it("says it is live, then acts on it", () => {
        const connection = new Connection(wentLive);
        connection.broker(true);
        connection.socket(true);

        expect(heard).toEqual([["socket", true], ["status", "live"], "went live"]);
    });

    it("says each change once", () => {
        const connection = new Connection(wentLive);
        connection.socket(true);
        connection.broker(true);
        connection.broker(true);
        heard.length = 0;

        connection.broker(false);
        connection.broker(false);

        expect(heard).toEqual([["status", "disconnected"]]);
    });

    it("always passes on the socket's own change, which the turn depends on", () => {
        const connection = new Connection(wentLive);

        connection.socket(true);
        connection.socket(false);

        expect(heard).toEqual([
            ["socket", true],
            ["socket", false],
        ]);
    });

    it("goes live again after a drop", () => {
        const connection = new Connection(wentLive);
        connection.broker(true);
        connection.socket(true);
        connection.socket(false);
        connection.socket(true);

        expect(wentLive).toHaveBeenCalledTimes(2);
    });
});
