/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn(), close: vi.fn() } }));

import { toast } from "../ui/ToastManager";
import { DashboardChat, type ChatHost } from "../ui/dashboard/DashboardChat";
import { TURN_IDLE_TIMEOUT_MS } from "../chat/turnTracker";
import { allByRole, byRole, byTestId } from "./queries";
import { rememberedConversation } from "../ui/dashboard/viewRoutes";
import { listen } from "../events";
import {
    defaultChatTarget,
    leadingMention,
    preferredChatTarget,
    sendBlockedReason,
    stripLeadingMention,
} from "../ui/dashboard/chatRouting";
import type { AgentInfo, ChatMessage } from "../types/agent";
import type { View } from "../ui/dashboard/types";

// Every chat a test wires is unwired once the test ends, whether or not the
// test does it: one left listening hears every later test's events, holds
// their messages and redraws for them, so the file slows as it goes.
const wiredChats: DashboardChat[] = [];
const realWire = DashboardChat.prototype.wire;
DashboardChat.prototype.wire = function (this: DashboardChat): void {
    wiredChats.push(this);
    realWire.call(this);
};
afterEach(() => {
    wiredChats.splice(0).forEach(chat => chat.unwire());
});
function agent(name: string, over: Partial<AgentInfo> = {}): AgentInfo {
    return { id: name, name, state: "running", protected: false, ...over };
}

/** A host whose agents a test changes, as the store would. */
interface FakeChatHost extends ChatHost {
    readonly agents: Map<string, AgentInfo>;
}

function makeHost(agents: AgentInfo[] = [agent("main"), agent("worker")], view: View = "chat"): FakeChatHost {
    const map = new Map(agents.map(a => [a.id, a]));
    let current = view;
    return {
        root: document.createElement("div"),
        agents: map,
        getView: () => current,
        setView: vi.fn<(v: View) => void>(v => {
            current = v;
        }),
        sortedAgents: () => [...map.values()],
        label: a => a.name,
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

/** Press Stop, as the person does. */
function pressStop(host: ChatHost, chat: DashboardChat): void {
    if (!host.root.querySelector(".af-stop-btn")) {
        host.root.appendChild(chat.buildIobar());
    }
    host.root.querySelector<HTMLButtonElement>(".af-stop-btn")!.click();
}

/** Let what is waiting in the task queue run: a request's answer, and what follows it. */
const drained = () => new Promise<void>(resolve => setTimeout(resolve, 0));

/** Send `words` as the person does: typed into the composer and sent with its button. */
function sendFromComposer(host: ChatHost, chat: DashboardChat, words: string): void {
    if (!host.root.querySelector("#af-iobar-input")) {
        host.root.appendChild(chat.buildIobar());
    }
    host.root.querySelector<HTMLTextAreaElement>("#af-iobar-input")!.value = words;
    host.root.querySelector<HTMLButtonElement>(".af-send-btn")!.click();
}

/**
 * Send `words`, then answer as the server does once it has routed them: the message
 * reached the agent `name` (whose id, in these fakes, is its name). A mention names
 * an agent by name, so only the server can say which agent that is.
 */
function sendReaching(host: ChatHost, chat: DashboardChat, words: string, name: string): void {
    let sent = "";
    const record = (e: Event) => {
        sent = (e as CustomEvent<{ id: string }>).detail.id;
    };
    document.addEventListener("af-send-message", record);
    sendFromComposer(host, chat, words);
    document.removeEventListener("af-send-message", record);
    document.dispatchEvent(new CustomEvent("af-turn-reached", { detail: { id: sent, agentId: name, name } }));
}

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

describe("leadingMention", () => {
    const agents = [agent("main"), agent("catalog"), agent("worker")];

    it("names the agent a leading @mention is to", () => {
        expect(leadingMention("@catalog do the thing", agents)).toEqual({
            spelled: "catalog",
            found: [agents[1]],
        });
    });

    it("matches the mention case-insensitively, spelled as the agent spells it", () => {
        expect(leadingMention("@Catalog hi", agents)?.spelled).toBe("catalog");
    });

    it("is null with no mention", () => {
        expect(leadingMention("do the thing", agents)).toBeNull();
    });

    it("finds nothing when the mention names no known agent", () => {
        expect(leadingMention("@ghost hi", agents)?.found).toEqual([]);
    });
});

describe("sendBlockedReason", () => {
    it("passes a running agent", () => {
        expect(sendBlockedReason(agent("worker"), "worker")).toBeNull();
    });

    it("tells a stopped agent apart from one that is gone", () => {
        // "start it and try again" is actively wrong for an agent that no longer
        // exists — there is nothing to start.
        expect(sendBlockedReason(agent("worker", { state: "stopped" }), "worker")).toContain("start it");
        expect(sendBlockedReason(undefined, "worker")).not.toContain("start it");
        expect(sendBlockedReason(undefined, "worker")).toContain("worker");
    });

    it("blocks a message to an agent that cannot be messaged, opened for its question", () => {
        const reason = sendBlockedReason(agent("installer", { protected: true }), "installer");
        expect(reason).toContain("can't be messaged");
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

    it("hands the target to main when it turns up before anything has been shown", () => {
        // Agents register one at a time, so the first resolution runs against a
        // partial list and can land on whatever sorts first. A fallback is never
        // remembered, so nothing here is the user's choice yet.
        const host = makeHost([agent("catalog")]);
        const dc = new DashboardChat(host);
        dc.resolveDefaultTarget();
        expect(dc.chatTarget).toBe("catalog");

        host.agents.set("main", agent("main"));
        dc.resolveDefaultTarget();
        expect(dc.chatTarget).toBe("main");
    });

    it("shows the conversation it settles on when the chat opened before any agent arrived", () => {
        // A fresh page lands on the chat it was left on, before the first
        // agent has registered: the view is drawn for nobody, and the target
        // the arriving agents give it must reach the header and thread too.
        localStorage.setItem("wactorz-chat-agent-id", "catalog");
        const fetchSpy = vi.fn(async () => ({ ok: true, json: async () => [] }));
        globalThis.fetch = fetchSpy as unknown as typeof fetch;
        const host = makeHost([]);
        const dc = mount(host);
        dc.afterMount();
        const header = host.root.querySelector<HTMLElement>("#af-chat-pane-header")!;

        host.agents.set("main", agent("main"));
        host.agents.set("catalog", agent("catalog"));
        dc.resolveDefaultTarget();

        expect(dc.chatTarget).toBe("catalog");
        expect(header.textContent).toContain("catalog");
        expect(thread(host).textContent).not.toContain("with @ yet");
        expect(fetchSpy).toHaveBeenCalled();
    });

    it("keeps a target resolved earlier once the chat has been shown", () => {
        // Mounting is what marks the target as seen, and from there main
        // arriving late must not take the conversation over.
        const host = makeHost([agent("catalog")]);
        const dc = mount(host);
        dc.afterMount();
        expect(dc.chatTarget).toBe("catalog");

        host.agents.set("main", agent("main"));
        dc.resolveDefaultTarget();
        expect(dc.chatTarget).toBe("catalog");
    });
});

describe("DashboardChat — view construction", () => {
    let host: FakeChatHost;
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
        const names = [...list.querySelectorAll<HTMLElement>(".af-chat-agent-row")].map(r => r.dataset["id"]);
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
    let host: FakeChatHost;
    beforeEach(() => {
        document.body.innerHTML = "";
        host = makeHost();
        globalThis.fetch = vi.fn(async () => ({ ok: true, json: async () => [] })) as unknown as typeof fetch;
    });
    afterEach(() => vi.restoreAllMocks());

    it("selecting a sidebar agent switches the open thread", () => {
        const dc = mount(host);
        const workerRow = host.root.querySelector<HTMLElement>('.af-chat-agent-row[data-id="worker"]')!;
        workerRow.click();
        expect(dc.chatTarget).toBe("worker");
        expect(host.root.querySelector(".af-chat-pane-title")!.textContent).toBe("@worker");
    });

    it("opens an agent that cannot be messaged while it is asking something", () => {
        host = makeHost([agent("main"), agent("installer", { protected: true })]);
        const dc = mount(host);
        dc.wire();
        const row = () =>
            host.root.querySelector<HTMLButtonElement>('.af-chat-agent-row[data-id="installer"]')!;
        expect(row().disabled).toBe(true);

        const msg: ChatMessage = {
            id: "q1",
            from: "installer",
            to: "user",
            threadId: "installer",
            content: "Install?",
            timestampMs: Date.now(),
            agentId: "installer-id",
            options: ["Install", "Refuse"],
        };
        document.dispatchEvent(new CustomEvent("af-chat-message", { detail: { msg } }));
        expect(row().disabled).toBe(false);

        row().click();
        expect(dc.chatTarget).toBe("installer");
        expect(thread(host).querySelectorAll("button.af-chat-option")).toHaveLength(2);

        // Answered, its row locks again, as any agent's that cannot be messaged.
        document.dispatchEvent(new CustomEvent("af-answered", { detail: { id: "q1", option: "Refuse" } }));
        expect(row().disabled).toBe(true);
        dc.unwire();
    });

    it("takes a question asked while the page was away, once, and opens its thread", () => {
        host = makeHost([agent("main"), agent("installer", { protected: true })]);
        const dc = mount(host);
        dc.wire();
        const row = () =>
            host.root.querySelector<HTMLButtonElement>('.af-chat-agent-row[data-id="installer"]')!;
        const asked: ChatMessage = {
            id: "q7",
            from: "installer",
            to: "user",
            threadId: "installer",
            content: "Install?",
            timestampMs: Date.now() - 60_000,
            agentId: "installer-id",
            options: ["Install", "Refuse"],
        };

        document.dispatchEvent(
            new CustomEvent("af-questions-listed", { detail: { open: [asked], answered: [] } }),
        );
        expect(row().disabled).toBe(false);
        row().click();
        expect(thread(host).querySelectorAll("button.af-chat-option")).toHaveLength(2);

        // Listed again on the next reconnect: still one question in the thread.
        document.dispatchEvent(
            new CustomEvent("af-questions-listed", { detail: { open: [asked], answered: [] } }),
        );
        expect(thread(host).querySelectorAll("button.af-chat-option")).toHaveLength(2);
        dc.unwire();
    });

    it("draws a question asked while away straight into the thread it belongs to", () => {
        host = makeHost([agent("main"), agent("installer", { protected: true })]);
        const dc = mount(host);
        dc.wire();
        dc.setTarget("installer");
        expect(thread(host).querySelectorAll("button.af-chat-option")).toHaveLength(0);

        const asked: ChatMessage = {
            id: "q8",
            from: "installer",
            to: "user",
            threadId: "installer",
            content: "Install?",
            timestampMs: Date.now(),
            agentId: "installer-id",
            options: ["Install", "Refuse"],
        };
        document.dispatchEvent(
            new CustomEvent("af-questions-listed", { detail: { open: [asked], answered: [] } }),
        );

        expect(thread(host).querySelectorAll("button.af-chat-option")).toHaveLength(2);
        document.dispatchEvent(
            new CustomEvent("af-questions-listed", { detail: { open: [], answered: [] } }),
        );
        dc.unwire();
    });

    it("does not end a turn on a question re-listed from the ledger", () => {
        host = makeHost([agent("main"), agent("installer", { protected: true })]);
        const dc = mount(host);
        dc.wire();
        const asked: ChatMessage = {
            id: "q9",
            from: "main",
            to: "user",
            threadId: "main",
            content: "Approve the plan?",
            timestampMs: Date.now(),
            agentId: "main-id",
            options: ["Approve", "Reject"],
        };
        const ended = vi.fn();
        document.addEventListener("af-chat-message", ended);

        document.dispatchEvent(
            new CustomEvent("af-questions-listed", { detail: { open: [asked], answered: [] } }),
        );

        expect(ended).not.toHaveBeenCalled();
        document.removeEventListener("af-chat-message", ended);
        dc.unwire();
    });

    it("lets every listener go on unwire", () => {
        host = makeHost([agent("main"), agent("installer", { protected: true })]);
        const dc = mount(host);
        dc.wire();
        dc.unwire();
        const asked: ChatMessage = {
            id: "q10",
            from: "installer",
            to: "user",
            threadId: "installer",
            content: "Install?",
            timestampMs: Date.now(),
            agentId: "installer-id",
            options: ["Install", "Refuse"],
        };

        document.dispatchEvent(
            new CustomEvent("af-questions-listed", { detail: { open: [asked], answered: [] } }),
        );

        const row = host.root.querySelector<HTMLButtonElement>('.af-chat-agent-row[data-id="installer"]')!;
        expect(row.disabled).toBe(true);
    });

    it("does not switch to a non-messageable (system) agent", () => {
        host = makeHost([agent("main"), agent("monitor-agent")]);
        const dc = mount(host);
        host.root.querySelector<HTMLElement>('.af-chat-agent-row[data-id="monitor-agent"]')?.click();
        expect(dc.chatTarget).toBe("main"); // unchanged
    });

    it("resolveDefaultTarget leaves a target that vanished alone", () => {
        // A target that is merely absent may be coming back, and a deletion has
        // its own signal (dropTargetIfGone). Resolution only fills a blank.
        // Filled for them, not chosen: the agent it named then leaves the list.
        host = makeHost([agent("ghost")]);
        const dc = mount(host);
        expect(dc.chatTarget).toBe("ghost");
        host.agents.delete("ghost");

        dc.resolveDefaultTarget();

        expect(dc.chatTarget).toBe("ghost");
    });

    it("populates the iobar select with messageable agents, main pinned first", () => {
        host = makeHost([agent("zeta"), agent("main"), agent("monitor-agent")]);
        const dc = mount(host);
        const iobar = dc.buildIobar();
        const opts = [...iobar.querySelectorAll<HTMLOptionElement>("#af-target-select option")].map(
            o => o.value,
        );
        expect(opts[0]).toBe("main"); // PRIORITY pin
        expect(opts).not.toContain("monitor-agent"); // system agent excluded
    });
});

describe("DashboardChat — sending & live events", () => {
    let host: FakeChatHost;
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
            threadId: "main",
            content: "hi back",
            timestampMs: Date.now(),
        };
        document.dispatchEvent(new CustomEvent("af-chat-message", { detail: { msg } }));
        expect(thread(host).textContent).toContain("hi back");
    });

    it("closes a question's buttons when it is answered, here or elsewhere", () => {
        const msg: ChatMessage = {
            id: "q1",
            from: "main",
            to: "user",
            threadId: "main",
            content: "Changed?",
            timestampMs: Date.now(),
            options: ["yes", "no"],
        };
        document.dispatchEvent(new CustomEvent("af-chat-message", { detail: { msg } }));
        const buttons = (): HTMLButtonElement[] => [
            ...thread(host).querySelectorAll<HTMLButtonElement>("button.af-chat-option"),
        ];
        expect(buttons().map(b => b.disabled)).toEqual([false, false]);

        // The pick from another channel reaches the dashboard as an answer.
        document.dispatchEvent(new CustomEvent("af-answered", { detail: { id: "q1", option: "no" } }));
        expect(buttons().map(b => b.disabled)).toEqual([true, true]);
        expect(thread(host).querySelector(".af-chat-option-picked")!.textContent).toBe("no");

        // An answer to a question not shown here changes nothing.
        document.dispatchEvent(new CustomEvent("af-answered", { detail: { id: "other", option: "x" } }));
        expect(buttons()).toHaveLength(2);
    });

    it("keeps a voice session in its embodied surface thread", () => {
        dc.unwire();
        host = makeHost([agent("main"), agent("reachy-mini")]);
        dc = mount(host);
        dc.setTarget("main");
        dc.renderSidebar();
        dc.renderChatPaneHeader();
        dc.renderChatThread();
        dc.wire();

        const msg: ChatMessage = {
            id: "voice-1",
            from: "user",
            to: "reachy-mini",
            threadId: "reachy-mini",
            content: "turn on the light",
            timestampMs: Date.now(),
            source: "voice",
            surface: "reachy-mini",
            surfaceLabel: "Reachy",
            brain: "main",
        };
        document.dispatchEvent(new CustomEvent("af-chat-message", { detail: { msg } }));

        expect(dc.chatTarget).toBe("reachy-mini");
        expect(thread(host).textContent).toContain("turn on the light");
    });

    it("takes a voice session's thread as the person's choice: remembered, in the address, named in the composer", () => {
        dc.unwire();
        host = makeHost([agent("main"), agent("reachy-mini")]);
        dc = mount(host);
        host.root.appendChild(dc.buildIobar());
        dc.setTarget("main");
        dc.wire();
        const addressed: string[][] = [];
        const hear = (e: Event) => addressed.push((e as CustomEvent<{ path: string[] }>).detail.path);
        document.addEventListener("af-view-changed", hear);

        const msg: ChatMessage = {
            id: "voice-2",
            from: "user",
            to: "reachy-mini",
            threadId: "reachy-mini",
            content: "lights off",
            timestampMs: Date.now(),
            source: "voice",
            surface: "reachy-mini",
        };
        document.dispatchEvent(new CustomEvent("af-chat-message", { detail: { msg } }));

        document.removeEventListener("af-view-changed", hear);
        expect(rememberedConversation()).toBe("reachy-mini");
        expect(addressed).toContainEqual(["reachy-mini"]);
        expect(host.root.querySelector<HTMLTextAreaElement>("#af-iobar-input")?.placeholder).toBe(
            "Message @reachy-mini…",
        );
    });

    it("says whom the person addresses, typed or spoken, for the socket to attribute replies to", () => {
        dc.unwire();
        host = makeHost([agent("main"), agent("reachy-mini")]);
        dc = mount(host);
        dc.setTarget("main");
        dc.wire();
        const heard: string[] = [];
        const hear = listen("af-addressed", d => heard.push(d.agent));

        sendFromComposer(host, dc, "hello");
        const msg: ChatMessage = {
            id: "voice-3",
            from: "user",
            to: "reachy-mini",
            threadId: "reachy-mini",
            content: "lights on",
            timestampMs: Date.now(),
            source: "voice",
            surface: "reachy-mini",
        };
        document.dispatchEvent(new CustomEvent("af-chat-message", { detail: { msg } }));

        document.removeEventListener("af-addressed", hear);
        expect(heard).toEqual(["main", "reachy-mini"]);
    });

    it("streams chunks into a live bubble then commits on stream-end", () => {
        document.dispatchEvent(
            new CustomEvent("af-stream-chunk", { detail: { chunk: "Hel", from: "main", text: "Hel" } }),
        );
        document.dispatchEvent(
            new CustomEvent("af-stream-chunk", { detail: { chunk: "lo", from: "main", text: "Hello" } }),
        );
        expect(thread(host).textContent).toContain("Hello");
        document.dispatchEvent(new CustomEvent("af-stream-end", { detail: { text: "Hello", from: "main" } }));
        // committed into chatMessages → re-render still shows it
        dc.renderChatThread();
        expect(thread(host).textContent).toContain("Hello");
    });

    it("caps accumulation on a runaway stream instead of growing without bound", () => {
        const chunk = "x".repeat(10_000);
        for (let i = 0; i < 30; i++) {
            const text = chunk.repeat(i + 1);
            document.dispatchEvent(
                new CustomEvent("af-stream-chunk", { detail: { chunk, from: "main", text } }),
            );
        }
        const live = thread(host).querySelector('[data-testid="message-body"]');
        expect(live?.textContent?.length ?? 0).toBeGreaterThan(0);
        expect(live?.textContent?.length ?? 0).toBeLessThan(300_000);
    });

    it("af-reset-chat clears the thread", () => {
        document.dispatchEvent(
            new CustomEvent("af-chat-message", {
                detail: {
                    msg: {
                        id: "m",
                        from: "main",
                        to: "user",
                        threadId: "main",
                        content: "x",
                        timestampMs: 1,
                    },
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
                    msg: {
                        id: "m2",
                        from: "main",
                        to: "user",
                        threadId: "main",
                        content: "ignored",
                        timestampMs: 1,
                    },
                },
            }),
        );
        expect(thread(host).textContent).not.toContain("ignored");
    });
});

describe("DashboardChat — thread integrity", () => {
    let host: FakeChatHost;
    let dc: DashboardChat;
    /** What `name`'s thread shows, message by message, once it is the one open. */
    const said = (name: string): string[] => {
        dc.setTarget(name);
        return [...thread(host).querySelectorAll('[data-testid="message-body"]')].map(
            n => n.textContent ?? "",
        );
    };

    beforeEach(() => {
        document.body.innerHTML = "";
        host = makeHost();
        dc = mount(host);
        dc.wire();
    });

    it("scoped af-reset-chat drops only that agent's thread, keeping user messages to others", () => {
        sendFromComposer(host, dc, "hi main");
        document.dispatchEvent(
            new CustomEvent("af-chat-message", {
                detail: {
                    msg: {
                        id: "r1",
                        from: "main",
                        to: "user",
                        threadId: "main",
                        content: "reply",
                        timestampMs: 1,
                    },
                },
            }),
        );
        sendReaching(host, dc, "@worker hi worker", "worker");

        document.dispatchEvent(new CustomEvent("af-reset-chat", { detail: { agent: "main" } }));

        expect(said("main")).toEqual([]);
        expect(said("worker")).toEqual(["hi worker"]);
    });

    it("keeps the bubble of a send that did not go out, waiting to go, found by its id", () => {
        const sends: { id: string; content: string; target: string }[] = [];
        const record = (e: Event) =>
            sends.push((e as CustomEvent<{ id: string; content: string; target: string }>).detail);
        document.addEventListener("af-send-message", record);
        sendFromComposer(host, dc, "hello?");
        document.removeEventListener("af-send-message", record);
        expect(sends).toHaveLength(1);
        const sent = sends[0]!;

        document.dispatchEvent(new CustomEvent("af-send-failed", { detail: sent }));

        expect(said("main")).toEqual(["hello?"]);
        expect(thread(host).textContent).toContain("Waiting to send");
    });

    it("keeps concurrent agent streams separate (no merged bubble)", () => {
        const soFar = new Map<string, string>();
        const chunk = (c: string, from: string) => {
            const text = (soFar.get(from) ?? "") + c;
            soFar.set(from, text);
            document.dispatchEvent(
                new CustomEvent("af-stream-chunk", { detail: { chunk: c, from, agentId: from, text } }),
            );
        };
        const end = (from: string, text: string) =>
            document.dispatchEvent(
                new CustomEvent("af-stream-end", { detail: { text, from, agentId: from } }),
            );

        chunk("A1", "alpha");
        chunk("B1", "beta");
        chunk("A2", "alpha");
        end("alpha", "A1A2");

        // with a single shared buffer this committed "A1B1A2" attributed to alpha
        expect(said("alpha")).toEqual(["A1A2"]);

        end("beta", "B1");
        expect(said("beta")).toEqual(["B1"]);
    });
});

describe("DashboardChat — one place for its messages", () => {
    let host: FakeChatHost;
    let dc: DashboardChat;
    const origFetch = globalThis.fetch;
    const shown = (name: string): string[] => {
        dc.setTarget(name);
        return [...thread(host).querySelectorAll('[data-testid="message-body"]')].map(
            n => n.textContent ?? "",
        );
    };
    const arrive = (msg: ChatMessage) =>
        document.dispatchEvent(new CustomEvent("af-chat-message", { detail: { msg } }));
    const listed = (open: ChatMessage[], answered: ChatMessage[] = []) =>
        document.dispatchEvent(new CustomEvent("af-questions-listed", { detail: { open, answered } }));
    const asked = (id: string, over: Partial<ChatMessage> = {}): ChatMessage => ({
        id,
        from: "triage",
        to: "user",
        threadId: over.from ?? "triage",
        content: `${id}?`,
        timestampMs: 1_000,
        agentId: "triage-id",
        options: ["Yes", "No"],
        ...over,
    });

    beforeEach(() => {
        document.body.innerHTML = "";
        host = makeHost([agent("main"), agent("worker"), agent("triage")]);
        dc = mount(host);
        dc.wire();
        vi.mocked(toast.close).mockClear();
    });

    afterEach(() => {
        dc.unwire();
        globalThis.fetch = origFetch;
    });

    it("puts a question the ledger lists where its time is, above what came after it", () => {
        arrive({
            id: "later",
            from: "triage",
            to: "user",
            threadId: "triage",
            content: "since then",
            timestampMs: 5_000,
        });

        listed([asked("asked-earlier")]);

        expect(shown("triage")).toEqual(["asked-earlier?YesNo", "since then"]);
    });

    it("keeps another agent's restored history while a busy agent talks", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => [{ id: 1, ts: 1_700_000_000, role: "assistant", content: "from history" }],
        })) as unknown as typeof fetch;
        await dc.loadHistory("worker");
        // Reading the worker while main floods: what the cap must not break, and
        // main's rows are not drawn, only held.
        dc.setTarget("worker");

        for (let i = 0; i <= 500; i++) {
            arrive({
                id: `m${i}`,
                from: "main",
                to: "user",
                threadId: "main",
                content: `reply ${i}`,
                timestampMs: 10 + i,
            });
        }

        expect(shown("worker")).toEqual(["from history"]);
    });

    it("closes a question answered meanwhile, and removes one the ledger no longer lists", () => {
        listed([asked("answered-elsewhere"), asked("gone")]);

        listed([], [asked("answered-elsewhere", { answer: "Yes" })]);

        expect(shown("triage")).toEqual(["answered-elsewhere?YesNo"]);
        const buttons = [...thread(host).querySelectorAll<HTMLButtonElement>("button.af-chat-option")];
        expect(buttons.every(b => b.disabled)).toBe(true);
        expect(
            vi
                .mocked(toast.close)
                .mock.calls.map(c => c[0])
                .sort(),
        ).toEqual(["answered-elsewhere", "gone"]);
    });

    it("takes a frame delivered twice as one message", () => {
        const twice = {
            id: "r1",
            from: "main",
            to: "user",
            threadId: "main",
            content: "once",
            timestampMs: 1,
        };
        arrive(twice);
        arrive({ ...twice });

        expect(shown("main")).toEqual(["once"]);
    });
});

describe("DashboardChat — one turn at a time", () => {
    let host: FakeChatHost;
    let dc: DashboardChat;
    let input: HTMLTextAreaElement;
    const sent: string[] = [];
    const ids: string[] = [];
    const record = (e: Event) => {
        const detail = (e as CustomEvent<{ id: string; content: string }>).detail;
        sent.push(detail.content);
        ids.push(detail.id);
    };
    const type = (words: string) => {
        input.value = words;
        host.root.querySelector<HTMLButtonElement>(".af-send-btn")!.click();
    };
    const waitingToSend = () =>
        [...thread(host).querySelectorAll('[data-testid="user-message"]')]
            .filter(row => row.textContent?.includes("Waiting to send"))
            .map(row => row.querySelector('[data-testid="message-body"]')?.textContent);
    const ends = (from: string) =>
        document.dispatchEvent(new CustomEvent("af-stream-end", { detail: { text: "ok", from } }));
    const socket = (open: boolean) =>
        document.dispatchEvent(new CustomEvent("af-socket", { detail: { open } }));
    const status = (value: string) =>
        document.dispatchEvent(new CustomEvent("af-connection-status", { detail: { status: value } }));
    /** The newest send that went out fails: its id comes back, as `IOManager` sends it. */
    const fails = (content: string) =>
        document.dispatchEvent(
            new CustomEvent("af-send-failed", {
                detail: { id: ids[ids.length - 1], content, target: "main" },
            }),
        );
    const said = () =>
        [...thread(host).querySelectorAll('[data-testid="user-message"] [data-testid="message-body"]')].map(
            n => n.textContent,
        );

    beforeEach(() => {
        document.body.innerHTML = "";
        sent.length = 0;
        ids.length = 0;
        host = makeHost();
        dc = mount(host);
        dc.wire();
        host.root.appendChild(dc.buildIobar());
        input = host.root.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;
        dc.setTarget("main");
        document.addEventListener("af-send-message", record);
    });

    afterEach(() => {
        document.removeEventListener("af-send-message", record);
        vi.useRealTimers();
    });

    it("queues a message sent during a turn and sends it when the turn ends", () => {
        type("first");
        type("second");

        expect(sent).toEqual(["first"]);
        expect(waitingToSend()).toEqual(["second"]);

        ends("main");

        expect(sent).toEqual(["first", "second"]);
        expect(waitingToSend()).toEqual([]);
    });

    it("takes Enter down the same path as the button", () => {
        type("first");
        input.value = "by enter";
        input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

        expect(sent).toEqual(["first"]);
        expect(waitingToSend()).toEqual(["by enter"]);
    });

    it("keeps what is sent while the socket is closed, and sends it once it opens", () => {
        socket(false);

        type("offline");
        expect(sent).toEqual([]);
        expect(waitingToSend()).toEqual(["offline"]);

        socket(true);
        expect(sent).toEqual(["offline"]);
    });

    it("does not hold a message back while only the broker is away: main answers over the socket", () => {
        status("disconnected");

        type("hello");

        expect(sent).toEqual(["hello"]);
        expect(waitingToSend()).toEqual([]);
    });

    it("keeps a send that failed in its own bubble, waiting, and sends it first once the socket opens", () => {
        type("same");
        type("same");

        fails("same");

        // Both bubbles stay; neither is taken for the other.
        expect(said()).toEqual(["same", "same"]);
        expect(waitingToSend()).toEqual(["same", "same"]);
        socket(true);
        expect(sent).toEqual(["same", "same"]);
        expect(waitingToSend()).toEqual(["same"]);
    });

    it("stops showing the agent working once a quiet turn is given up on, and sends the next", () => {
        vi.useFakeTimers();
        type("first");
        type("second");
        expect(thread(host).querySelector('[data-testid="message-waiting"]')).not.toBeNull();

        vi.advanceTimersByTime(TURN_IDLE_TIMEOUT_MS + 1);

        expect(sent).toEqual(["first", "second"]);
    });

    it("says on the bus who the turn waits on, and what waits", () => {
        const turns: { agent: string | null; queued: number }[] = [];
        const hear = (e: Event) =>
            turns.push((e as CustomEvent<{ agent: string | null; queued: number }>).detail);
        document.addEventListener("af-turn", hear);

        type("first");
        type("second");
        ends("main");
        ends("main");

        document.removeEventListener("af-turn", hear);
        expect(turns).toEqual([
            { agent: "main", queued: 0 },
            { agent: "main", queued: 1 },
            { agent: null, queued: 1 },
            { agent: "main", queued: 0 },
            { agent: null, queued: 0 },
        ]);
    });

    it("drops what waits on a wipe", () => {
        type("first");
        type("second");

        dc.clearAll();
        ends("main");

        expect(sent).toEqual(["first"]);
    });
});

describe("DashboardChat — history & misc state", () => {
    let host: FakeChatHost;
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
        const host = makeHost();
        const dc = mount(host);
        const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true } as unknown as Response);
        pressStop(host, dc);
        expect(fetchSpy).toHaveBeenCalledWith(expect.stringContaining("/api/chat/stop"), {
            method: "POST",
        });
        fetchSpy.mockRestore();
    });

    it("stops the answer being read aloud too, before the server is asked", () => {
        const host = makeHost();
        const dc = mount(host);
        const order: string[] = [];
        const stopped = () => order.push("tts-stop");
        document.addEventListener("tts-stop", stopped);
        const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(() => {
            order.push("fetch");
            return Promise.resolve({ ok: true } as unknown as Response);
        });
        try {
            pressStop(host, dc);
        } finally {
            document.removeEventListener("tts-stop", stopped);
            fetchSpy.mockRestore();
        }
        // First, so an unreachable backend still leaves the page quiet.
        expect(order).toEqual(["tts-stop", "fetch"]);
    });

    it("says so when stop cannot reach the backend", async () => {
        const host = makeHost();
        const dc = mount(host);
        vi.mocked(toast.show).mockClear();
        const fetchSpy = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("network down"));

        pressStop(host, dc);

        // Stop against a dead backend must say so rather than appear to work.
        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ type: "alert-error" })),
        );
        fetchSpy.mockRestore();
    });

    it("says so when the backend refuses the stop", async () => {
        const host = makeHost();
        const dc = mount(host);
        vi.mocked(toast.show).mockClear();
        const fetchSpy = vi
            .spyOn(globalThis, "fetch")
            .mockResolvedValue({ ok: false, status: 503 } as unknown as Response);

        pressStop(host, dc);

        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ type: "alert-error" })),
        );
        fetchSpy.mockRestore();
    });

    it("stays quiet when stop succeeds", async () => {
        const host = makeHost();
        const dc = mount(host);
        vi.mocked(toast.show).mockClear();
        const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true } as unknown as Response);

        pressStop(host, dc);
        await drained();

        expect(fetchSpy).toHaveBeenCalled();
        expect(toast.show).not.toHaveBeenCalled();
        fetchSpy.mockRestore();
    });

    it("puts the caret in the composer when a file is attached", () => {
        // Attaching is the first half of composing; the caret belongs where the
        // second half gets typed.
        const host = makeHost();
        const dc = mount(host);
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
        const dc = mount(host);
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
        const dc = mount(host);
        host.root.appendChild(dc.buildIobar());
        dc.wire();
        const seen: Array<{ name: string; attachments: unknown[] }> = [];
        const onSend = (e: Event) => seen.push((e as CustomEvent).detail);
        document.addEventListener("af-send-message", onSend);

        document.dispatchEvent(new CustomEvent("af-attachment-added", { detail: { attachment: att() } }));
        sendFromComposer(host, dc, "@worker look at this");

        document.removeEventListener("af-send-message", onSend);
        dc.unwire();
        expect(seen).toHaveLength(1);
        expect(seen[0]!.name).toBe("worker");
        expect(seen[0]!.attachments).toHaveLength(1);
    });

    it("sends an attachments-only message and clears the pending tray", () => {
        const host = makeHost();
        const dc = mount(host);
        host.root.appendChild(dc.buildIobar());
        dc.wire();
        dc.setTarget("worker"); // the choice is what routes; the select only shows it
        const seen: Array<{ content: string; target: string; attachments: string[] }> = [];
        const onSend = (e: Event) => seen.push((e as CustomEvent).detail);
        document.addEventListener("af-send-message", onSend);

        document.dispatchEvent(new CustomEvent("af-attachment-added", { detail: { attachment: att() } }));
        sendFromComposer(host, dc, ""); // no text — the attachment alone must still send

        document.removeEventListener("af-send-message", onSend);
        expect(seen.map(s => [s.content, s.target, s.attachments])).toEqual([["", "worker", ["a1"]]]);
        expect(allByRole(host.root, "button", "Remove Image #1")).toHaveLength(0);
    });

    it("removing a blob attachment revokes its object URL; a non-blob one does not", () => {
        const host = makeHost();
        const dc = mount(host);
        host.root.appendChild(dc.buildIobar());
        dc.wire();
        const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
        const added = (attachment: object) =>
            document.dispatchEvent(new CustomEvent("af-attachment-added", { detail: { attachment } }));
        added(att({ url: "blob:abc" }));
        added(att({ id: "a2", name: "notes.txt", mime: "text/plain", url: "https://x/y.txt" }));

        byRole(host.root, "button", "Remove notes.txt").click();
        expect(revoke).not.toHaveBeenCalled(); // non-blob → nothing to release
        byRole(host.root, "button", "Remove Image #1").click();
        expect(revoke).toHaveBeenCalledWith("blob:abc");
        expect(allByRole(host.root, "button", "Remove Image #1")).toHaveLength(0);
        revoke.mockRestore();
    });

    it("sending revokes pending blob attachment URLs (dev-stub uploads)", () => {
        const host = makeHost();
        const dc = mount(host);
        host.root.appendChild(dc.buildIobar());
        dc.wire();
        const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
        document.dispatchEvent(
            new CustomEvent("af-attachment-added", { detail: { attachment: att({ url: "blob:send-me" }) } }),
        );

        sendFromComposer(host, dc, "@worker hi");

        expect(revoke).toHaveBeenCalledWith("blob:send-me");
        expect(allByRole(host.root, "button", "Remove Image #1")).toHaveLength(0);
        revoke.mockRestore();
    });

    it("clearAll revokes pending blob attachment URLs", () => {
        const host = makeHost();
        const dc = mount(host);
        host.root.appendChild(dc.buildIobar());
        dc.wire();
        const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
        document.dispatchEvent(
            new CustomEvent("af-attachment-added", { detail: { attachment: att({ url: "blob:wipe-me" }) } }),
        );

        dc.clearAll();

        expect(revoke).toHaveBeenCalledWith("blob:wipe-me");
        revoke.mockRestore();
    });

    it("af-reset-chat scoped to an agent drops that agent + user turns, keeps others", () => {
        const host = makeHost();
        const dc = mount(host);
        dc.wire();
        const arrive = (msg: ChatMessage) =>
            document.dispatchEvent(new CustomEvent("af-chat-message", { detail: { msg } }));
        arrive({ id: "1", from: "worker", to: "user", threadId: "worker", content: "w", timestampMs: 1 });
        arrive({ id: "2", from: "main", to: "user", threadId: "main", content: "m", timestampMs: 2 });
        sendReaching(host, dc, "@worker u", "worker");

        document.dispatchEvent(new CustomEvent("af-reset-chat", { detail: { agent: "worker" } }));

        const shown = (name: string) => {
            dc.setTarget(name);
            return [...thread(host).querySelectorAll('[data-testid="message-body"]')].map(n => n.textContent);
        };
        expect(shown("worker")).toEqual([]);
        expect(shown("main")).toEqual(["m"]);
        dc.unwire();
    });

    it("the target select pins priority names then orders the rest alphabetically", () => {
        const host = makeHost([agent("zeta"), agent("alpha"), agent("main")]);
        const dc = mount(host);
        host.root.appendChild(dc.buildIobar());

        dc.updateTargetSelect();

        const select = host.root.querySelector<HTMLSelectElement>("#af-target-select")!;
        expect([...select.options].map(o => o.value)).toEqual(["main", "alpha", "zeta"]);
    });

    it("re-rendering mid-stream reattaches the live bubble with its accumulated text", () => {
        const host = makeHost();
        const dc = mount(host);
        dc.wire();
        document.dispatchEvent(
            new CustomEvent("af-stream-chunk", {
                detail: { chunk: "partial reply", from: "main", text: "partial reply" },
            }),
        );

        dc.renderChatThread();

        expect(thread(host).textContent).toContain("partial reply");
        dc.unwire();
    });
});

describe("DashboardChat — @mention target stickiness", () => {
    afterEach(() => vi.restoreAllMocks());

    it("stays on an @mentioned agent after a later reconcile (no snap back to main)", () => {
        globalThis.fetch = vi.fn(async () => ({ ok: true, json: async () => [] })) as unknown as typeof fetch;
        const host = makeHost([agent("main"), agent("catalog")], "chat");
        const dc = mount(host);
        dc.wire();
        host.root.appendChild(dc.buildIobar());

        sendReaching(host, dc, "@catalog spawn weather agent", "catalog");
        // the @mention reached catalog, and the open thread followed it
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
        sendFromComposer(host, chat, "@catalog spawn weather agent");
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
        // harness supplies it the way the dashboard does.
        host.root.appendChild(chat.buildIobar());
        const input = host.root.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;
        chat.wire();

        sendReaching(host, chat, "@catalog spawn weather agent", "catalog");

        expect(input.placeholder).toBe("Message @catalog…");
    });

    it("opens the conversation when sending while the list is showing", () => {
        // Already in the chat view, list showing: nothing mounts, so clearing
        // the flag is not enough on its own — the send path has to apply it.
        const { host, chat, el } = mount([agent("main"), agent("catalog")]);
        host.root.querySelector<HTMLElement>(".af-chat-back-btn")?.click();
        expect(el()?.classList.contains("agent-selected")).toBe(false);

        chat.wire();
        sendFromComposer(host, chat, "@catalog spawn weather-agent");

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

describe("two agents of one name", () => {
    // One on main and one on a Pi: told apart by id, and on screen by `name@node`.
    const twins = (): FakeChatHost => {
        const host = makeHost([
            agent("main", { id: "main-id" }),
            agent("counter", { id: "on-main" }),
            agent("counter", { id: "on-pi", node: "pi" }),
        ]);
        return {
            ...host,
            label: a => (a.name === "counter" ? `counter@${a.id === "on-pi" ? "pi" : "main"}` : a.name),
        };
    };
    const said = (host: ChatHost) =>
        [...thread(host).querySelectorAll('[data-testid="message-body"]')].map(n => n.textContent);
    const arrive = (msg: ChatMessage) =>
        document.dispatchEvent(new CustomEvent("af-chat-message", { detail: { msg } }));
    let fetched: string[];

    beforeEach(() => {
        document.body.innerHTML = "";
        fetched = [];
        globalThis.fetch = vi.fn(async (url: string) => {
            fetched.push(url);
            return { ok: true, json: async () => [] };
        }) as unknown as typeof fetch;
    });
    afterEach(() => vi.restoreAllMocks());

    it("lists each under the name the page gives it", () => {
        const host = twins();
        mount(host);

        const list = byTestId(host.root, "chat-agent-list");
        expect(allByRole(list, "button").map(b => b.textContent)).toEqual([
            "main",
            "counter@main",
            "counter@pi",
        ]);
    });

    it("keeps a thread for each, and fetches each one's history by its id", () => {
        const host = twins();
        const dc = mount(host);
        dc.wire();
        arrive({
            id: "a",
            from: "counter",
            to: "user",
            threadId: "on-main",
            content: "here",
            timestampMs: 1,
        });
        arrive({ id: "b", from: "counter", to: "user", threadId: "on-pi", content: "there", timestampMs: 2 });

        byRole(byTestId(host.root, "chat-agent-list"), "button", "counter@pi").click();

        expect(dc.chatTarget).toBe("on-pi");
        expect(said(host)).toEqual(["there"]);
        expect(fetched.some(url => url.includes("agent_id=on-pi"))).toBe(true);

        byRole(byTestId(host.root, "chat-agent-list"), "button", "counter@main").click();

        expect(said(host)).toEqual(["here"]);
    });

    it("opened by the name alone, opens neither and offers each by its address", () => {
        const host = twins();
        const dc = mount(host);

        dc.openByName("counter");

        expect(dc.chatTarget).toBe("");
        const links = [...thread(host).querySelectorAll("a")];
        expect(links.map(a => [a.textContent, a.getAttribute("href")])).toEqual([
            ["@counter@main", "#/chat/counter@main"],
            ["@counter@pi", "#/chat/counter@pi"],
        ]);
    });

    it("opened by an address, opens that one", () => {
        const host = twins();
        const dc = mount(host);

        dc.openByName("counter@pi");

        expect(dc.chatTarget).toBe("on-pi");
        expect(dc.chatTargetAddress).toBe("counter@pi");
    });

    it("keeps a turn sent to one waiting through the other's reply", () => {
        const host = twins();
        const dc = mount(host);
        dc.wire();
        dc.setTarget("on-pi");
        const turns: (string | null)[] = [];
        const stop = listen("af-turn", t => turns.push(t.agent));
        sendFromComposer(host, dc, "count");

        arrive({ id: "a", from: "counter", to: "user", threadId: "on-main", content: "1", timestampMs: 1 });
        expect(turns.at(-1)).toBe("on-pi");

        arrive({ id: "b", from: "counter", to: "user", threadId: "on-pi", content: "2", timestampMs: 2 });
        document.removeEventListener("af-turn", stop);
        expect(turns.at(-1)).toBeNull();
        dc.unwire();
    });

    it("offers the addresses a bare name fits, and sends the message again to the one picked", () => {
        const host = twins();
        const dc = mount(host);
        dc.wire();
        dc.setTarget("main-id");
        const sends: { id: string; content: string; answerer: string }[] = [];
        const record = (e: Event) => sends.push((e as CustomEvent<(typeof sends)[number]>).detail);
        document.addEventListener("af-send-message", record);
        sendFromComposer(host, dc, "@counter five");
        arrive({
            id: "r1",
            from: "main",
            to: "user",
            threadId: "main-id",
            content: "More than one agent is called 'counter'.",
            timestampMs: 2,
            addressChoice: { addresses: ["counter@main", "counter@pi"], retryOf: sends[0]!.id },
        });

        byRole(thread(host), "button", "@counter@pi").click();
        byRole(thread(host), "button", "@counter@main").click();
        document.removeEventListener("af-send-message", record);

        expect(sends.map(s => [s.content, s.answerer])).toEqual([
            ["@counter five", "main-id"],
            ["@counter@pi five", "on-pi"],
        ]);
        expect((byRole(thread(host), "button", "@counter@main") as HTMLButtonElement).disabled).toBe(true);
        dc.unwire();
    });

    it("moves a message to the one the server says it reached, and follows it", () => {
        const host = twins();
        const dc = mount(host);
        dc.wire();
        dc.setTarget("main-id");
        let sent = "";
        const record = (e: Event) => {
            sent = (e as CustomEvent<{ id: string }>).detail.id;
        };
        document.addEventListener("af-send-message", record);
        sendFromComposer(host, dc, "@counter@pi count");
        document.removeEventListener("af-send-message", record);

        document.dispatchEvent(
            new CustomEvent("af-turn-reached", { detail: { id: sent, agentId: "on-pi", name: "counter" } }),
        );

        expect(dc.chatTarget).toBe("on-pi");
        expect(said(host)).toEqual(["count"]);
        dc.setTarget("main-id");
        expect(said(host)).toEqual([]);
    });
});
