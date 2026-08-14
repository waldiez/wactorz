/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import { toast } from "../ui/ToastManager";
import { DashboardChat, type ChatHost } from "../ui/dashboard/DashboardChat";
import { withoutAttachment } from "../ui/dashboard/attachTray";
import {
    defaultChatTarget,
    preferredChatTarget,
    resolveSendTarget,
    sendBlockedReason,
    stripLeadingMention,
} from "../ui/dashboard/chatRouting";
import type { AgentInfo, ChatMessage } from "../types/agent";
import type { View } from "../ui/dashboard/types";

function agent(name: string, over: Partial<AgentInfo> = {}): AgentInfo {
    return { id: name, name, state: "running", protected: false, ...over };
}

function makeHost(agents: AgentInfo[] = [agent("main"), agent("worker")], view: View = "chat"): ChatHost {
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

// The picked agent is remembered in localStorage now, so it outlives the
// instance that chose it — and, without this, the test that chose it. Reset it
// like any other global rather than per describe block: six of them here build
// their own host, and only the ones that happened to assert on the target would
// have caught the leak.
beforeEach(() => localStorage.clear());

describe("defaultChatTarget", () => {
    // Only ever consulted for an empty choice, so it answers one question:
    // who should this dashboard open on? Keeping a choice, and moving off a
    // dead one, are separate concerns now (resolveDefaultTarget, dropTargetIfGone).
    it("prefers main, whatever order the agents arrived in", () => {
        expect(defaultChatTarget([agent("catalog"), agent("main")])).toBe("main");
    });

    it("falls back to the alphabetical-first messageable agent when main is absent", () => {
        expect(defaultChatTarget([agent("catalog"), agent("worker")])).toBe("catalog");
    });

    it("returns nothing rather than guessing when there is no one to talk to", () => {
        expect(defaultChatTarget([])).toBe("");
    });
});

describe("preferredChatTarget", () => {
    // The remembered name is a preference, consulted only when nothing has been
    // chosen yet. It has to be checked against the live list: a name that no
    // longer exists would otherwise become a target nothing can deliver to.
    it("reopens on the remembered agent when it is still there", () => {
        expect(preferredChatTarget([agent("main"), agent("worker")], "worker")).toBe("worker");
    });

    it("falls back to the default when the remembered agent is gone", () => {
        expect(preferredChatTarget([agent("main")], "deleted-one")).toBe("main");
    });

    it("keeps a remembered agent that is merely stopped", () => {
        // Stopping is a state the conversation survives — the composer blocks
        // and says so. Moving the user off it on reload would be the bug the
        // target split removed, reintroduced by the back door.
        const agents = [agent("main"), agent("worker", { state: "stopped" })];

        expect(preferredChatTarget(agents, "worker")).toBe("worker");
    });

    it("ignores a remembered agent nobody may message", () => {
        const agents = [agent("main"), agent("monitor-agent")];

        expect(preferredChatTarget(agents, "monitor-agent")).toBe("main");
    });

    it("behaves as the plain default with nothing remembered", () => {
        expect(preferredChatTarget([agent("worker")], null)).toBe("worker");
    });

    it("returns nothing when the list has not arrived yet", () => {
        // An empty list means "not back yet", never "yours is gone" — so this
        // stays empty and resolveDefaultTarget tries again on the next visit,
        // rather than burning the preference against an empty roster.
        expect(preferredChatTarget([], "worker")).toBe("");
    });
});

describe("resolveSendTarget", () => {
    const names = ["main", "catalog", "worker"];

    it("routes to a leading @mention over the picker target", () => {
        expect(resolveSendTarget("@catalog do the thing", names, "main")).toBe("catalog");
    });

    it("matches the mention case-insensitively", () => {
        expect(resolveSendTarget("@Catalog hi", names, "main")).toBe("catalog");
    });

    it("falls back to the picker when there is no mention", () => {
        expect(resolveSendTarget("do the thing", names, "main")).toBe("main");
    });

    it("falls back when the mention names no known agent", () => {
        expect(resolveSendTarget("@ghost hi", names, "main")).toBe("main");
    });
});

describe("sendBlockedReason", () => {
    it("passes a running agent", () => {
        expect(sendBlockedReason([agent("worker")], "worker")).toBeNull();
    });

    it("tells a stopped agent apart from one that is gone", () => {
        // "start it and try again" is actively wrong for an agent that no longer
        // exists — there is nothing to start.
        expect(sendBlockedReason([agent("worker", { state: "stopped" })], "worker")).toContain("start it");
        expect(sendBlockedReason([], "worker")).not.toContain("start it");
        expect(sendBlockedReason([], "worker")).toContain("worker");
    });
});

describe("stripLeadingMention", () => {
    it("drops a leading @target (case-insensitive) and following space", () => {
        expect(stripLeadingMention("@catalog spawn x", "catalog")).toBe("spawn x");
        expect(stripLeadingMention("@Catalog spawn x", "catalog")).toBe("spawn x");
    });

    it("handles a hyphenated agent name", () => {
        expect(stripLeadingMention("@main hi", "main")).toBe("hi");
    });

    it("leaves content untouched when the leading mention isn't the target", () => {
        expect(stripLeadingMention("@worker hi", "catalog")).toBe("@worker hi");
        expect(stripLeadingMention("plain text", "catalog")).toBe("plain text");
    });

    it("does not strip a partial-name match", () => {
        expect(stripLeadingMention("@catalogue hi", "catalog")).toBe("@catalogue hi");
    });
});

describe("DashboardChat — resolveDefaultTarget", () => {
    it("prefers main even when an id-named agent sorts first", () => {
        const dc = new DashboardChat(makeHost([agent(UUID), agent("main")]));
        dc.resolveDefaultTarget();
        expect(dc.chatTarget).toBe("main");
    });

    it("never auto-targets a raw id — leaves the choice unmade instead", () => {
        const dc = new DashboardChat(makeHost([agent(UUID)]));
        dc.resolveDefaultTarget();
        expect(dc.chatTarget).toBe(""); // NOT the uuid
    });

    it("falls back to the first human-named agent (ignoring id-named ones) when there is no main", () => {
        const dc = new DashboardChat(makeHost([agent(UUID), agent("zebra"), agent("alpha")]));
        dc.resolveDefaultTarget();
        expect(dc.chatTarget).toBe("alpha");
    });

    it("keeps a target the user picked", () => {
        const dc = new DashboardChat(makeHost([agent("main"), agent("worker")]));
        dc.setTarget("worker");
        dc.resolveDefaultTarget();
        expect(dc.chatTarget).toBe("worker");
    });

    it("says nothing about a recipient, and fetches nothing, until there is one", () => {
        // An empty target is a real state now, so the two places that read it
        // must cope: the composer must not advertise "@" and history must not
        // request a conversation with nobody.
        const fetchSpy = vi.fn();
        globalThis.fetch = fetchSpy as unknown as typeof fetch;
        const host = makeHost([]);
        const dc = mount(host);
        host.root.appendChild(dc.buildIobar());
        dc.afterMount(); // the path that loads history for the open target

        expect(dc.chatTarget).toBe("");
        expect(host.root.querySelector<HTMLTextAreaElement>("#af-iobar-input")?.placeholder).toBe("Message…");
        expect(fetchSpy).not.toHaveBeenCalled();
    });

    it("keeps a target resolved earlier, even once main turns up", () => {
        // The behaviour change: first resolution sticks. Main arriving late no
        // longer takes the conversation over — the user has already seen the
        // current target named on screen, and moving it silently is the bug.
        const host = makeHost([agent("catalog")]);
        const dc = new DashboardChat(host);
        dc.resolveDefaultTarget();
        expect(dc.chatTarget).toBe("catalog");

        host.agents.set("main", agent("main"));
        dc.resolveDefaultTarget();
        expect(dc.chatTarget).toBe("catalog");
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

    it("the agent filter input has an accessible name and form attributes", () => {
        mount(host);
        const search = host.root.querySelector<HTMLInputElement>(".af-chat-sidebar-search input")!;
        expect(search.getAttribute("aria-label")).toBe("Filter agents");
        expect(search.name).toBe("agent-filter");
        expect(search.id).toBe("af-agent-filter");
        expect(search.type).toBe("text"); // not "search" — avoids browser search chrome
    });

    it("renders the pane header with the current target and its state", () => {
        mount(host);
        const hdr = host.root.querySelector<HTMLElement>("#af-chat-pane-header")!;
        expect(hdr.querySelector(".af-chat-pane-title")!.textContent).toBe("@main");
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
        host = makeHost([agent("main"), agent("io-agent")]);
        const dc = mount(host);
        host.root.querySelector<HTMLElement>('.af-chat-agent-row[data-name="io-agent"]')?.click();
        expect(dc.chatTarget).toBe("main"); // unchanged
    });

    it("resolveDefaultTarget leaves a target that vanished alone", () => {
        // A target that is merely absent may be coming back, and a deletion has
        // its own signal (dropTargetIfGone). Resolution only fills a blank.
        const dc = mount(host);
        dc.chatTarget = "ghost";
        dc.resolveDefaultTarget();
        expect(dc.chatTarget).toBe("ghost");
    });

    it("populates the iobar select with messageable agents, main pinned first", () => {
        host = makeHost([agent("zeta"), agent("main"), agent("io-agent")]);
        const dc = mount(host);
        const iobar = dc.buildIobar();
        const opts = [...iobar.querySelectorAll<HTMLOptionElement>("#af-target-select option")].map(
            o => o.value,
        );
        expect(opts[0]).toBe("main"); // PRIORITY pin
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
            from: "main",
            to: "user",
            content: "hi back",
            timestampMs: Date.now(),
        };
        document.dispatchEvent(new CustomEvent("af-chat-message", { detail: { msg } }));
        expect(thread(host).textContent).toContain("hi back");
    });

    it("streams chunks into a live bubble then commits on stream-end", () => {
        document.dispatchEvent(
            new CustomEvent("af-stream-chunk", { detail: { chunk: "Hel", from: "main" } }),
        );
        document.dispatchEvent(new CustomEvent("af-stream-chunk", { detail: { chunk: "lo", from: "main" } }));
        expect(thread(host).textContent).toContain("Hello");
        document.dispatchEvent(new CustomEvent("af-stream-end"));
        // committed into chatMessages → re-render still shows it
        dc.renderChatThread();
        expect(thread(host).textContent).toContain("Hello");
    });

    it("caps accumulation on a runaway stream instead of growing without bound", () => {
        const chunk = "x".repeat(10_000);
        for (let i = 0; i < 30; i++) {
            document.dispatchEvent(new CustomEvent("af-stream-chunk", { detail: { chunk, from: "main" } }));
        }
        const streams = (dc as unknown as { _streamUI: { streams: { text: (from: string) => string } } })
            ._streamUI.streams;
        expect(streams.text("main").length).toBeLessThan(300_000);
    });

    it("af-reset-chat clears the thread", () => {
        document.dispatchEvent(
            new CustomEvent("af-chat-message", {
                detail: {
                    msg: { id: "m", from: "main", to: "user", content: "x", timestampMs: 1 },
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
                    msg: { id: "m2", from: "main", to: "user", content: "ignored", timestampMs: 1 },
                },
            }),
        );
        expect(thread(host).textContent).not.toContain("ignored");
    });
});

describe("DashboardChat — thread integrity", () => {
    let host: ChatHost;
    let dc: DashboardChat;
    const messages = () => (dc as unknown as { chatMessages: ChatMessage[] }).chatMessages;

    beforeEach(() => {
        document.body.innerHTML = "";
        host = makeHost();
        dc = mount(host);
        dc.wire();
    });

    it("scoped af-reset-chat drops only that agent's thread, keeping user messages to others", () => {
        document.dispatchEvent(
            new CustomEvent("af-send-message", {
                detail: { content: "hi main", target: "main", attachments: [] },
            }),
        );
        document.dispatchEvent(
            new CustomEvent("af-chat-message", {
                detail: {
                    msg: { id: "r1", from: "main", to: "user", content: "reply", timestampMs: 1 },
                },
            }),
        );
        document.dispatchEvent(
            new CustomEvent("af-send-message", {
                detail: { content: "hi worker", target: "worker", attachments: [] },
            }),
        );

        document.dispatchEvent(new CustomEvent("af-reset-chat", { detail: { agent: "main" } }));

        // the old filter dropped every user message regardless of its target
        expect(messages().map(m => `${m.from}->${m.to}:${m.content}`)).toEqual(["user->worker:hi worker"]);
    });

    it("removes the optimistic bubble when the send fails (af-send-failed)", () => {
        document.dispatchEvent(
            new CustomEvent("af-send-message", {
                detail: { content: "hello?", target: "main", attachments: [] },
            }),
        );
        expect(thread(host).textContent).toContain("hello?");

        document.dispatchEvent(
            new CustomEvent("af-send-failed", { detail: { content: "hello?", target: "main" } }),
        );

        expect(thread(host).textContent).not.toContain("hello?");
        expect(messages().some(m => m.content === "hello?")).toBe(false);
    });

    it("keeps concurrent agent streams separate (no merged bubble)", () => {
        const chunk = (c: string, from: string) =>
            document.dispatchEvent(new CustomEvent("af-stream-chunk", { detail: { chunk: c, from } }));
        const end = (from: string, text: string) =>
            document.dispatchEvent(new CustomEvent("af-stream-end", { detail: { text, from } }));

        chunk("A1", "alpha");
        chunk("B1", "beta");
        chunk("A2", "alpha");
        end("alpha", "A1A2");

        // with a single shared buffer this committed "A1B1A2" attributed to alpha
        expect(messages().find(m => m.from === "alpha")?.content).toBe("A1A2");
        expect(messages().some(m => m.content.includes("A1B1"))).toBe(false);

        end("beta", "B1");
        expect(messages().find(m => m.from === "beta")?.content).toBe("B1");
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
        await dc.loadHistory("main");
        expect(thread(host).textContent).toContain("from history");
        await dc.loadHistory("main"); // already loaded → no second fetch
        expect(fetch).toHaveBeenCalledTimes(1);
    });

    it("clearAll drops messages and lets history reload", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => [{ id: 1, ts: 1_700_000_000, role: "assistant", content: "h" }],
        })) as unknown as typeof fetch;
        const dc = mount(host);
        await dc.loadHistory("main"); // 1 fetch (chat_log non-empty → no fallback)
        dc.clearAll();
        await dc.loadHistory("main"); // history-loaded set cleared → fetches again
        expect(fetch).toHaveBeenCalledTimes(2);
    });

    it("loadHistory retries after a failed fetch instead of caching it as loaded", async () => {
        globalThis.fetch = vi.fn(async () => ({ ok: false })) as unknown as typeof fetch;
        const dc = mount(host);
        await dc.loadHistory("main"); // fails — must NOT mark loaded
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => [{ id: 1, ts: 1_700_000_000, role: "assistant", content: "later" }],
        })) as unknown as typeof fetch;
        await dc.loadHistory("main"); // transient failure earlier → real fetch now
        expect(thread(host).textContent).toContain("later");
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

    it("stop generation POSTs to /api/chat/stop", () => {
        const dc = mount(makeHost()) as any;
        const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true } as unknown as Response);
        dc._stopGeneration();
        expect(fetchSpy).toHaveBeenCalledWith(expect.stringContaining("/api/chat/stop"), {
            method: "POST",
        });
        fetchSpy.mockRestore();
    });

    it("says so when stop cannot reach the backend", async () => {
        const dc = mount(makeHost()) as any;
        vi.mocked(toast.show).mockClear();
        const fetchSpy = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("network down"));

        await dc._stopGeneration();

        // Stop against a dead backend must say so rather than appear to work.
        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ type: "alert-error" }));
        fetchSpy.mockRestore();
    });

    it("says so when the backend refuses the stop", async () => {
        const dc = mount(makeHost()) as any;
        vi.mocked(toast.show).mockClear();
        const fetchSpy = vi
            .spyOn(globalThis, "fetch")
            .mockResolvedValue({ ok: false, status: 503 } as unknown as Response);

        await dc._stopGeneration();

        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ type: "alert-error" }));
        fetchSpy.mockRestore();
    });

    it("stays quiet when stop succeeds", async () => {
        const dc = mount(makeHost()) as any;
        vi.mocked(toast.show).mockClear();
        const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true } as unknown as Response);

        await dc._stopGeneration();

        expect(toast.show).not.toHaveBeenCalled();
        fetchSpy.mockRestore();
    });

    it("puts the caret in the composer when a file is attached", () => {
        // Attaching is the first half of composing; the caret belongs where the
        // second half gets typed.
        const host = makeHost();
        const dc = mount(host) as any;
        host.root.appendChild(dc.buildIobar());
        dc.wire();

        document.dispatchEvent(new CustomEvent("af-attachment-added", { detail: { attachment: att() } }));

        expect(document.activeElement).toBe(host.root.querySelector("#af-iobar-input"));
        dc.unwire();
    });

    it("leaves the user on the view they chose", () => {
        // Deliberate: the iobar sits below every view, so a file dropped from
        // the overview is already visible in the tray. Sending moves to the chat
        // view on its own — attaching should not.
        const host = makeHost([agent("main")], "overview");
        const dc = mount(host) as any;
        host.root.appendChild(dc.buildIobar());
        dc.wire();

        document.dispatchEvent(new CustomEvent("af-attachment-added", { detail: { attachment: att() } }));

        expect(host.setView).not.toHaveBeenCalled();
        expect(host.root.querySelectorAll(".af-attach-chip")).toHaveLength(1);
        dc.unwire();
    });

    it("keeps a dropped file through an @mention typed afterwards", () => {
        // The natural order: drop the file, then say who it is for. Nothing
        // clears the tray on a target change, and the mention decides the
        // recipient — so the file must ride along to the agent named last.
        const host = makeHost([agent("main"), agent("worker")]);
        const dc = mount(host) as any;
        host.root.appendChild(dc.buildIobar());
        dc.wire();
        const seen: Array<{ target: string; attachments: unknown[] }> = [];
        const onSend = (e: Event) => seen.push((e as CustomEvent).detail);
        document.addEventListener("af-send-message", onSend);

        document.dispatchEvent(new CustomEvent("af-attachment-added", { detail: { attachment: att() } }));
        const input = host.root.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;
        input.value = "@worker look at this";
        dc._sendMessage(input);

        document.removeEventListener("af-send-message", onSend);
        dc.unwire();
        expect(seen).toHaveLength(1);
        expect(seen[0]!.target).toBe("worker");
        expect(seen[0]!.attachments).toHaveLength(1);
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
        dc.chatTarget = "worker"; // the choice is what routes; the select only shows it
        dc._sendMessage(input, document.createElement("select"));

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

        dc._pendingAttachments = withoutAttachment(dc._pendingAttachments, remote);
        expect(revoke).not.toHaveBeenCalled(); // non-blob → nothing to release
        dc._pendingAttachments = withoutAttachment(dc._pendingAttachments, blob);
        expect(revoke).toHaveBeenCalledWith("blob:abc");
        expect(dc._pendingAttachments).toEqual([]);
        revoke.mockRestore();
    });

    it("sending revokes pending blob attachment URLs (dev-stub uploads)", () => {
        const dc = mount(makeHost()) as any;
        const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
        dc._pendingAttachments = [att({ url: "blob:send-me" })];
        const input = document.createElement("textarea");
        input.value = "hi";
        const select = document.createElement("select");
        const opt = document.createElement("option");
        opt.value = "worker";
        select.append(opt);
        select.value = "worker";
        dc._sendMessage(input, select);
        expect(revoke).toHaveBeenCalledWith("blob:send-me");
        expect(dc._pendingAttachments).toEqual([]);
        revoke.mockRestore();
    });

    it("clearAll revokes pending blob attachment URLs", () => {
        const dc = mount(makeHost()) as any;
        const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
        dc._pendingAttachments = [att({ url: "blob:wipe-me" })];
        dc.clearAll();
        expect(revoke).toHaveBeenCalledWith("blob:wipe-me");
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
            { id: "2", from: "main", to: "user", content: "m", timestampMs: 2 },
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
        dc._streamUI.streams.append("main", "partial reply");
        dc._streamUI["_targets"].set("main", "main");
        dc._lastSentTarget = "main";
        dc.renderChatThread();
        expect(thread(host).textContent).toContain("partial reply");
    });
});

describe("DashboardChat — @mention target stickiness", () => {
    afterEach(() => vi.restoreAllMocks());

    it("stays on an @mentioned agent after a later reconcile (no snap back to main)", () => {
        globalThis.fetch = vi.fn(async () => ({ ok: true, json: async () => [] })) as unknown as typeof fetch;
        const host = makeHost([agent("main"), agent("catalog")], "chat");
        const dc = mount(host);
        dc.wire();
        const iobar = dc.buildIobar();
        host.root.appendChild(iobar);
        const input = iobar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;

        input.value = "@catalog spawn weather agent";
        iobar.querySelector<HTMLButtonElement>(".af-send-btn")!.click();
        // the @mention switched the open thread to catalog
        expect(dc.chatTarget).toBe("catalog");

        // arriving at the view resolves a default — it must NOT revert to main
        // now that the @mention has set the target.
        dc.resolveDefaultTarget();
        expect(dc.chatTarget).toBe("catalog");

        dc.unwire();
    });
});

describe("mobile master–detail: which pane is showing", () => {
    /**
     * The pane is revealed by an `agent-selected` class on `.af-chat`. It used to
     * be added only inside `_selectAgent`, so arriving at the chat view with a
     * target already chosen — which is what tapping "Chat" does, since the
     * default resolves to `main` — left the sidebar showing and the rest of the
     * screen empty.
     */
    function mount(agents: AgentInfo[] = [agent("main"), agent("worker")]) {
        const host = makeHost(agents);
        const chat = new DashboardChat(host);
        host.root.appendChild(chat.buildChatView());
        chat.afterMount();
        return { host, chat, el: () => host.root.querySelector(".af-chat") };
    }

    it("opens the conversation when the view is entered with a target", () => {
        const { el } = mount();
        expect(el()?.classList.contains("agent-selected")).toBe(true);
    });

    it("still opens it when an agent is picked from the list", () => {
        const { host, el } = mount();
        host.root.querySelector<HTMLElement>("#af-chat-agent-list button")?.click();
        expect(el()?.classList.contains("agent-selected")).toBe(true);
    });

    it("shows the list when Back is pressed", () => {
        const { host, el } = mount();
        host.root.querySelector<HTMLElement>(".af-chat-back-btn")?.click();
        expect(el()?.classList.contains("agent-selected")).toBe(false);
    });

    it("opens the conversation when a message is sent from another view", () => {
        // Typing "@catalog spawn weather agent" on Overview navigates to chat.
        // Landing on the agent list there strands the user: the reply is already
        // on its way to an agent they cannot see.
        const { host, chat, el } = mount([agent("main"), agent("catalog")]);
        host.root.querySelector<HTMLElement>(".af-chat-back-btn")?.click();
        expect(el()?.classList.contains("agent-selected")).toBe(false);

        chat.wire();
        document.dispatchEvent(
            new CustomEvent("af-send-message", {
                detail: { content: "@catalog spawn weather agent", target: "catalog", attachments: [] },
            }),
        );
        chat.afterMount();

        expect(el()?.classList.contains("agent-selected")).toBe(true);
    });

    it("opens the conversation again when the view is re-entered", () => {
        // Back is a choice within one visit. Leaving and returning via the nav
        // means "Chat", which always means a conversation — otherwise one Back
        // silently changes what that tab does until the next select or send.
        const { host, chat, el } = mount();
        host.root.querySelector<HTMLElement>(".af-chat-back-btn")?.click();
        expect(el()?.classList.contains("agent-selected")).toBe(false);

        chat.showConversation(); // what CardDashboard._setView does on arrival
        chat.afterMount();

        expect(el()?.classList.contains("agent-selected")).toBe(true);
    });

    it("names the new recipient in the composer after an @mention send", () => {
        // The placeholder is the only on-screen statement of where a message
        // goes. Left stale it read "Message @main…" while replies went to
        // catalog — worse than unhelpful, because it is wrong.
        // Start on another view: that is the path where _showSentMessage
        // switches view and returns early, skipping updateTargetSelect. With the
        // host already on "chat" the placeholder gets set by that branch instead,
        // and the test passes whether or not the fix is present.
        const host = makeHost([agent("main"), agent("catalog")], "overview");
        const chat = new DashboardChat(host);
        host.root.appendChild(chat.buildChatView());
        chat.afterMount();
        // The composer lives in the shell, outside the chat view, so the
        // harness has to supply it the way the dashboard does.
        const input = document.createElement("textarea");
        input.id = "af-iobar-input";
        host.root.appendChild(input);
        chat.wire();

        document.dispatchEvent(
            new CustomEvent("af-send-message", {
                detail: { content: "@catalog spawn weather agent", target: "catalog", attachments: [] },
            }),
        );

        expect(input.placeholder).toBe("Message @catalog…");
    });

    it("opens the conversation when sending while the list is showing", () => {
        // Already in the chat view, list showing: nothing mounts, so clearing
        // the flag is not enough on its own — the send path has to apply it.
        const { host, chat, el } = mount([agent("main"), agent("catalog")]);
        host.root.querySelector<HTMLElement>(".af-chat-back-btn")?.click();
        expect(el()?.classList.contains("agent-selected")).toBe(false);

        chat.wire();
        document.dispatchEvent(
            new CustomEvent("af-send-message", {
                detail: { content: "@catalog spawn weather-agent", target: "catalog", attachments: [] },
            }),
        );

        expect(el()?.classList.contains("agent-selected")).toBe(true);
    });

    it("keeps the list showing across a re-render", () => {
        // Deriving purely from "has a target" would re-open the pane here,
        // making Back appear to do nothing the moment anything re-rendered.
        const { host, chat, el } = mount();
        host.root.querySelector<HTMLElement>(".af-chat-back-btn")?.click();
        chat.afterMount();
        expect(el()?.classList.contains("agent-selected")).toBe(false);
    });
});
