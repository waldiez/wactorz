/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fetchChatHistory } from "../ui/dashboard/chatHistory";

describe("fetchChatHistory", () => {
    const origFetch = globalThis.fetch;
    afterEach(() => {
        globalThis.fetch = origFetch;
        vi.restoreAllMocks();
    });
    beforeEach(() => {
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "";
    });

    it("gives a row that is a question the ledger knows its id, options and pick back", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => [
                {
                    id: 9,
                    ts: 1_700_000_000,
                    role: "assistant",
                    content: "Maintenance?\n\n1. Maintenance\n2. Real problem",
                    question: {
                        id: "q-1",
                        agent_id: "agent-1",
                        text: "Maintenance?",
                        options: ["Maintenance", "Real problem"],
                        answer: "Maintenance",
                    },
                },
            ],
        })) as unknown as typeof fetch;

        const out = await fetchChatHistory("triage");

        expect(out).toHaveLength(1);
        expect(out![0]).toMatchObject({
            id: "q-1",
            from: "triage",
            content: "Maintenance?",
            agentId: "agent-1",
            options: ["Maintenance", "Real problem"],
            answer: "Maintenance",
            timestampMs: 1_700_000_000_000,
        });
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

    it("shows a stored reply without its action blocks, and the user's words as typed", async () => {
        globalThis.fetch = vi.fn(async (url: string) => {
            if (url.includes("/api/chats")) {
                return {
                    ok: true,
                    json: async () => [
                        {
                            id: 2,
                            ts: 2,
                            role: "assistant",
                            content: 'Starting it.\n<spawn>{"name": "a"}</spawn>',
                        },
                        { id: 1, ts: 1, role: "user", content: "what is <spawn>x</spawn>?" },
                    ],
                };
            }
            return { ok: false };
        }) as unknown as typeof fetch;

        const out = await fetchChatHistory("main");

        expect(out?.map(m => m.content)).toEqual(["what is <spawn>x</spawn>?", "Starting it.\n"]);
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
