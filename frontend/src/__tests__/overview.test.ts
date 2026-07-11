/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { OverviewView, type OverviewHost } from "../ui/dashboard/overview";
import type { AgentInfo } from "../types/agent";
import type { StatCardData, AgentAction } from "../ui/dashboard/cards";

function agent(name: string, over: Partial<AgentInfo> = {}): AgentInfo {
    return { id: name, name, state: "running", protected: false, ...over };
}

function makeHost(agents: AgentInfo[] = [agent("main-actor"), agent("worker")]): OverviewHost {
    const map = new Map(agents.map(a => [a.id, a]));
    const statData = (): StatCardData => ({
        agents: [...map.values()],
        totalMessages: 0,
        totalCostUsd: 0,
        feedCount: 0,
        costLimit: null,
    });
    return {
        root: document.createElement("div"),
        agents: map,
        lastHb: new Map(),
        remoteNodes: new Map(),
        removingIds: new Set(),
        hostStats: () => [10, 512, 1024],
        statData,
        onChat: vi.fn<(name: string) => void>(),
        onCommand: vi.fn<(id: string, action: AgentAction, btn: HTMLButtonElement) => void>(),
    };
}

function mount(host: OverviewHost): OverviewView {
    const view = new OverviewView(host);
    host.root.appendChild(view.build());
    document.body.appendChild(host.root);
    return view;
}

describe("OverviewView.build", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("renders host bar, stat cards, wactor cards and nodes", () => {
        const host = makeHost();
        mount(host);
        expect(host.root.querySelector("#af-host-bar")).not.toBeNull();
        expect(host.root.querySelectorAll("#af-stats-grid .af-stat-card").length).toBe(4);
        expect(host.root.querySelectorAll("#af-wactor-cards [data-id]").length).toBe(2);
        expect(host.root.querySelector("#af-node-list .af-node-name")!.textContent).toBe("local");
    });

    it("wires card Chat → host.onChat", () => {
        const host = makeHost([agent("worker")]);
        mount(host);
        host.root.querySelector<HTMLButtonElement>("#af-wactor-cards .af-chat-btn")!.click();
        expect(host.onChat).toHaveBeenCalledWith("worker");
    });
});

describe("OverviewView.renderCards", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("adds cards for new agents and removes cards for gone ones", () => {
        const host = makeHost([agent("worker")]);
        const view = mount(host);
        expect(host.root.querySelectorAll("#af-wactor-cards [data-id]").length).toBe(1);

        host.agents.set("extra", agent("extra"));
        view.renderCards();
        expect(host.root.querySelectorAll("#af-wactor-cards [data-id]").length).toBe(2);

        host.agents.delete("worker");
        view.renderCards();
        const ids = [...host.root.querySelectorAll<HTMLElement>("#af-wactor-cards [data-id]")].map(
            e => e.dataset["id"],
        );
        expect(ids).toEqual(["extra"]);
    });
});

describe("OverviewView.patchCard", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("updates an existing card's state in place", () => {
        const host = makeHost([agent("worker", { state: "running" })]);
        const view = mount(host);
        host.agents.set("worker", agent("worker", { state: "paused" }));
        view.patchCard(host.agents.get("worker")!);
        const card = host.root.querySelector<HTMLElement>('[data-id="worker"]')!;
        expect(card.querySelector(".af-card-state-label")!.textContent).toBe("paused");
    });

    it("skips agents currently being removed", () => {
        const host = makeHost([agent("worker")]);
        const view = mount(host);
        host.removingIds.add("worker");
        const before = host.root.querySelector('[data-id="worker"]')!.outerHTML;
        view.patchCard(agent("worker", { state: "stopped" }));
        expect(host.root.querySelector('[data-id="worker"]')!.outerHTML).toBe(before);
    });
});

describe("OverviewView.renderNodes", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("renders the local node plus remote nodes with online/offline pills", () => {
        const host = makeHost([agent("worker")]);
        host.remoteNodes.set("rpi", { agents: ["edge"], lastSeen: Date.now() });
        host.remoteNodes.set("old", { agents: [], lastSeen: Date.now() - 200_000 });
        mount(host);
        const list = host.root.querySelector<HTMLElement>("#af-node-list")!;
        expect(list.textContent).toContain("rpi");
        const pills = [...list.querySelectorAll(".af-node-pill")].map(p => p.textContent);
        expect(pills).toContain("online"); // local + rpi
        expect(pills).toContain("offline"); // stale "old"
    });

    it("keeps remote-runner agents out of the local node's agent list", () => {
        const host = makeHost([agent("main-actor"), agent("cpu-monitor", { node: "rpi-new" })]);
        host.remoteNodes.set("rpi-new", { agents: ["cpu-monitor"], lastSeen: Date.now() });
        mount(host);
        const list = host.root.querySelector<HTMLElement>("#af-node-list")!;
        const localMeta = list.querySelector(".af-node-meta")!.textContent!;
        expect(localMeta).toContain("main-actor");
        expect(localMeta).not.toContain("cpu-monitor");
    });

    // Node and agent names arrive from untrusted MQTT topics — they must never be
    // parsed as HTML (XSS). textContent rendering means the payload becomes inert text.
    it("does not inject HTML from a malicious remote-node name", () => {
        const host = makeHost([agent("worker")]);
        const payload = `<img src=x onerror="window.__pwned = 1">`;
        host.remoteNodes.set(payload, { agents: ["edge"], lastSeen: Date.now() });
        mount(host);
        const list = host.root.querySelector<HTMLElement>("#af-node-list")!;
        expect(list.querySelector("img")).toBeNull(); // not parsed into real DOM
        const names = [...list.querySelectorAll(".af-node-name")].map(n => n.textContent);
        expect(names).toContain(payload); // rendered verbatim as text
    });

    it("does not inject HTML from a malicious agent name in a remote node's list", () => {
        const host = makeHost([agent("worker")]);
        const payload = `<script>window.__pwned = 1</script>`;
        host.remoteNodes.set("rpi", { agents: [payload], lastSeen: Date.now() });
        mount(host);
        const list = host.root.querySelector<HTMLElement>("#af-node-list")!;
        expect(list.querySelector("script")).toBeNull();
        expect(list.textContent).toContain(payload);
    });

    it("does not inject HTML from a malicious local agent name", () => {
        const payload = `<img src=x onerror="window.__pwned = 1">`;
        const host = makeHost([agent(payload)]);
        mount(host);
        const list = host.root.querySelector<HTMLElement>("#af-node-list")!;
        expect(list.querySelector("img")).toBeNull();
        expect(list.querySelector(".af-node-meta")!.textContent).toContain(payload);
    });
});
