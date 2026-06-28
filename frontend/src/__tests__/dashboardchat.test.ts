/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { DashboardChat, type ChatHost } from "../ui/dashboard/DashboardChat";
import type { AgentInfo, ChatMessage } from "../types/agent";
import type { View } from "../ui/dashboard/types";

function agent(name: string, over: Partial<AgentInfo> = {}): AgentInfo {
    return { id: name, name, state: "running", protected: false, ...over };
}

function makeHost(
    agents: AgentInfo[] = [agent("main-actor"), agent("worker")],
    view: View = "chat",
): ChatHost {
    const map = new Map(agents.map(a => [a.name, a]));
    let current = view;
    return {
        root: document.createElement("div"),
        agents: map,
        getView: () => current,
        setView: vi.fn<(v: View) => void>(v => {
            current = v;
        }),
        sortedAgents: () => [...map.values()],
    };
}

/** Mount the chat view into the host root so query selectors resolve. */
function mount(host: ChatHost): DashboardChat {
    const dc = new DashboardChat(host);
    host.root.appendChild(dc.buildChatView());
    document.body.appendChild(host.root);
    // buildChatView's renders run while detached; re-run now that it's attached
    // (the app does this via afterMount(), which we avoid here so it doesn't
    // pre-load history and skew the history tests).
    dc.renderSidebar();
    dc.renderChatPaneHeader();
    dc.renderChatThread();
    return dc;
}

const thread = (host: ChatHost) => host.root.querySelector<HTMLElement>("#af-chat-thread")!;

// A raw UUID agent id — the backend uses these (not WIDs), and one that never
// resolved to a friendly name keeps the id as its name.
const UUID = "45511e2b-3a2f-4c1d-9e8a-1b2c3d4e5f60";

describe("DashboardChat — syncChatTarget", () => {
    it("prefers main even when an id-named agent sorts first", () => {
        const dc = new DashboardChat(makeHost([agent(UUID), agent("main")]));
        dc.syncChatTarget();
        expect(dc.chatTarget).toBe("main");
    });

    it("never auto-targets a raw id — stays on the default when nothing better exists", () => {
        const dc = new DashboardChat(makeHost([agent(UUID)]));
        dc.syncChatTarget();
        expect(dc.chatTarget).toBe("main-actor"); // the default, NOT the uuid
    });

    it("falls back to the first human-named agent (ignoring id-named ones) when there is no main", () => {
        const dc = new DashboardChat(makeHost([agent(UUID), agent("zebra"), agent("alpha")]));
        dc.syncChatTarget();
        expect(dc.chatTarget).toBe("alpha");
    });

    it("keeps a current valid target instead of re-picking", () => {
        const dc = new DashboardChat(makeHost([agent("main"), agent("worker")]));
        dc.chatTarget = "worker";
        dc.syncChatTarget();
        expect(dc.chatTarget).toBe("worker");
    });
});

describe("DashboardChat — view construction", () => {
    let host: ChatHost;
    beforeEach(() => {
        document.body.innerHTML = "";
        host = makeHost();
        globalThis.fetch = vi.fn(async () => ({ ok: true, json: async () => [] })) as unknown as typeof fetch;
    });
    afterEach(() => vi.restoreAllMocks());

    it("builds the sidebar, pane header and thread", () => {
        mount(host);
        expect(host.root.querySelector("#af-chat-agent-list")).not.toBeNull();
        expect(host.root.querySelector("#af-chat-pane-header")).not.toBeNull();
        expect(host.root.querySelector("#af-chat-thread")).not.toBeNull();
    });

    it("lists agents in the sidebar and filters them by the search box", () => {
        mount(host);
        const list = host.root.querySelector<HTMLElement>("#af-chat-agent-list")!;
        expect(list.querySelectorAll(".af-chat-agent-row").length).toBe(2);
        const search = host.root.querySelector<HTMLInputElement>(".af-chat-sidebar-search input")!;
        search.value = "work";
        search.dispatchEvent(new Event("input"));
        const names = [...list.querySelectorAll<HTMLElement>(".af-chat-agent-row")].map(
            r => r.dataset["name"],
        );
        expect(names).toEqual(["worker"]);
    });

    it("renders the pane header with the current target and its state", () => {
        mount(host);
        const hdr = host.root.querySelector<HTMLElement>("#af-chat-pane-header")!;
        expect(hdr.querySelector(".af-chat-pane-title")!.textContent).toBe("@main-actor");
        expect(hdr.querySelector(".af-chat-agent-dot")).not.toBeNull();
        expect(hdr.querySelector(".af-chat-pane-state")!.textContent).toBe("running");
    });

    it("shows the empty state when the open thread has no messages", () => {
        mount(host);
        expect(thread(host).querySelector(".af-chat-empty")).not.toBeNull();
    });
});

describe("DashboardChat — target selection", () => {
    let host: ChatHost;
    beforeEach(() => {
        document.body.innerHTML = "";
        host = makeHost();
        globalThis.fetch = vi.fn(async () => ({ ok: true, json: async () => [] })) as unknown as typeof fetch;
    });
    afterEach(() => vi.restoreAllMocks());

    it("selecting a sidebar agent switches the open thread", () => {
        const dc = mount(host);
        const workerRow = host.root.querySelector<HTMLElement>('.af-chat-agent-row[data-name="worker"]')!;
        workerRow.click();
        expect(dc.chatTarget).toBe("worker");
        expect(host.root.querySelector(".af-chat-pane-title")!.textContent).toBe("@worker");
    });

    it("does not switch to a non-messageable (system) agent", () => {
        host = makeHost([agent("main-actor"), agent("io-agent")]);
        const dc = mount(host);
        host.root.querySelector<HTMLElement>('.af-chat-agent-row[data-name="io-agent"]')?.click();
        expect(dc.chatTarget).toBe("main-actor"); // unchanged
    });

    it("syncChatTarget falls back to main when the target vanished", () => {
        const dc = mount(host);
        dc.chatTarget = "ghost";
        dc.syncChatTarget();
        expect(dc.chatTarget).toBe("main-actor");
    });

    it("populates the iobar select with messageable agents, main pinned first", () => {
        host = makeHost([agent("zeta"), agent("main-actor"), agent("io-agent")]);
        const dc = mount(host);
        const iobar = dc.buildIobar();
        const opts = [...iobar.querySelectorAll<HTMLOptionElement>("#af-target-select option")].map(
            o => o.value,
        );
        expect(opts[0]).toBe("main-actor"); // PRIORITY pin
        expect(opts).not.toContain("io-agent"); // system agent excluded
    });
});

describe("DashboardChat — sending & live events", () => {
    let host: ChatHost;
    let dc: DashboardChat;
    beforeEach(() => {
        document.body.innerHTML = "";
        host = makeHost();
        globalThis.fetch = vi.fn(async () => ({ ok: true, json: async () => [] })) as unknown as typeof fetch;
        dc = mount(host);
        dc.wire();
    });
    afterEach(() => {
        dc.unwire();
        vi.restoreAllMocks();
    });

    it("sending dispatches af-send-message and shows the message", () => {
        const seen = vi.fn();
        document.addEventListener("af-send-message", seen);
        const iobar = dc.buildIobar();
        host.root.appendChild(iobar);
        const input = iobar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;
        input.value = "hello";
        iobar.querySelector<HTMLButtonElement>(".af-send-btn")!.click();

        expect(seen).toHaveBeenCalled();
        expect(input.value).toBe("");
        expect(thread(host).textContent).toContain("hello");
        document.removeEventListener("af-send-message", seen);
    });

    it("an incoming af-chat-message from the open agent appends to the thread", () => {
        const msg: ChatMessage = {
            id: "m1",
            from: "main-actor",
            to: "user",
            content: "hi back",
            timestampMs: Date.now(),
        };
        document.dispatchEvent(new CustomEvent("af-chat-message", { detail: { msg } }));
        expect(thread(host).textContent).toContain("hi back");
    });

    it("streams chunks into a live bubble then commits on stream-end", () => {
        document.dispatchEvent(
            new CustomEvent("af-stream-chunk", { detail: { chunk: "Hel", from: "main-actor" } }),
        );
        document.dispatchEvent(
            new CustomEvent("af-stream-chunk", { detail: { chunk: "lo", from: "main-actor" } }),
        );
        expect(thread(host).textContent).toContain("Hello");
        document.dispatchEvent(new CustomEvent("af-stream-end"));
        // committed into chatMessages → re-render still shows it
        dc.renderChatThread();
        expect(thread(host).textContent).toContain("Hello");
    });

    it("af-reset-chat clears the thread", () => {
        document.dispatchEvent(
            new CustomEvent("af-chat-message", {
                detail: {
                    msg: { id: "m", from: "main-actor", to: "user", content: "x", timestampMs: 1 },
                },
            }),
        );
        document.dispatchEvent(new CustomEvent("af-reset-chat", { detail: { agent: null } }));
        expect(thread(host).querySelector(".af-chat-empty")).not.toBeNull();
    });

    it("unwire() stops the controller reacting to events", () => {
        dc.unwire();
        document.dispatchEvent(
            new CustomEvent("af-chat-message", {
                detail: {
                    msg: { id: "m2", from: "main-actor", to: "user", content: "ignored", timestampMs: 1 },
                },
            }),
        );
        expect(thread(host).textContent).not.toContain("ignored");
    });
});

describe("DashboardChat — history & misc state", () => {
    let host: ChatHost;
    beforeEach(() => {
        document.body.innerHTML = "";
        host = makeHost();
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "";
    });
    afterEach(() => vi.restoreAllMocks());

    it("loadHistory fetches once per agent and renders the merged messages", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => [{ id: 1, ts: 1_700_000_000, role: "assistant", content: "from history" }],
        })) as unknown as typeof fetch;
        const dc = mount(host);
        await dc.loadHistory("main-actor");
        expect(thread(host).textContent).toContain("from history");
        await dc.loadHistory("main-actor"); // already loaded → no second fetch
        expect(fetch).toHaveBeenCalledTimes(1);
    });

    it("clearAll drops messages and lets history reload", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => [{ id: 1, ts: 1_700_000_000, role: "assistant", content: "h" }],
        })) as unknown as typeof fetch;
        const dc = mount(host);
        await dc.loadHistory("main-actor"); // 1 fetch (chat_log non-empty → no fallback)
        dc.clearAll();
        await dc.loadHistory("main-actor"); // history-loaded set cleared → fetches again
        expect(fetch).toHaveBeenCalledTimes(2);
    });

    it("setTarget changes the active thread target", () => {
        const dc = new DashboardChat(host);
        dc.setTarget("worker");
        expect(dc.chatTarget).toBe("worker");
    });
});

describe("DashboardChat — stop, attachments, external events", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    function att(over: Record<string, unknown> = {}) {
        return { id: "a1", name: "x.png", mime: "image/png", size: 10, url: "blob:abc", ...over };
    }

    it("stop generation fire-and-forgets a POST to /api/chat/stop", () => {
        const dc = mount(makeHost()) as any;
        const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true } as unknown as Response);
        dc._stopGeneration();
        expect(fetchSpy).toHaveBeenCalledWith(expect.stringContaining("/api/chat/stop"), {
            method: "POST",
        });
        fetchSpy.mockRestore();
    });

    it("sends an attachments-only message and clears the pending tray", () => {
        const host = makeHost();
        const dc = mount(host) as any;
        const seen: Array<{ attachments: string[] }> = [];
        const onSend = (e: Event) => seen.push((e as CustomEvent).detail);
        document.addEventListener("af-send-message", onSend);

        dc._pendingAttachments = [att()];
        const input = document.createElement("textarea");
        input.value = ""; // no text — attachment alone must still send
        const select = document.createElement("select");
        const opt = document.createElement("option");
        opt.value = "worker";
        select.append(opt);
        select.value = "worker";
        dc._sendMessage(input, select);

        document.removeEventListener("af-send-message", onSend);
        expect(seen).toEqual([{ content: "", target: "worker", attachments: ["a1"] }]);
        expect(dc._pendingAttachments).toEqual([]);
    });

    it("removing a blob attachment revokes its object URL; a non-blob one does not", () => {
        const dc = mount(makeHost()) as any;
        const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});

        const blob = att({ url: "blob:abc" });
        const remote = att({ id: "a2", url: "https://x/y.png" });
        dc._pendingAttachments = [blob, remote];

        dc._removeAttachment(remote); // non-blob → no revoke
        expect(revoke).not.toHaveBeenCalled();
        dc._removeAttachment(blob); // blob → revoke
        expect(revoke).toHaveBeenCalledWith("blob:abc");
        expect(dc._pendingAttachments).toEqual([]);
        revoke.mockRestore();
    });

    it("an external af-send-message (not self-dispatched) appends to the thread", () => {
        const host = makeHost();
        const dc = mount(host) as any;
        dc.wire();
        document.dispatchEvent(
            new CustomEvent("af-send-message", { detail: { content: "ping", target: "worker" } }),
        );
        expect(dc.chatTarget).toBe("worker");
        expect(thread(host).textContent).toContain("ping");
        dc.unwire();
    });

    it("af-reset-chat scoped to an agent drops that agent + user turns, keeps others", () => {
        const host = makeHost();
        const dc = mount(host) as any;
        dc.wire();
        dc.chatMessages = [
            { id: "1", from: "worker", to: "user", content: "w", timestampMs: 1 },
            { id: "2", from: "main-actor", to: "user", content: "m", timestampMs: 2 },
            { id: "3", from: "user", to: "worker", content: "u", timestampMs: 3 },
        ];
        document.dispatchEvent(new CustomEvent("af-reset-chat", { detail: { agent: "worker" } }));
        expect(dc.chatMessages.map((m: ChatMessage) => m.id)).toEqual(["2"]);
        dc.unwire();
    });

    it("populateSelect pins priority names then orders the rest alphabetically", () => {
        const host = makeHost([agent("zeta"), agent("alpha"), agent("main")]);
        const dc = mount(host) as any;
        const select = document.createElement("select");
        dc._populateSelect(select);
        expect([...select.options].map(o => o.value)).toEqual(["main", "alpha", "zeta"]);
    });

    it("re-rendering mid-stream reattaches the live bubble with its accumulated text", () => {
        const host = makeHost();
        const dc = mount(host) as any;
        dc._streamFrom = "main-actor";
        dc._streamTarget = "main-actor";
        dc._lastSentTarget = "main-actor";
        dc._streamText = "partial reply";
        dc.renderChatThread();
        expect(thread(host).textContent).toContain("partial reply");
    });
});
