/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import type { AgentStore } from "../agents/AgentStore";
import { emit } from "../events";
import type { CardDashboard } from "../ui/CardDashboard";
import type { FeedItem } from "../types/feed";
import type { HeartbeatPayload } from "../types/agent";
import {
    agent,
    buildDashboard,
    cardOf,
    goTo,
    openDashboard,
    pressView,
    settled,
    shownView,
} from "./dashboard";
import { allByRole, byRole, byTestId } from "./queries";

// Behaviour of the dashboard as a whole, driven the way the page drives it --
// the store, the event bus, a click -- and read off the screen: feed pushes,
// wipes and clears, connection status, what a change in the store does to the
// cards and the nodes panel, the card actions, and the timers.

function feedItem(over: Partial<FeedItem> = {}): FeedItem {
    return { type: "chat", label: "hello", agentName: "main", timestamp: 1_700_000_000_000, ...over };
}

function hb(name: string, over: Partial<HeartbeatPayload> = {}): HeartbeatPayload {
    return {
        agentId: name,
        agentName: name,
        state: "running",
        sequence: 1,
        timestampMs: Date.now(),
        ...over,
    };
}

/** How many times `text` is on screen in the view that is open. */
function timesShown(text: string): number {
    return (byTestId(document, `view-${shownView()}`).textContent ?? "").split(text).length - 1;
}

/** The value of the overview's stat card called `label`. */
function stat(label: string): string {
    const name = [...document.querySelectorAll("div")].find(
        node => node.textContent === label && node.childElementCount === 0,
    );
    return name?.nextElementSibling?.textContent ?? "";
}

/** The command events the dashboard sends while `act` runs. */
function commandsDuring(act: () => void): unknown[] {
    const seen: unknown[] = [];
    const onCmd = (e: Event) => seen.push((e as CustomEvent).detail);
    document.addEventListener("af-agent-command", onCmd);
    try {
        act();
    } finally {
        document.removeEventListener("af-agent-command", onCmd);
    }
    return seen;
}

let store: AgentStore;
let dashboard: CardDashboard;

function open(...names: string[]): void {
    ({ store, dashboard } = openDashboard(names.map(n => agent(n))));
}

beforeEach(() => {
    document.body.replaceChildren();
    localStorage.clear();
    history.replaceState(null, "", "#");
});

afterEach(() => {
    dashboard?.destroy();
    vi.useRealTimers();
    vi.restoreAllMocks();
});

describe("feed events", () => {
    it("a pushed item shows in the open feed, once however often it comes", () => {
        open("main");
        goTo("feed");
        const item = feedItem({ label: "first" });
        emit("af-feed-push", { item });
        emit("af-feed-push", { item });
        expect(timesShown("first")).toBe(1);
    });

    it("a log line pushed after leaving the activity view is there on coming back", () => {
        open("main");
        goTo("feed");
        goTo("overview");
        const entries = [
            { source: "app", ts: 1_700_000_000, level: "INFO", origin: "wactorz.web", text: "while away" },
        ];
        emit("af-app-log", { entries });
        goTo("feed");

        expect(timesShown("while away")).toBe(1);
    });

    it("an item pushed while another view is open is there in the feed", () => {
        open("main");
        emit("af-feed-push", { item: feedItem({ label: "meanwhile" }) });
        goTo("feed");
        expect(timesShown("meanwhile")).toBe(1);
    });

    it("af-clear-feed and af-wipe-all empty the feed", () => {
        open("main");
        goTo("feed");
        emit("af-feed-push", { item: feedItem({ label: "one" }) });
        emit("af-clear-feed");
        expect(timesShown("one")).toBe(0);

        emit("af-feed-push", { item: feedItem({ label: "two" }) });
        emit("af-wipe-all", { survivors: [] });
        expect(timesShown("two")).toBe(0);
    });

    it("a wipe zeroes the totals", () => {
        open("main");
        dashboard.setTotalCostUsd(3);
        dashboard.setTotalMessages(12);
        emit("af-wipe-all", { survivors: [] });
        goTo("overview");
        expect([stat("Cost"), stat("Messages")]).toEqual(["$0.0000", "0"]);
    });
});

describe("connection status", () => {
    it.each([
        ["live", "● live"],
        ["connecting", "○ Connecting…"],
        ["disconnected", "◎ Disconnected…"],
    ] as const)("renders the %s badge", (status, text) => {
        open("main");
        emit("af-connection-status", { status });
        expect(byTestId(document, "connection").textContent).toBe(text);
    });

    it("the health line counts running agents", () => {
        ({ store, dashboard } = openDashboard([agent("main"), agent("idle", { state: "stopped" })]));
        emit("af-connection-status", { status: "live" });
        expect(document.body.textContent).toContain("1/2 wactorz healthy");
    });
});

describe("the Devices link", () => {
    const links = () =>
        allByRole(document, "link", "Open Home Assistant in a new tab") as HTMLAnchorElement[];

    it("points at the stored Home Assistant URL, in a new tab", () => {
        localStorage.setItem("wactorz-ha-url", "http://ha.local:8123");
        ({ store, dashboard } = buildDashboard());
        const link = links()[0]!;
        expect(link.getAttribute("href")).toBe("http://ha.local:8123");
        expect(link.target).toBe("_blank");
        // noreferrer as well: Home Assistant has no use for this page's address.
        expect(link.rel).toBe("noopener noreferrer");
    });

    it("is hidden when no Home Assistant URL is configured", () => {
        ({ store, dashboard } = buildDashboard());
        expect(links().every(a => !a.hasAttribute("href") && a.style.display === "none")).toBe(true);
    });

    it("hides once the server runs without Home Assistant, though this browser saw one", async () => {
        localStorage.setItem("wactorz-ha-url", "http://ha.local");
        localStorage.setItem("wactorz-ha-url__server", "http://ha.local");
        vi.spyOn(globalThis, "fetch").mockResolvedValue({
            ok: true,
            json: async () => ({ ha: { url: "" } }),
        } as Response);

        open("main");

        await vi.waitFor(() => {
            expect(links().length).toBeGreaterThan(0);
            expect(links().every(a => a.style.display === "none" && !a.hasAttribute("href"))).toBe(true);
        });
    });
});

describe("the agents on the overview", () => {
    it("draws an agent the store gains, and takes off one it loses", async () => {
        open("main");
        store.addOrUpdateAgent(agent("catalog"));
        await settled();
        expect(cardOf("catalog")).toBeTruthy();

        vi.useFakeTimers();
        store.removeAgent("catalog");
        await settled();
        vi.advanceTimersByTime(300);
        expect(allByRole(document, "listitem", "catalog")).toEqual([]);
    });

    it("repaints a card's state when a heartbeat changes it", async () => {
        open("main", "worker");
        await settled(); // drawn already: the heartbeat is a change of its own
        store.onHeartbeat(hb("worker", { state: "stopped" }));
        await settled();
        expect(byTestId(cardOf("worker"), "agent-state").textContent).toBe("stopped");
    });
});

describe("two agents of one name", () => {
    beforeEach(() => {
        open("main");
        store.addOrUpdateAgent(agent("counter", { id: "c-main" }));
        store.addOrUpdateAgent(agent("counter", { id: "c-pi", node: "pi" }));
    });

    it("are two cards, each called by its address", async () => {
        await settled();
        expect(cardOf("counter@main")).not.toBe(cardOf("counter@pi"));
        expect(byRole(cardOf("counter@pi"), "button", "History of counter@pi")).toBeTruthy();
    });

    it("say which node each runs on", async () => {
        await settled();
        expect(byTestId(cardOf("counter@pi"), "agent-node").textContent).toBe("on pi");
        expect(byTestId(cardOf("counter@main"), "agent-node").hidden).toBe(true);
    });

    it("leave the one left called by its name alone", async () => {
        await settled();
        vi.useFakeTimers();
        store.removeAgent("c-pi");
        await settled();
        vi.advanceTimersByTime(300);
        expect(byTestId(cardOf("counter"), "agent-node").hidden).toBe(true);
    });

    it("are each asked about by address before a delete", async () => {
        await settled();
        byRole(cardOf("counter@pi"), "button", "Delete counter@pi").click();
        expect(byTestId(document, "confirm-message").textContent).toContain("counter@pi");
        byTestId(document, "confirm-cancel").click();
    });
});

describe("the nodes", () => {
    it("draws a node the store hears from", async () => {
        open("main");
        store.updateRemoteNode("edge-1", ["alpha", "beta"]);
        await settled();
        expect(byTestId(cardOf("edge-1"), "node-agents").textContent).toContain("alpha");
    });

    it("shows a node the server lists before its first heartbeat reaches the page", async () => {
        vi.spyOn(globalThis, "fetch").mockImplementation(async input =>
            String(input).endsWith("/api/nodes")
                ? ({
                      ok: true,
                      json: async () => ({
                          nodes: [
                              { node: "rpi", online: true, last_seen: Date.now() / 1000, agents: ["flic"] },
                          ],
                      }),
                  } as Response)
                : ({ ok: false, json: async () => ({}) } as Response),
        );

        open("main");

        await vi.waitFor(() => expect(cardOf("rpi").textContent).toContain("online"));
    });

    it("takes a listing in off the overview too, drawing it there later", async () => {
        vi.spyOn(globalThis, "fetch").mockImplementation(async input =>
            String(input).endsWith("/api/nodes")
                ? ({ ok: true, json: async () => ({ nodes: [{ node: "rpi", online: true }] }) } as Response)
                : ({ ok: false, json: async () => ({}) } as Response),
        );
        history.replaceState(null, "", "#/feed");

        open("main");

        await vi.waitFor(() => expect(store.nodes.has("rpi")).toBe(true));
        goTo("overview");
        expect(cardOf("rpi")).toBeTruthy();
    });

    it("lists an agent under the node it moved to", async () => {
        ({ store, dashboard } = openDashboard([agent("main"), agent("counter", { node: "edge" })]));
        const localRow = () => byTestId(cardOf("local"), "node-agents").textContent;
        expect(localRow()).not.toContain("counter");
        await settled();

        store.onHeartbeat(hb("counter", { node: "" }));
        await settled();

        expect(localRow()).toContain("counter");
        expect(byTestId(cardOf("counter"), "agent-node").hidden).toBe(true);
    });

    it("lists an agent that moved while another view showed once the overview is back", async () => {
        ({ store, dashboard } = openDashboard([agent("main"), agent("counter", { node: "edge" })]));
        await settled();
        goTo("chat");
        store.onHeartbeat(hb("counter", { node: "" }));
        await settled();
        goTo("overview");

        expect(byTestId(cardOf("local"), "node-agents").textContent).toContain("counter");
    });

    it("lists an agent that moved while the dashboard was hidden once it is shown", async () => {
        ({ store, dashboard } = buildDashboard([agent("counter", { node: "edge" })]));
        store.onHeartbeat(hb("counter", { node: "" }));
        await settled();
        dashboard.show();

        expect(byTestId(cardOf("local"), "node-agents").textContent).toContain("counter");
    });
});

describe("the card actions", () => {
    it("send the command, with the button busy for a moment", () => {
        vi.useFakeTimers();
        open("main");
        const stop = byRole(cardOf("main"), "button", "Stop main") as HTMLButtonElement;

        expect(commandsDuring(() => stop.click())).toEqual([{ command: "stop", agentId: "main" }]);
        expect(stop.disabled).toBe(true);
        vi.advanceTimersByTime(600);
        expect(stop.disabled).toBe(false);
    });

    it("start and stop are their own way back, so they do not ask", () => {
        ({ store, dashboard } = openDashboard([agent("main"), agent("idle", { state: "stopped" })]));
        const sent = commandsDuring(() => {
            byRole(cardOf("main"), "button", "Stop main").click();
            byRole(cardOf("idle"), "button", "Start idle").click();
        });
        expect(allByRole(document, "dialog")).toEqual([]);
        expect(sent).toEqual([
            { command: "stop", agentId: "main" },
            { command: "start", agentId: "idle" },
        ]);
    });

    // Delete is the one card action with no way back, so it asks first --
    // in an overlay of our own, never the platform's blocking confirm().
    it("asks before deleting, naming the agent, and sends nothing when declined", async () => {
        open("main", "weather-watcher");
        const sent = commandsDuring(() =>
            byRole(cardOf("weather-watcher"), "button", "Delete weather-watcher").click(),
        );
        expect(byTestId(document, "confirm-message").textContent).toContain("weather-watcher");

        byTestId(document, "confirm-cancel").click();
        await Promise.resolve();

        expect(sent).toEqual([]);
    });

    it("sends the delete once the question is answered", async () => {
        open("main", "weather-watcher");
        byRole(cardOf("weather-watcher"), "button", "Delete weather-watcher").click();
        const sent: unknown[] = [];
        const onCmd = (e: Event) => sent.push((e as CustomEvent).detail);
        document.addEventListener("af-agent-command", onCmd);
        byTestId(document, "confirm-ok").click();
        await vi.waitFor(() => expect(sent).toEqual([{ command: "delete", agentId: "weather-watcher" }]));
        document.removeEventListener("af-agent-command", onCmd);
    });

    it("names the agent by its id when it left before the press", () => {
        ({ store, dashboard } = openDashboard([agent("main"), agent("weather", { id: "w-7" })]));
        const del = byRole(cardOf("weather"), "button", "Delete weather");
        store.removeAgent("w-7"); // gone from the store; the card is not redrawn yet

        del.click();

        expect(byTestId(document, "confirm-message").textContent).toContain("w-7");
        byTestId(document, "confirm-cancel").click();
    });

    it("Chat opens the chat view", () => {
        open("main");
        byRole(cardOf("main"), "button", "Chat with main").click();
        expect(shownView()).toBe("chat");
    });

    it("a header view button switches the view", () => {
        open("main");
        pressView("Feed");
        expect(shownView()).toBe("feed");
    });
});

describe("moments on a card", () => {
    it("a heartbeat says how long ago it came", async () => {
        open("main");
        expect(cardOf("main").textContent).toContain("♥ —");
        store.onHeartbeat(hb("main"));
        await settled();
        expect(cardOf("main").textContent).toContain("♥ now");
    });

    it("a heartbeat while hidden is kept for when the cards are drawn", async () => {
        open("main");
        dashboard.hide();
        store.onHeartbeat(hb("main"));
        await settled();
        dashboard.show();
        expect(cardOf("main").textContent).toContain("♥ now");
    });

    it("an alert and a chat message flash the card for a moment", async () => {
        vi.useFakeTimers();
        open("main");
        store.onAlert({
            agentId: "main",
            agentName: "main",
            severity: "error",
            message: "x",
            timestampMs: 1,
        });
        store.onChat("main");
        await settled();
        expect([...cardOf("main").classList]).toEqual(
            expect.arrayContaining(["af-card-alert-error", "af-card-chat-flash"]),
        );
        store.onAlert({
            agentId: "main",
            agentName: "main",
            severity: "warning",
            message: "x",
            timestampMs: 1,
        });
        await settled();
        expect(cardOf("main").classList.contains("af-card-alert-warn")).toBe(true);

        vi.advanceTimersByTime(1000);
        expect([...cardOf("main").classList]).not.toEqual(expect.arrayContaining(["af-card-alert-error"]));
        expect(cardOf("main").classList.contains("af-card-chat-flash")).toBe(false);
    });

    it("a moment for an agent with no card shows nowhere", async () => {
        open("main");
        store.onAlert({
            agentId: "ghost",
            agentName: "ghost",
            severity: "error",
            message: "x",
            timestampMs: 1,
        });
        await expect(settled()).resolves.toBeUndefined();
    });

    it("a card that leaves forgets when it was last heard from", async () => {
        open("main", "catalog");
        store.onHeartbeat(hb("catalog"));
        await settled();
        vi.useFakeTimers();
        store.removeAgent("catalog");
        await settled();
        vi.advanceTimersByTime(300);
        vi.useRealTimers();

        store.addOrUpdateAgent(agent("catalog"));
        await settled();

        expect(cardOf("catalog").textContent).toContain("♥ —");
    });
});

describe("the timers", () => {
    it("ages every card's last heartbeat as time passes", async () => {
        vi.useFakeTimers();
        open("main");
        store.onHeartbeat(hb("main"));
        await settled();
        vi.advanceTimersByTime(65_000);
        expect(cardOf("main").textContent).toContain("♥ 1m ago");
    });

    it("leaves the ages alone while the tab is hidden", async () => {
        vi.useFakeTimers();
        open("main");
        store.onHeartbeat(hb("main"));
        await settled();
        vi.spyOn(document, "hidden", "get").mockReturnValue(true);
        vi.advanceTimersByTime(65_000);
        expect(cardOf("main").textContent).toContain("♥ now");
    });
});

describe("the chat's agent list follows the store", () => {
    it("gains and loses an agent while the chat is open", async () => {
        open("main");
        goTo("chat");
        const list = () => byTestId(document, "chat-agent-list");

        store.addOrUpdateAgent(agent("catalog"));
        await settled();
        expect(allByRole(list(), "button", "catalog")).toHaveLength(1);

        store.removeAgent("catalog");
        await settled();
        expect(allByRole(list(), "button", "catalog")).toEqual([]);
    });
});

describe("an extension's view", () => {
    it("is added when the extension asks for it", () => {
        open("main");
        const content = document.createElement("div");
        content.textContent = "custom-view";

        emit("af-register-view", { key: "custom", icon: "hexagon", label: "Custom", build: () => content });
        pressView("Custom");

        expect(shownView()).toBe("custom");
        expect(document.body.textContent).toContain("custom-view");
    });
});

describe("entering the chat view opens a conversation", () => {
    beforeEach(() => open("main", "worker"));

    const paneOpen = () => byTestId(document, "view-chat").classList.contains("agent-selected");

    it("arriving from another view clears a previous Back", () => {
        goTo("chat");
        byRole(document, "button", "‹ Back").click();
        expect(paneOpen()).toBe(false);

        goTo("overview");
        goTo("chat");

        expect(paneOpen()).toBe(true);
    });

    it("re-rendering while already in chat leaves the list showing", () => {
        // A redraw also runs for state updates. Resetting on every one would
        // undo Back the moment anything changed on screen.
        goTo("chat");
        byRole(document, "button", "‹ Back").click();

        goTo("chat");

        expect(paneOpen()).toBe(false);
    });
});
