/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach } from "vitest";
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

        expect(select.value).toBe("catalog");
        expect(cd._chat.chatTarget).toBe("catalog");
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
