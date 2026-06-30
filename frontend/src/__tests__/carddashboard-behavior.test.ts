/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { CardDashboard } from "../ui/CardDashboard";
import type { AgentInfo } from "../types/agent";
import type { FeedItem } from "../types/feed";

// Behaviour tests for CardDashboard's event handlers and render helpers — the
// document-event bus (feed push/dedup, wipe/clear, connection status), per-card
// effects (heartbeat, alert, chat flash), remote nodes, command dispatch and the
// timestamp refresh. Complements carddashboard-render.test.ts (structure smoke).

function agent(name: string, over: Partial<AgentInfo> = {}): AgentInfo {
    return { id: name, name, state: "running", protected: false, ...over };
}

function feedItem(over: Partial<FeedItem> = {}): FeedItem {
    return { type: "chat", label: "hello", agentName: "main", timestamp: 1_700_000_000_000, ...over };
}

describe("CardDashboard behaviour", () => {
    let cd: any;

    beforeEach(() => {
        document.body.innerHTML = "";
        localStorage.clear();
        cd = new CardDashboard() as any;
    });

    afterEach(() => {
        try {
            cd.destroy();
        } catch {
            /* ignore */
        }
        vi.useRealTimers();
    });

    describe("feed events", () => {
        it("af-feed-push appends to the open feed view and dedups by key", () => {
            cd.show([agent("main")]);
            cd._setView("feed");
            const item = feedItem({ label: "first" });
            document.dispatchEvent(new CustomEvent("af-feed-push", { detail: { item } }));
            expect(cd.feedItems.length).toBe(1);
            expect(cd.root.querySelector(".af-feed")?.textContent).toContain("first");
            // exact duplicate is dropped
            document.dispatchEvent(new CustomEvent("af-feed-push", { detail: { item } }));
            expect(cd.feedItems.length).toBe(1);
        });

        it("af-feed-push while not on the feed view still records the item", () => {
            cd.show([agent("main")]);
            cd._setView("overview");
            document.dispatchEvent(new CustomEvent("af-feed-push", { detail: { item: feedItem() } }));
            expect(cd.feedItems.length).toBe(1);
        });

        it("af-wipe-all and af-clear-feed empty the feed", () => {
            cd.show([agent("main")]);
            document.dispatchEvent(new CustomEvent("af-feed-push", { detail: { item: feedItem() } }));
            expect(cd.feedItems.length).toBe(1);
            document.dispatchEvent(new CustomEvent("af-clear-feed"));
            expect(cd.feedItems.length).toBe(0);

            document.dispatchEvent(new CustomEvent("af-feed-push", { detail: { item: feedItem() } }));
            expect(cd.feedItems.length).toBe(1);
            document.dispatchEvent(new CustomEvent("af-wipe-all"));
            expect(cd.feedItems.length).toBe(0);
        });
    });

    describe("connection status", () => {
        it.each([
            ["live", "● live"],
            ["connecting", "○ Connecting…"],
            ["demo", "◎ Demo fallback"],
        ])("renders the %s badge", (status, text) => {
            cd.show([agent("main")]);
            document.dispatchEvent(new CustomEvent("af-connection-status", { detail: { status } }));
            expect(cd.connState).toBe(status);
            expect(cd.root.querySelector(".af-conn-badge")?.textContent).toBe(text);
        });

        it("health line counts running agents", () => {
            cd.show([agent("main"), agent("idle", { state: "stopped" })]);
            document.dispatchEvent(new CustomEvent("af-connection-status", { detail: { status: "live" } }));
            expect(cd.root.querySelector(".af-health")?.textContent).toBe("1/2 wactorz healthy");
        });
    });

    describe("remote nodes", () => {
        it("updateRemoteNode stores the node and renders it on the overview", () => {
            cd.show([agent("main")]);
            cd.updateRemoteNode("edge-1", ["alpha", "beta"]);
            expect(cd._remoteNodes.get("edge-1")?.agents).toEqual(["alpha", "beta"]);
            expect(cd.root.textContent).toContain("edge-1");
        });

        it("updateRemoteNode off the overview view does not throw", () => {
            cd.show([agent("main")]);
            cd._setView("feed");
            expect(() => cd.updateRemoteNode("edge-2", [])).not.toThrow();
            expect(cd._remoteNodes.has("edge-2")).toBe(true);
        });
    });

    describe("_sendCommand", () => {
        it("dispatches af-agent-command and disables the button", () => {
            const seen: any[] = [];
            const onCmd = (e: Event) => seen.push((e as CustomEvent).detail);
            document.addEventListener("af-agent-command", onCmd);
            const btn = document.createElement("button");
            cd._sendCommand("main", "pause", btn);
            document.removeEventListener("af-agent-command", onCmd);
            expect(seen).toEqual([{ command: "pause", agentId: "main" }]);
            expect(btn.disabled).toBe(true);
            expect(btn.classList.contains("sending")).toBe(true);
        });

        it("works without a button argument", () => {
            const seen: any[] = [];
            const onCmd = (e: Event) => seen.push((e as CustomEvent).detail);
            document.addEventListener("af-agent-command", onCmd);
            cd._sendCommand("x", "stop");
            document.removeEventListener("af-agent-command", onCmd);
            expect(seen).toEqual([{ command: "stop", agentId: "x" }]);
        });
    });

    describe("per-card effects", () => {
        it("onHeartbeat updates the card's relative time and pulses the dot", () => {
            cd.show([agent("main")]);
            cd.onHeartbeat("main", Date.now());
            const card = cd.root.querySelector('[data-id="main"]');
            expect(card.querySelector(".af-card-state-dot").classList.contains("af-card-pulse")).toBe(true);
        });

        it("onHeartbeat is a no-op for an unknown card or while hidden", () => {
            cd.show([agent("main")]);
            expect(() => cd.onHeartbeat("ghost", Date.now())).not.toThrow();
            cd.hide();
            expect(() => cd.onHeartbeat("main", Date.now())).not.toThrow();
        });

        it("showAlert flags error vs warn and onChat flashes the card", () => {
            cd.show([agent("main")]);
            cd.showAlert("main", "error");
            expect(cd.root.querySelector('[data-id="main"]').classList.contains("af-card-alert-error")).toBe(
                true,
            );
            cd.showAlert("main", "warning");
            expect(cd.root.querySelector('[data-id="main"]').classList.contains("af-card-alert-warn")).toBe(
                true,
            );
            cd.onChat("main", "user");
            expect(cd.root.querySelector('[data-id="main"]').classList.contains("af-card-chat-flash")).toBe(
                true,
            );
            // unknown ids are no-ops
            expect(() => cd.showAlert("ghost", "error")).not.toThrow();
            expect(() => cd.onChat("ghost", "user")).not.toThrow();
        });

        it("_refreshTimestamps updates known cards and marks stale dots", () => {
            cd.show([agent("main")]);
            cd.lastHb.set("main", Date.now() - 200_000); // older than STALE_MS (180s)
            cd._refreshTimestamps();
            const dot = cd.root.querySelector('[data-id="main"] .af-card-state-dot');
            expect(dot.classList.contains("af-card-stale")).toBe(true);
        });
    });

    describe("overview card interactions", () => {
        it("the card Chat button switches to the chat view", () => {
            cd.show([agent("main")]);
            (cd.root.querySelector(".af-chat-btn") as HTMLButtonElement).click();
            expect(cd.view).toBe("chat");
        });

        it("a card action button routes through onCommand → af-agent-command", () => {
            const seen: any[] = [];
            const onCmd = (e: Event) => seen.push((e as CustomEvent).detail);
            document.addEventListener("af-agent-command", onCmd);
            cd.show([agent("main")]);
            (cd.root.querySelector('[data-action="pause"]') as HTMLButtonElement).click();
            document.removeEventListener("af-agent-command", onCmd);
            expect(seen).toEqual([{ command: "pause", agentId: "main" }]);
        });

        it("a header view button switches the active view", () => {
            cd.show([agent("main")]);
            (cd.root.querySelector('.af-header .af-view-btn[data-view="feed"]') as HTMLButtonElement).click();
            expect(cd.view).toBe("feed");
        });
    });

    describe("timer-driven callbacks", () => {
        it("removeAgent animates then drops the card after the timeout", () => {
            vi.useFakeTimers();
            cd.show([agent("main"), agent("catalog")]);
            cd.removeAgent("catalog");
            expect(cd._removingIds.has("catalog")).toBe(true);
            vi.advanceTimersByTime(300);
            expect(cd.root.querySelector('[data-id="catalog"]')).toBeNull();
            expect(cd._removingIds.has("catalog")).toBe(false);
        });

        it("showAlert and onChat clear their class after the timeout", () => {
            vi.useFakeTimers();
            cd.show([agent("main")]);
            cd.showAlert("main", "error");
            cd.onChat("main", "user");
            vi.advanceTimersByTime(1000);
            const card = cd.root.querySelector('[data-id="main"]');
            expect(card.classList.contains("af-card-alert-error")).toBe(false);
            expect(card.classList.contains("af-card-chat-flash")).toBe(false);
        });

        it("_sendCommand re-enables its button after the timeout", () => {
            vi.useFakeTimers();
            const btn = document.createElement("button");
            cd._sendCommand("main", "pause", btn);
            expect(btn.disabled).toBe(true);
            vi.advanceTimersByTime(600);
            expect(btn.disabled).toBe(false);
            expect(btn.classList.contains("sending")).toBe(false);
        });

        it("the tick interval refreshes timestamps", () => {
            vi.useFakeTimers();
            const fresh = new CardDashboard() as any;
            fresh.show([agent("main")]);
            const spy = vi.spyOn(fresh, "_refreshTimestamps");
            vi.advanceTimersByTime(5000);
            expect(spy).toHaveBeenCalled();
            fresh.destroy();
        });
    });

    describe("agent-map mutations across views/visibility", () => {
        it("add/update/remove agent re-render the chat sidebar in chat view", () => {
            cd.show([agent("main")]);
            cd._setView("chat");
            expect(() => {
                cd.addAgent(agent("catalog"));
                cd.updateAgent(agent("catalog", { state: "paused" }));
                cd.removeAgent("catalog");
            }).not.toThrow();
        });

        it("update/remove agent are no-ops while the dashboard is hidden", () => {
            cd.show([agent("main")]);
            cd.hide();
            expect(() => {
                cd.updateAgent(agent("main", { state: "paused" }));
                cd.removeAgent("main");
            }).not.toThrow();
        });

        it("onHeartbeat is skipped for an agent mid-removal", () => {
            cd.show([agent("main"), agent("catalog")]);
            cd.removeAgent("catalog"); // adds catalog to _removingIds for ~250ms
            expect(cd._removingIds.has("catalog")).toBe(true);
            expect(() => cd.onHeartbeat("catalog", Date.now())).not.toThrow();
        });

        it("_refreshTimestamps skips ids with no rendered card", () => {
            cd.show([agent("main")]);
            cd.lastHb.set("ghost", Date.now());
            expect(() => cd._refreshTimestamps()).not.toThrow();
        });
    });

    describe("Home Assistant nav link (external link, no embedded client)", () => {
        it("points the Devices nav link at the stored HA URL (new tab)", () => {
            localStorage.setItem("wactorz-ha-url", "http://ha.local:8123");
            const withHa = new CardDashboard() as any;
            const link = withHa.root.querySelector(".af-ha-nav-link");
            expect(link.getAttribute("href")).toBe("http://ha.local:8123");
            expect(link.target).toBe("_blank");
            expect(link.rel).toBe("noopener");
            withHa.destroy();
        });

        it("hides the Devices link when no HA URL is configured", () => {
            const withHa = new CardDashboard() as any;
            const link = withHa.root.querySelector(".af-ha-nav-link");
            expect(link.hasAttribute("href")).toBe(false);
            expect(link.style.display).toBe("none");
            withHa.destroy();
        });
    });
});
