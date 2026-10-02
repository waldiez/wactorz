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

    it("does not show a reply twice when history arrives after the reply did", () => {
        // An agent that goes and comes back under its name has its history
        // fetched again, with its replies already in the thread.
        // As the page records a reply: addressed to the thread it was sent in.
        const reply = msg({
            id: "stream-7",
            from: "counter",
            to: "counter",
            content: "counted 1",
            timestampMs: 5000,
        });
        const existing = [
            msg({ id: "hist-counter-1", to: "counter", content: "one", timestampMs: 4900 }),
            reply,
        ];
        const incoming = [
            msg({ id: "hist-counter-1", to: "counter", content: "one", timestampMs: 4900 }),
            msg({
                id: "hist-counter-2",
                from: "counter",
                to: "user",
                content: "counted 1",
                timestampMs: 5020,
            }),
        ];

        expect(mergeChatHistory(existing, incoming)).toEqual([]);
        expect(reply.id).toBe("hist-counter-2");
    });

    it("pairs two replies with the same words each with its own persisted copy", () => {
        const first = msg({ id: "stream-1", from: "main", to: "user", content: "done", timestampMs: 1000 });
        const second = msg({ id: "stream-2", from: "main", to: "user", content: "done", timestampMs: 3000 });
        const incoming = [
            msg({ id: "hist-main-1", from: "main", to: "user", content: "done", timestampMs: 1010 }),
            msg({ id: "hist-main-2", from: "main", to: "user", content: "done", timestampMs: 3010 }),
        ];

        expect(mergeChatHistory([first, second], incoming)).toEqual([]);
        expect([first.id, second.id]).toEqual(["hist-main-1", "hist-main-2"]);
    });

    it("still tells the user's turns apart by whom they were to", () => {
        const toMain = msg({ id: "user-1", to: "main", content: "status", timestampMs: 1000 });
        const toOther = msg({ id: "hist-other-1", to: "other", content: "status", timestampMs: 1010 });

        expect(mergeChatHistory([toMain], [toOther])).toEqual([toOther]);
        expect(toMain.id).toBe("user-1");
    });

    it("keeps a persisted reply from long before the one on screen with the same words", () => {
        const shown = msg({
            id: "stream-9",
            from: "main",
            to: "user",
            content: "done",
            timestampMs: 10_000_000,
        });
        const old = msg({ id: "hist-main-1", from: "main", to: "user", content: "done", timestampMs: 1000 });

        expect(mergeChatHistory([shown], [old])).toEqual([old]);
        expect(shown.id).toBe("stream-9");
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
        const firstOut = out ? out[0] : null;
        expect(firstOut).toMatchObject({
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
        const firstOut = out ? out[0] : null;
        expect(firstOut).toMatchObject({ id: "hist-main-0", from: "main", to: "user", content: "hello" });
    });

    it("strips the [SYSTEM] deletion note and its paired ack from kv_store history", async () => {
        globalThis.fetch = vi.fn(async (url: string) => {
            if (url.includes("/api/chats")) {
                return { ok: true, json: async () => [] };
            }
            return {
                ok: true,
                json: async () => [
                    { role: "user", content: "spawn image-gen-agent" },
                    { role: "assistant", content: "done" },
                    { role: "user", content: "[SYSTEM] Agent 'image-gen-agent' was deleted (deleted)." },
                    { role: "assistant", content: "Acknowledged — 'image-gen-agent' has been removed." },
                    { role: "user", content: "what now?" },
                ],
            };
        }) as unknown as typeof fetch;

        const out = await fetchChatHistory("main");
        expect((out || []).map(m => m.content)).toEqual(["spawn image-gen-agent", "done", "what now?"]);
        expect((out || []).some(m => m.content.startsWith("[SYSTEM]"))).toBe(false);
        expect((out || []).some(m => m.content.startsWith("Acknowledged"))).toBe(false);
    });

    it("drops a lone [SYSTEM] note but keeps a following real user turn", async () => {
        globalThis.fetch = vi.fn(async (url: string) => {
            if (url.includes("/api/chats")) {
                return { ok: true, json: async () => [] };
            }
            return {
                ok: true,
                json: async () => [
                    { role: "user", content: "[SYSTEM] Agent 'x' was deleted (deleted)." },
                    { role: "user", content: "hello" },
                ],
            };
        }) as unknown as typeof fetch;

        const out = await fetchChatHistory("main");
        expect((out || []).map(m => m.content)).toEqual(["hello"]);
    });

    it("keeps a real assistant reply that is not the paired ack", async () => {
        globalThis.fetch = vi.fn(async (url: string) => {
            if (url.includes("/api/chats")) {
                return { ok: true, json: async () => [] };
            }
            return {
                ok: true,
                json: async () => [
                    { role: "user", content: "[SYSTEM] Agent 'x' was deleted (deleted)." },
                    { role: "assistant", content: "Sure, anything else?" },
                ],
            };
        }) as unknown as typeof fetch;

        const out = await fetchChatHistory("main");
        expect((out || []).map(m => m.content)).toEqual(["Sure, anything else?"]);
    });

    it("returns null (a retryable failure) when both sources fail", async () => {
        globalThis.fetch = vi.fn(async () => ({ ok: false })) as unknown as typeof fetch;
        expect(await fetchChatHistory("main")).toBeNull();
    });

    it("returns null (a retryable failure) when fetch throws", async () => {
        globalThis.fetch = vi.fn(async () => {
            throw new Error("network");
        }) as unknown as typeof fetch;
        expect(await fetchChatHistory("main")).toBeNull();
    });
});

describe("attachments restored from history", () => {
    const origFetch = globalThis.fetch;
    afterEach(() => {
        globalThis.fetch = origFetch;
        vi.restoreAllMocks();
    });
    beforeEach(() => {
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "";
    });

    function servingRow(attachments?: unknown): void {
        globalThis.fetch = vi.fn(async (url: string) => {
            if (url.includes("/api/chats")) {
                return {
                    ok: true,
                    json: async () => [
                        {
                            id: 7,
                            ts: 1_700_000_000,
                            role: "user",
                            content: "describe this",
                            ...(attachments === undefined ? {} : { attachments }),
                        },
                    ],
                };
            }
            return { ok: false };
        }) as unknown as typeof fetch;
    }

    it("brings a turn's attachments back with it", async () => {
        // Without this the turn reloads as bare text: the file was stored on
        // the row and the chip vanished on the next restart.
        servingRow([{ id: "a".repeat(32), name: "shot.png", mime: "image/png", size: 100 }]);

        const out = await fetchChatHistory("main");

        expect(out![0]!.attachments).toHaveLength(1);
        expect(out![0]!.attachments![0]!.name).toBe("shot.png");
    });

    it("rebuilds the address the row does not carry", async () => {
        // The server stores the record, not a link. Without deriving the url an
        // image comes back as a plain file chip instead of its thumbnail.
        const id = "b".repeat(32);
        servingRow([{ id, name: "shot.png", mime: "image/png", size: 100 }]);

        const out = await fetchChatHistory("main");

        expect(out![0]!.attachments![0]!.url).toBe(`/api/upload/${id}`);
    });

    it("honours the ingress prefix in that address", async () => {
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "/hassio/ingress/xyz";
        const id = "c".repeat(32);
        servingRow([{ id, name: "shot.png", mime: "image/png", size: 100 }]);

        const out = await fetchChatHistory("main");

        expect(out![0]!.attachments![0]!.url).toBe(`/hassio/ingress/xyz/api/upload/${id}`);
    });

    it("leaves a turn with no attachments alone", async () => {
        servingRow(undefined);

        const out = await fetchChatHistory("main");

        expect(out![0]!.attachments).toBeUndefined();
    });

    it("treats an empty list as no attachments", async () => {
        servingRow([]);

        const out = await fetchChatHistory("main");

        expect(out![0]!.attachments).toBeUndefined();
    });
});
