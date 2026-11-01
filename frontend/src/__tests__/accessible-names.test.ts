/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * What the dashboard tells assistive technology about the elements a person
 * acts on, and which the end-to-end suite finds the same way: by role and name.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

import { buildWactorCard, type WactorCardCallbacks } from "../ui/overview/cards";
import { buildNodeCard } from "../ui/overview/nodeCard";
import { buildHeader, buildBottomNav, releaseBottomNav, releaseHeaderPopovers } from "../ui/shell/header";
import { buildChatPane, buildChatSidebar } from "../ui/chat/chatLayout";
import { confirmDialog } from "../ui/shell/confirmDialog";
import type { AgentInfo } from "../types/agent";
import { allByRole, allByTestId, byRole, byTestId, nameOf } from "./queries";

function agent(name: string, over: Partial<AgentInfo> = {}): AgentInfo {
    return { id: name, name, state: "running", protected: false, ...over };
}

const cb = (): WactorCardCallbacks => ({ onChat: vi.fn(), onCommand: vi.fn(), onHistory: vi.fn() });

beforeEach(() => {
    releaseHeaderPopovers();
    releaseBottomNav();
    document.body.replaceChildren();
});

describe("an agent's card", () => {
    it("is an item named by its agent", () => {
        const card = buildWactorCard(agent("weather"), 0, cb());

        expect(card.getAttribute("role")).toBe("listitem");
        expect(nameOf(card)).toBe("weather");
    });

    it("names each button after its agent, starting with the word it shows", () => {
        const card = buildWactorCard(agent("weather"), 0, cb());

        const names = allByRole(card, "button").map(nameOf);

        expect(names).toEqual(["Chat with weather", "History of weather", "Stop weather", "Delete weather"]);
        for (const btn of allByRole(card, "button")) {
            expect(nameOf(btn).startsWith(btn.textContent!)).toBe(true);
        }
    });

    it("marks its state for the tests that read it", () => {
        const card = buildWactorCard(agent("weather", { state: "stopped" }), 0, cb());

        expect(byTestId(card, "agent-state").textContent).toBe("stopped");
    });
});

describe("a node's card", () => {
    it("is an item named by its node, with its agents and History named after it", () => {
        const card = buildNodeCard(
            { name: "rpi", online: true, agents: ["flic"], lastSeen: Date.now() },
            vi.fn(),
        );

        expect([card.getAttribute("role"), nameOf(card), card.dataset["node"]]).toEqual([
            "listitem",
            "rpi",
            "rpi",
        ]);
        expect(byTestId(card, "node-agents").textContent).toBe("flic");
        expect(byRole(card, "button", "History of rpi").textContent).toBe("History");
    });
});

describe("the header", () => {
    const header = () =>
        buildHeader({ view: "chat", connState: "live", onSetView: vi.fn(), haUrl: null, extraViews: [] });

    it("puts the view buttons in a navigation landmark, the open one marked current", () => {
        const nav = byRole(header(), "navigation", "Views");

        expect(allByRole(nav, "button").map(nameOf)).toEqual([
            "Overview",
            "Feed",
            "Chat",
            "Models",
            "Inbox",
            "Settings",
        ]);
        expect(byRole(nav, "button", "Chat").getAttribute("aria-current")).toBe("page");
    });

    it("reads out the connection badge when it changes", () => {
        const badge = byTestId(header(), "connection");

        expect(badge.getAttribute("role")).toBe("status");
        expect(badge.getAttribute("aria-live")).toBe("polite");
    });

    it("names the phone's navigation the same way", () => {
        const nav = buildBottomNav({ view: "overview", onSetView: vi.fn(), haUrl: null, extraViews: [] });

        expect(nav.getAttribute("aria-label")).toBe("Views");
        expect(byRole(nav, "button", "More").getAttribute("aria-expanded")).toBe("false");
    });

    it("names each popover after the button that opens it", () => {
        document.body.appendChild(header());

        const opener = byRole(document.body, "button", "Clear stored state");
        const panel = byRole(document.body, "group", "Clear stored state");

        expect(opener.getAttribute("aria-controls")).toBe(panel.id);
    });
});

describe("the chat", () => {
    it("is a conversation log beside a named list of agents", () => {
        const pane = buildChatPane();
        const sidebar = buildChatSidebar("", vi.fn());

        expect(byRole(pane, "log", "Conversation").getAttribute("aria-live")).toBe("polite");
        expect(byRole(sidebar, "group", "Agents to chat with")).toBe(byTestId(sidebar, "chat-agent-list"));
    });
});

describe("a toast", () => {
    // A fresh manager each time: the body is emptied before every test, and the
    // manager's container with it.
    async function freshToast() {
        vi.resetModules();
        return (await import("../ui/ToastManager")).toast;
    }

    it("that asks is a group named after who asks, its answers inside it", async () => {
        (await freshToast()).show({
            title: "installer",
            message: "Install numpy?",
            actions: [{ label: "Yes", onClick: vi.fn() }],
        });

        const asking = byRole(document.body, "group", "installer");

        expect(asking.dataset["testid"]).toBe("toast");
        expect(byRole(asking, "button", "Yes")).toBeTruthy();
        expect(byTestId(asking, "toast-message").textContent).toBe("Install numpy?");
    });

    it("that only tells is no group, and shows its title as text", async () => {
        (await freshToast()).show({ title: "<b>weather</b>", message: "started" });

        const told = allByTestId(document.body, "toast").at(-1)!;

        expect(told.hasAttribute("role")).toBe(false);
        expect(byTestId(told, "toast-title").textContent).toBe("<b>weather</b>");
        expect(told.querySelector("b")).toBeNull();
    });
});

describe("the confirm dialog", () => {
    it("is named by its question and described by its consequence", async () => {
        const answer = confirmDialog({
            title: "Delete weather?",
            message: "It cannot be undone.",
            confirmLabel: "Delete",
        });
        const box = byRole(document.body, "dialog", "Delete weather?");

        const described = document.getElementById(box.getAttribute("aria-describedby")!);

        expect(described).toBe(byTestId(box, "confirm-message"));
        expect(described?.textContent).toBe("It cannot be undone.");
        byTestId(box, "confirm-cancel").click();
        await expect(answer).resolves.toBe(false);
    });
});
