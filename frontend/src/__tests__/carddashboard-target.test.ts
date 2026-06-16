import { describe, it, expect, beforeEach } from "vitest";
import { CardDashboard } from "../ui/CardDashboard";
import type { AgentInfo } from "../types/agent";

// Regression tests for the chat-target <select> never rendering blank.
// The bug: chatTarget defaulted to "main-actor" but if the live agent is named
// "main" (or main-actor is absent), select.value matched no option and the
// control rendered with nothing selected. _populateSelect now re-syncs the
// target to a messageable agent and falls back to the first option.

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

  it("falls back to 'main' when chatTarget default ('main-actor') is absent", () => {
    cd.agents.set("1", agent("main", true)); // protected, but always messageable
    cd.agents.set("2", agent("io-agent", true)); // system → not messageable
    cd.chatTarget = "main-actor";

    const select = document.createElement("select");
    cd._populateSelect(select);

    expect(select.value).toBe("main"); // never blank
    expect(cd.chatTarget).toBe("main");
    // the system agent is not an option
    expect([...select.options].map((o) => o.value)).not.toContain("io-agent");
  });

  it("falls back to the first messageable agent when no main exists", () => {
    cd.agents.set("1", agent("catalog"));
    cd.agents.set("2", agent("monitor-agent", true)); // system → excluded
    cd.chatTarget = "main-actor";

    const select = document.createElement("select");
    cd._populateSelect(select);

    expect(select.value).toBe("catalog");
    expect(cd.chatTarget).toBe("catalog");
  });

  it("keeps a valid existing chatTarget unchanged", () => {
    cd.agents.set("1", agent("main"));
    cd.agents.set("2", agent("home-assistant-agent"));
    cd.chatTarget = "home-assistant-agent";

    const select = document.createElement("select");
    cd._populateSelect(select);

    expect(select.value).toBe("home-assistant-agent");
  });
});
