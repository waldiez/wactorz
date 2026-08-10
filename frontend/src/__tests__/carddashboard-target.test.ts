/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import { toast } from "../ui/ToastManager";
import { CardDashboard } from "../ui/CardDashboard";
import type { AgentInfo } from "../types/agent";

// The chat-target <select> must never render blank. If the stored target
// matches no option — main absent, or an agent that itself is named "main" —
// `select.value` selects nothing and the control comes up empty, so
// _populateSelect re-syncs to a messageable agent and falls back to the first
// option.

function agent(name: string, protectedFlag = false): AgentInfo {
    return { id: name, name, state: "running", protected: protectedFlag };
}

describe("CardDashboard chat-target selection", () => {
    let cd: any;

    beforeEach(() => {
        document.body.innerHTML = "";
        cd = new CardDashboard() as any;
        cd.agents.clear();
    });

    it("falls back to 'main' when chatTarget default ('main') is absent", () => {
        cd.agents.set("1", agent("main", true)); // protected, but always messageable
        cd.agents.set("2", agent("io-agent", true)); // system → not messageable
        cd._chat.chatTarget = "main";

        const select = document.createElement("select");
        cd._chat._populateSelect(select);

        expect(select.value).toBe("main"); // never blank
        expect(cd._chat.chatTarget).toBe("main");
        // the system agent is not an option
        expect([...select.options].map(o => o.value)).not.toContain("io-agent");
    });

    it("falls back to the first messageable agent when no main exists", () => {
        cd.agents.set("1", agent("catalog"));
        cd.agents.set("2", agent("monitor-agent", true)); // system → excluded
        cd._chat.chatTarget = "main";

        const select = document.createElement("select");
        cd._chat._populateSelect(select);

        // The *select* falls back so the control shows something real. The
        // target is the user's choice and drawing a dropdown must not change it
        // — that is what let a reset hand the conversation to another agent.
        expect(select.value).toBe("catalog");
        expect(cd._chat.chatTarget).toBe("main");
    });

    it("keeps a user-picked chatTarget unchanged", () => {
        cd.agents.set("1", agent("main"));
        cd.agents.set("2", agent("home-assistant-agent"));
        cd._chat.setTarget("home-assistant-agent"); // explicit user pick → sticky

        const select = document.createElement("select");
        cd._chat._populateSelect(select);

        expect(select.value).toBe("home-assistant-agent");
    });
});

describe("a reset does not hand the conversation to another agent", () => {
    let cd: any;

    beforeEach(() => {
        document.body.innerHTML = "";
        localStorage.clear();
        cd = new CardDashboard() as any;
        cd.show([agent("main"), agent("catalog")]);
        cd._setView("chat");
        cd._chat._selectAgent("catalog");
    });

    afterEach(() => {
        try {
            cd.destroy();
        } catch {
            /* ignore */
        }
    });

    const paneTitle = () => cd.root.querySelector(".af-chat-pane-title")?.textContent;

    it("keeps the chosen agent while the list churns", () => {
        // A reset removes agents one at a time. Once catalog is gone but main
        // remains, the old code fell through to main and never came back.
        cd.removeAgent("catalog");

        expect(cd._chat.chatTarget).toBe("catalog");
    });

    it("keeps every part of the screen naming the same agent", () => {
        // The screenshot that started this: sidebar, select and placeholder said
        // main while the header and thread said catalog — and the message would
        // have gone to main.
        cd.removeAgent("catalog");

        expect(paneTitle()).toBe(`@${cd._chat.chatTarget}`);
        expect(cd.root.querySelector("#af-iobar-input")?.placeholder).toContain(cd._chat.chatTarget);
    });

    it("still shows the choice after the agents come back", () => {
        cd.removeAgent("catalog");
        cd.addAgent(agent("catalog"));

        expect(cd._chat.chatTarget).toBe("catalog");
        expect(paneTitle()).toBe("@catalog");
    });
});

describe("an agent that does not survive a reset", () => {
    let cd: any;

    beforeEach(() => {
        document.body.innerHTML = "";
        localStorage.clear();
        vi.mocked(toast.show).mockClear();
        cd = new CardDashboard() as any;
        cd.show([agent("main"), agent("weather-agent")]);
        cd._setView("chat");
        cd._chat._selectAgent("weather-agent");
    });

    afterEach(() => {
        try {
            cd.destroy();
        } catch {
            /* ignore */
        }
    });

    /** A reset: the survivors replace the list, then the settled signal fires. */
    const resetTo = (names: string[]) => {
        cd.agents.clear();
        names.forEach(n => cd.agents.set(n, agent(n)));
        cd._chat.dropTargetIfResetRemovedIt();
    };

    it("moves to main, because a spawned agent is destroyed by the reset", () => {
        resetTo(["main", "catalog"]);

        expect(cd._chat.chatTarget).toBe("main");
        expect(cd.root.querySelector(".af-chat-pane-title")?.textContent).toBe("@main");
    });

    it("says so rather than moving silently", () => {
        resetTo(["main", "catalog"]);

        expect(toast.show).toHaveBeenCalledTimes(1);
        const msg = vi.mocked(toast.show).mock.calls[0]![0].message;
        expect(msg).toContain("weather-agent");
        expect(msg).toContain("main");
    });

    it("leaves an agent that did survive alone, and says nothing", () => {
        // System agents come back; the choice must not be disturbed.
        resetTo(["main", "weather-agent"]);

        expect(cd._chat.chatTarget).toBe("weather-agent");
        expect(toast.show).not.toHaveBeenCalled();
    });

    it("does nothing when the list is momentarily empty", () => {
        // Mid-churn, "no agents" means "not back yet", never "yours is gone".
        resetTo([]);

        expect(cd._chat.chatTarget).toBe("weather-agent");
        expect(toast.show).not.toHaveBeenCalled();
    });
});
