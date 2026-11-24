/**
 * SPDX-License-Identifier: Apache-2.0
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { emit } from "../events";
import { IntroductionReader, readIntroductions } from "../io/introductionsApi";
import { REQUEST_TIMEOUT_MS } from "../io/api";
import { DashboardChat, type ChatHost } from "../ui/chat/DashboardChat";
import type { AgentInfo } from "../types/agent";

const chats: DashboardChat[] = [];

function mount() {
    const agent: AgentInfo = { id: "weather", name: "weather", state: "running", protected: false };
    const agents = new Map([[agent.id, agent]]);
    const host: ChatHost = {
        root: document.createElement("div"),
        agents,
        getView: () => "chat",
        setView: vi.fn(),
        sortedAgents: () => [...agents.values()],
        label: agent => agent.name,
    };
    const chat = new DashboardChat(host);
    chats.push(chat);
    host.root.append(chat.buildChatView());
    document.body.append(host.root);
    chat.wire();
    chat.setTarget(agent.id);
    chat.setIntroductions({ weather: "Already loaded" });
    return { chat, agents, root: host.root };
}

beforeEach(() => {
    document.body.replaceChildren();
    localStorage.clear();
});

afterEach(() => {
    chats.splice(0).forEach(chat => chat.destroy());
    vi.restoreAllMocks();
    vi.useRealTimers();
});

describe("introduction replies", () => {
    it.each([
        { value: null },
        { value: [] },
        { value: ["description"] },
        { value: "description" },
        { value: true },
        { value: 42 },
        { value: { weather: 42 } },
    ])("rejects a malformed successful response $value", async ({ value }) => {
        vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json(value));
        expect(await readIntroductions()).toBeNull();
    });

    it("bounds a stalled request", async () => {
        vi.useFakeTimers();
        vi.spyOn(globalThis, "fetch").mockImplementation(
            (_input, init) =>
                new Promise((_resolve, reject) =>
                    init?.signal?.addEventListener("abort", () => reject(new Error("aborted"))),
                ),
        );
        const reading = readIntroductions();
        await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 1);
        expect(await reading).toBeNull();
    });
});

describe("introduction failures and overlapping changes", () => {
    it("stops retrying when the last agent leaves", async () => {
        vi.useFakeTimers();
        const fetch = vi
            .spyOn(globalThis, "fetch")
            .mockImplementation(async input =>
                String(input).endsWith("/api/introductions")
                    ? new Response(null, { status: 503 })
                    : Response.json([]),
            );
        const { chat, agents } = mount();
        await vi.advanceTimersByTimeAsync(0);
        const removed = new Map(agents);
        agents.clear();
        chat.agentsChanged({ added: new Set(), removed });
        await vi.advanceTimersByTimeAsync(60_000);
        expect(fetch.mock.calls.filter(([url]) => String(url).endsWith("/api/introductions"))).toHaveLength(
            1,
        );
    });

    it("coalesces a burst of notifications into one request", async () => {
        const fetch = vi
            .spyOn(globalThis, "fetch")
            .mockImplementation(async input =>
                String(input).endsWith("/api/introductions")
                    ? Response.json({ weather: "Fresh" })
                    : Response.json([]),
            );
        const { root } = mount();
        for (let i = 0; i < 50; i++) {
            emit("af-introductions-changed");
        }
        await vi.waitFor(() => expect(root.textContent).toContain("Fresh"));
        expect(fetch.mock.calls.filter(([url]) => String(url).endsWith("/api/introductions"))).toHaveLength(
            1,
        );
    });

    it("backs off repeated failures, stops retrying when released, and resumes when shown", async () => {
        vi.useFakeTimers();
        let reads = 0;
        vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
            if (!String(input).endsWith("/api/introductions")) {
                return Response.json([]);
            }
            reads++;
            return new Response(null, { status: 503 });
        });
        const { chat } = mount();
        await vi.advanceTimersByTimeAsync(0);
        expect(reads).toBe(1);
        await vi.advanceTimersByTimeAsync(999);
        expect(reads).toBe(1);
        await vi.advanceTimersByTimeAsync(1);
        expect(reads).toBe(2);
        await vi.advanceTimersByTimeAsync(1_999);
        expect(reads).toBe(2);
        await vi.advanceTimersByTimeAsync(1);
        expect(reads).toBe(3);
        chat.unwire();
        await vi.advanceTimersByTimeAsync(60_000);
        expect(reads).toBe(3);
        chat.wire();
        await vi.advanceTimersByTimeAsync(0);
        expect(reads).toBe(4);
    });

    it("caps retry delays and resets them after a successful read", async () => {
        vi.useFakeTimers();
        const received = vi.fn();
        const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 503 }));
        const reader = new IntroductionReader(received);
        try {
            await reader.refresh();
            for (const delay of [1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000]) {
                const count = fetch.mock.calls.length;
                await vi.advanceTimersByTimeAsync(delay - 1);
                expect(fetch).toHaveBeenCalledTimes(count);
                await vi.advanceTimersByTimeAsync(1);
                expect(fetch).toHaveBeenCalledTimes(count + 1);
            }
            fetch.mockResolvedValueOnce(Response.json({ weather: "Restored" }));
            await vi.advanceTimersByTimeAsync(30_000);
            expect(received).toHaveBeenCalledWith({ weather: "Restored" });
            await reader.refresh();
            const count = fetch.mock.calls.length;
            await vi.advanceTimersByTimeAsync(1_000);
            expect(fetch).toHaveBeenCalledTimes(count + 1);
        } finally {
            reader.stop();
        }
    });

    it("keeps loaded text on malformed JSON and displays remote text safely", async () => {
        const fetch = vi
            .spyOn(globalThis, "fetch")
            .mockImplementation(async input =>
                String(input).endsWith("/api/introductions") ? new Response("{broken") : Response.json([]),
            );
        const { chat, root } = mount();
        await new Promise(resolve => setTimeout(resolve, 0));
        expect(root.textContent).toContain("Already loaded");
        fetch.mockImplementation(async input =>
            String(input).endsWith("/api/introductions")
                ? Response.json({ weather: '<img src=x onerror="window.bad=true">' })
                : Response.json([]),
        );
        emit("af-introductions-changed");
        await vi.waitFor(() => expect(root.textContent).toContain("<img src=x"));
        expect(root.querySelector('img[src="x"], [onerror]')).toBeNull();
        chat.destroy();
    });
    it("keeps loaded text and recovers from a temporary failure without another event", async () => {
        vi.useFakeTimers();
        let reads = 0;
        vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
            if (!String(input).endsWith("/api/introductions")) {
                return Response.json([]);
            }
            reads++;
            return reads === 1
                ? new Response(null, { status: 503 })
                : Response.json({ weather: "Recovered" });
        });
        const { root } = mount();
        emit("af-introductions-changed");
        await vi.advanceTimersByTimeAsync(0);
        expect(root.textContent).toContain("Already loaded");
        await vi.advanceTimersByTimeAsync(1_000);
        expect(root.textContent).toContain("Recovered");
        expect(reads).toBe(2);
    });

    it("ignores an older reply arriving after the latest revision", async () => {
        const pending: ((response: Response) => void)[] = [];
        vi.spyOn(globalThis, "fetch").mockImplementation(input =>
            String(input).endsWith("/api/introductions")
                ? new Promise(resolve => pending.push(resolve))
                : Promise.resolve(Response.json([])),
        );
        const { root } = mount();
        emit("af-introductions-changed");
        await vi.waitFor(() => expect(pending).toHaveLength(1));
        emit("af-introductions-changed");
        await vi.waitFor(() => expect(pending).toHaveLength(2));
        pending[1]!(Response.json({ weather: "Newest" }));
        await vi.waitFor(() => expect(root.textContent).toContain("Newest"));
        pending[0]!(Response.json({ weather: "Obsolete" }));
        await new Promise(resolve => setTimeout(resolve, 0));
        expect(root.textContent).toContain("Newest");
        expect(root.textContent).not.toContain("Obsolete");
    });

    it.each(["reset", "destroy", "remove"])("drops a delayed reply after %s", async action => {
        let answer: (response: Response) => void = () => {};
        vi.spyOn(globalThis, "fetch").mockImplementation(input =>
            String(input).endsWith("/api/introductions")
                ? new Promise(resolve => (answer = resolve))
                : Promise.resolve(Response.json([])),
        );
        const { chat, agents, root } = mount();
        const set = vi.spyOn(chat, "setIntroductions");
        emit("af-introductions-changed");
        await new Promise(resolve => setTimeout(resolve, 0));
        if (action === "reset") {
            chat.clearAll();
        }
        if (action === "destroy") {
            chat.destroy();
        }
        if (action === "remove") {
            agents.clear();
        }
        answer(Response.json({ weather: "Delayed reply" }));
        await new Promise(resolve => setTimeout(resolve, 0));
        expect(root.textContent).not.toContain("Delayed reply");
        if (action !== "remove") {
            expect(set).not.toHaveBeenCalled();
        }
    });
});
