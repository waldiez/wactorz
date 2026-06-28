/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fetchChatHistory, mergeChatHistory } from "../ui/dashboard/chatHistory";
import type { ChatMessage } from "../types/agent";

function msg(over: Partial<ChatMessage>): ChatMessage {
    return { id: "x", from: "user", to: "main", content: "hi", timestampMs: 1000, ...over };
}

describe("mergeChatHistory", () => {
    it("skips messages whose id already exists", () => {
        const existing = [msg({ id: "hist-main-1" })];
        const out = mergeChatHistory(existing, [msg({ id: "hist-main-1" })]);
        expect(out).toEqual([]);
    });

    it("strips the @agent routing prefix without mutating the incoming message", () => {
        const incoming = msg({ id: "hist-main-2", content: "@main hello" });
        const out = mergeChatHistory([], [incoming]);
        expect(out).toHaveLength(1);
        expect(out[0]!.content).toBe("hello");
        expect(incoming.content).toBe("@main hello"); // caller's object untouched
    });

    it("adopts the persisted id onto a matching optimistic echo instead of duplicating", () => {
        const echo = msg({ id: "user-123", content: "deploy", timestampMs: 5000 });
        const existing = [echo];
        const incoming = [msg({ id: "hist-main-3", content: "deploy", timestampMs: 5050 })];
        const out = mergeChatHistory(existing, incoming);
        expect(out).toEqual([]); // reconciled, not added
        expect(echo.id).toBe("hist-main-3"); // id adopted in place
    });

    it("adds assistant messages and non-matching user messages", () => {
        const out = mergeChatHistory(
            [],
            [
                msg({ id: "hist-main-4", from: "main", to: "user", content: "reply" }),
                msg({ id: "hist-main-5", content: "new question" }),
            ],
        );
        expect(out.map(m => m.id)).toEqual(["hist-main-4", "hist-main-5"]);
    });
});

describe("fetchChatHistory", () => {
    const origFetch = globalThis.fetch;
    afterEach(() => {
        globalThis.fetch = origFetch;
        vi.restoreAllMocks();
    });
    beforeEach(() => {
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "";
    });

    it("maps chat_log rows (newest-last) and scales second-based timestamps", async () => {
        globalThis.fetch = vi.fn(async (url: string) => {
            if (url.includes("/api/chats")) {
                return {
                    ok: true,
                    json: async () => [{ id: 1, ts: 1_700_000_000, role: "user", content: "hi" }],
                };
            }
            return { ok: false };
        }) as unknown as typeof fetch;

        const out = await fetchChatHistory("main");
        expect(out).toHaveLength(1);
        expect(out[0]).toMatchObject({
            id: "hist-main-1",
            from: "user",
            to: "main",
            content: "hi",
            timestampMs: 1_700_000_000_000, // < 1e10 → ×1000
        });
    });

    it("falls back to the kv_store history when chat_log is empty", async () => {
        globalThis.fetch = vi.fn(async (url: string) => {
            if (url.includes("/api/chats")) {
                return { ok: true, json: async () => [] };
            }
            return { ok: true, json: async () => [{ role: "assistant", content: "hello" }] };
        }) as unknown as typeof fetch;

        const out = await fetchChatHistory("main");
        expect(out).toHaveLength(1);
        expect(out[0]).toMatchObject({ id: "hist-main-0", from: "main", to: "user", content: "hello" });
    });

    it("returns [] when both sources fail", async () => {
        globalThis.fetch = vi.fn(async () => ({ ok: false })) as unknown as typeof fetch;
        expect(await fetchChatHistory("main")).toEqual([]);
    });

    it("returns [] when fetch throws", async () => {
        globalThis.fetch = vi.fn(async () => {
            throw new Error("network");
        }) as unknown as typeof fetch;
        expect(await fetchChatHistory("main")).toEqual([]);
    });
});
