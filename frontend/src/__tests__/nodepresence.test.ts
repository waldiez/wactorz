/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";

import { NodePresence } from "../agents/nodePresence";

describe("NodePresence", () => {
    it("takes a node's first report as coming online, and the next ones as carrying on", () => {
        const presence = new NodePresence(1000);

        expect(presence.heard("rpi", 0)).toBe(true);
        expect(presence.heard("rpi", 500)).toBe(false);
        expect(presence.heard("rpi", 1400)).toBe(false);
    });

    it("takes a report after the window of silence as coming online again", () => {
        const presence = new NodePresence(1000);
        presence.heard("rpi", 0);

        expect(presence.heard("rpi", 1000)).toBe(true);
        expect(presence.heard("rpi", 1500)).toBe(false);
    });

    it("keeps each node's time apart", () => {
        const presence = new NodePresence(1000);
        presence.heard("rpi", 0);

        expect(presence.heard("sandbox", 10)).toBe(true);
        expect(presence.heard("rpi", 20)).toBe(false);
    });
});
