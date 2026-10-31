/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { emit } from "../events";
import type { CardDashboard } from "../ui/CardDashboard";
import { buildAudioPopover, buildResetPopover } from "../ui/dashboard/popovers";
import { agent, buildDashboard, cardOf, goTo, openDashboard, pressView, shownView } from "./dashboard";
import { allByRole, byRole, byTestId } from "./queries";

// What the dashboard puts on screen: its root, each view, the cards. Checked
// by role, name and test id, so the views can be reorganised freely as long as
// what a person finds on them stays.

let dashboard: CardDashboard;

beforeEach(() => {
    document.body.replaceChildren();
    localStorage.clear();
    history.replaceState(null, "", "#");
});

afterEach(() => {
    dashboard?.destroy();
});

describe("CardDashboard render", () => {
    it("builds its root into the page, hidden until shown", () => {
        ({ dashboard } = buildDashboard());
        const root = document.getElementById("card-dashboard")!;
        expect(root).toBeTruthy();
        expect(root.classList.contains("cd-visible")).toBe(false);
        dashboard.show();
        expect(root.classList.contains("cd-visible")).toBe(true);
    });

    it("draws a card for each agent the store holds when shown", () => {
        ({ dashboard } = openDashboard([agent("main"), agent("catalog")]));
        expect(allByRole(byRole(document, "list", "Agents"), "listitem")).toHaveLength(2);
        expect(cardOf("catalog").textContent).toContain("catalog");
    });

    it.each([
        ["overview", () => byRole(document, "list", "Agents")],
        ["chat", () => byRole(document, "log", "Conversation")],
        ["feed", () => byTestId(document, "view-feed")],
        ["settings", () => byTestId(document, "view-settings")],
    ])("renders the %s view", (view, landmark) => {
        ({ dashboard } = openDashboard([agent("main")]));
        goTo(view);
        expect(shownView()).toBe(view);
        expect(landmark()).toBeTruthy();
    });

    it("the chat view has the agents to talk to beside the conversation", () => {
        ({ dashboard } = openDashboard([agent("main")]));
        goTo("chat");
        expect(byTestId(document, "chat-agent-list")).toBeTruthy();
        expect(byRole(document, "log", "Conversation")).toBeTruthy();
    });

    it("renders an agent's message as markdown", () => {
        ({ dashboard } = openDashboard([agent("main")]));
        goTo("chat", "main");
        emit("af-chat-message", {
            msg: {
                id: "m1",
                from: "main",
                to: "user",
                threadId: "main",
                content: "**bold**",
                timestampMs: 1_700_000_000_000,
            },
        });
        expect(byRole(document, "log", "Conversation").querySelector("strong")?.textContent).toBe("bold");
    });

    it("builds the audio and reset popovers without throwing", () => {
        const audio = buildAudioPopover();
        expect(audio.classList.contains("af-audio-popover")).toBe(true);
        expect(audio.querySelector(".af-audio-tracks")).toBeTruthy();
        expect(() => buildResetPopover()).not.toThrow();
    });

    it("takes the totals and the host's readings", () => {
        ({ dashboard } = openDashboard([agent("main")]));
        dashboard.setHostStats(42, 1024, 4096);
        dashboard.setTotalCostUsd(1.23);
        dashboard.setTotalMessages(99);
        dashboard.renderView();
        expect(document.body.textContent).toContain("$1.2300");
        expect(document.body.textContent).toContain("99");
    });

    it("renderView rebuilds the view that is open", () => {
        ({ dashboard } = openDashboard([agent("A")]));
        byTestId(document, "view-overview").remove();
        dashboard.renderView();
        expect(shownView()).toBe("overview");
    });

    it("registerView adds a nav button that opens the view", () => {
        ({ dashboard } = openDashboard([agent("A")]));
        const content = document.createElement("div");
        content.textContent = "custom-view";
        dashboard.registerView("custom", "hexagon", "Custom", () => content);
        pressView("Custom");
        expect(document.body.textContent).toContain("custom-view");
    });
});
