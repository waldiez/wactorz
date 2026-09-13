/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { CardDashboard } from "../ui/CardDashboard";
import { buildAudioPopover, buildResetPopover } from "../ui/dashboard/popovers";
import type { AgentInfo, ChatMessage } from "../types/agent";

// Characterization tests: they pin CardDashboard's observable render behaviour
// (view containers, card structure, builders) so the upcoming module split can
// be proven behaviour-preserving. They assert on stable structural anchors
// rather than exact HTML.

function agent(name: string, over: Partial<AgentInfo> = {}): AgentInfo {
    return { id: name, name, state: "running", protected: false, ...over };
}

function chatMsg(over: Partial<ChatMessage> = {}): ChatMessage {
    return { id: "m1", from: "main", to: "user", content: "**hi**", timestampMs: 1_700_000_000_000, ...over };
}

describe("CardDashboard render", () => {
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
    });

    it("builds and mounts the root", () => {
        expect(cd.root).toBeTruthy();
        expect(cd.root.id).toBe("card-dashboard");
        expect(cd.root.classList.contains("cd-root")).toBe(true);
        expect(document.getElementById("card-dashboard")).toBe(cd.root);
    });

    it("show() makes the dashboard visible and renders agent cards", () => {
        cd.show([agent("main"), agent("catalog")]);
        expect(cd.root.classList.contains("cd-visible")).toBe(true);
        const cards = cd.root.querySelectorAll(".af-card");
        expect(cards.length).toBe(2);
        expect(cd.root.textContent).toContain("catalog");
    });

    it.each([
        ["overview", ".af-overview"],
        ["chat", ".af-chat-thread"],
        ["feed", ".af-feed"],
        ["settings", ".af-settings"],
    ])("renders the %s view", (view, selector) => {
        cd.show([agent("main")]);
        cd._setView(view);
        expect(cd.view).toBe(view);
        expect(cd.root.querySelector(selector)).toBeTruthy();
    });

    it("overview builds a wactor card with the agent name", () => {
        const card = cd._overview._buildCard(agent("alpha"));
        expect(card.classList.contains("af-card")).toBe(true);
        expect(card.querySelector(".af-card-name")?.textContent).toContain("alpha");
    });

    it("overview build includes the host bar and stats grid", () => {
        cd.show([agent("main")]);
        const ov = cd._overview.build();
        expect(ov.querySelector(".af-host-bar")).toBeTruthy();
        expect(ov.querySelector(".af-stats-grid")).toBeTruthy();
    });

    it("_buildChatView includes a thread and a sidebar", () => {
        cd.show([agent("main")]);
        const chat = cd._chat.buildChatView();
        expect(chat.querySelector(".af-chat-thread")).toBeTruthy();
        expect(chat.querySelector(".af-chat-sidebar")).toBeTruthy();
    });

    it("_appendChatMsgEl renders a markdown bubble", () => {
        const container = document.createElement("div");
        cd._chat._appendChatMsgEl(chatMsg({ content: "**bold**" }), container);
        const bubble = container.querySelector(".af-chat-msg-bubble");
        expect(bubble).toBeTruthy();
        expect(bubble!.innerHTML).toContain("<strong>");
    });

    it("builds the audio and reset popovers without throwing", () => {
        const audio = buildAudioPopover();
        expect(audio.classList.contains("af-audio-popover")).toBe(true);
        expect(audio.querySelector(".af-audio-tracks")).toBeTruthy();
        expect(() => buildResetPopover()).not.toThrow();
    });

    it("builds the settings view with the cost-limit section", () => {
        const settings = cd._buildSettingsView();
        expect(settings.classList.contains("af-settings")).toBe(true);
        // Only the spend-limit section remains (HA config was removed).
        expect(settings.querySelectorAll(".af-settings-section").length).toBe(1);
    });

    it("addAgent / updateAgent / removeAgent keep the agents map in sync", () => {
        cd.show([agent("main")]);
        cd.addAgent(agent("beta"));
        expect(cd.agents.has("beta")).toBe(true);
        cd.updateAgent(agent("beta", { state: "stopped" }));
        expect(cd.agents.get("beta").state).toBe("stopped");
        cd.removeAgent("beta");
        expect(cd.agents.has("beta")).toBe(false);
    });

    it("setHostStats / setTotalCostUsd / setTotalMessages do not throw", () => {
        cd.show([agent("main")]);
        expect(() => {
            cd.setHostStats(42, 1024, 4096);
            cd.setTotalCostUsd(1.23);
            cd.setTotalMessages(99);
        }).not.toThrow();
    });

    it("onChat / showAlert / onHeartbeat do not throw", () => {
        cd.show([agent("main")]);
        expect(() => {
            cd.onChat("main", "user");
            cd.showAlert("main", "error");
            cd.onHeartbeat("main", Date.now());
        }).not.toThrow();
    });

    it("renderView rebuilds the active view body", () => {
        cd.show([agent("A")]);
        const body = cd.root.querySelector(".af-body")!;
        body.innerHTML = "";
        cd.renderView();
        // Body was cleared and rebuilt — should contain overview content.
        expect(body.children.length).toBeGreaterThan(0);
    });

    it("registerView adds a nav button and view builder", () => {
        cd.show([agent("A")]);
        const el = document.createElement("div");
        el.textContent = "custom-view";
        cd.registerView("custom", "hexagon", "Custom", () => el);
        expect(cd.root.querySelector('[data-view="custom"]')).not.toBeNull();
        // Switch to the registered view — builder is called.
        cd.view = "custom";
        cd.renderView();
        expect(cd.root.textContent).toContain("custom-view");
    });
});
