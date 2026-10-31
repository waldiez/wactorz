/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { expect, it, vi } from "vitest";

const start = vi.fn();
vi.mock("../app", () => ({ createApp: () => ({ start, stop: vi.fn() }) }));

it("builds the app and starts it", async () => {
    await import("../main");

    expect(start).toHaveBeenCalledTimes(1);
});
