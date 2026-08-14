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

describe("choosing a different agent", () => {
    // Every way of choosing must move the whole screen, not just the part the
    // caller happened to remember. `setTarget` used to set the field and stop,
    // so the dropdown and the @mention panel updated the composer placeholder
    // inline and left the pane header and thread on the previous conversation.
    let cd: any;

    beforeEach(() => {
        document.body.innerHTML = "";
        localStorage.clear();
        cd = new CardDashboard() as any;
        cd.show([agent("main"), agent("catalog")]);
        cd._setView("chat");
        cd._chat._selectAgent("main");
    });

    afterEach(() => {
        try {
            cd.destroy();
        } catch {
            /* ignore */
        }
    });

    const paneTitle = () => cd.root.querySelector(".af-chat-pane-title")?.textContent;

    it("switches the visible conversation, from the dropdown", () => {
        const select = cd.root.querySelector("#af-target-select") as HTMLSelectElement;
        select.value = "catalog";
        select.dispatchEvent(new Event("change"));

        expect(cd._chat.chatTarget).toBe("catalog");
        expect(paneTitle()).toBe("@catalog");
    });

    it("sends to the chosen agent, not to whatever the dropdown shows", () => {
        // The dropdown's value is set programmatically whenever the options are
        // redrawn, and it falls back to the first option when the choice is not
        // among them. Reading the send target from there made drawing a control
        // decide where a message went — the same conflation, one layer down.
        cd._chat._selectAgent("catalog");
        const select = cd.root.querySelector("#af-target-select") as HTMLSelectElement;
        select.value = "main"; // programmatic: no change event, so no choice was made
        const input = cd.root.querySelector("#af-iobar-input") as HTMLTextAreaElement;
        input.value = "hello";

        const sent = vi.fn();
        document.addEventListener("af-send-message", sent);
        cd._chat._sendMessage(input, select);
        document.removeEventListener("af-send-message", sent);

        expect((sent.mock.calls[0]![0] as CustomEvent).detail.target).toBe("catalog");
    });

    it("switches the visible conversation, from any caller of setTarget", () => {
        // The @mention panel and the wactor-card Chat button go through here too.
        cd._chat.setTarget("catalog");

        expect(paneTitle()).toBe("@catalog");
        expect(cd.root.querySelector("#af-iobar-input")?.placeholder).toContain("catalog");
    });
});

describe("sending to an agent that cannot answer", () => {
    // The choice survives a stop — that is the point of holding it separately
    // from reachability. The send is what has to be honest about it, and saying
    // so beats the old behaviour of quietly routing to main.
    let cd: any;

    beforeEach(() => {
        document.body.innerHTML = "";
        localStorage.clear();
        vi.mocked(toast.show).mockClear();
        cd = new CardDashboard() as any;
        cd.show([agent("main"), agent("worker")]);
        cd._setView("chat");
        cd._chat._selectAgent("worker");
    });

    afterEach(() => {
        try {
            cd.destroy();
        } catch {
            /* ignore */
        }
    });

    /** Replace `worker` with a version in `state`, as a status frame would. */
    const workerBecomes = (state: AgentInfo["state"]) => {
        cd.agents.set("worker", { ...agent("worker"), state });
    };

    const send = (text: string) => {
        const input = cd.root.querySelector("#af-iobar-input") as HTMLTextAreaElement;
        const select = cd.root.querySelector("#af-target-select") as HTMLSelectElement;
        input.value = text;
        const sent = vi.fn();
        document.addEventListener("af-send-message", sent);
        cd._chat._sendMessage(input, select);
        document.removeEventListener("af-send-message", sent);
        return { sent, input };
    };

    it("blocks the send and says why", () => {
        workerBecomes("stopped");

        const { sent } = send("are you there");

        expect(sent).not.toHaveBeenCalled();
        expect(toast.show).toHaveBeenCalledTimes(1);
        expect(vi.mocked(toast.show).mock.calls[0]![0].message).toContain("worker");
    });

    it("keeps what the user typed, so it can be sent once the agent is back", () => {
        workerBecomes("stopped");

        const { input } = send("are you there");

        expect(input.value).toBe("are you there");
    });

    it("keeps the choice, and just works when the agent restarts", () => {
        // The respawn proof: a stop is not a deletion, so nothing moves.
        workerBecomes("stopped");
        send("are you there");
        expect(cd._chat.chatTarget).toBe("worker");

        workerBecomes("running");
        const { sent } = send("are you there");

        expect((sent.mock.calls[0]![0] as CustomEvent).detail.target).toBe("worker");
    });

    it("blocks a failed agent too", () => {
        workerBecomes({ failed: "boom" });

        const { sent } = send("hello");

        expect(sent).not.toHaveBeenCalled();
    });

    it("lets a paused or initializing agent through", () => {
        // Both are transient. A false block here would be worse than the bug.
        workerBecomes("initializing");
        expect(send("hello").sent).toHaveBeenCalledTimes(1);

        workerBecomes("paused");
        expect(send("hello").sent).toHaveBeenCalledTimes(1);
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
        cd._chat.dropTargetIfGone("reset");
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

describe("the target once something has been chosen, by the user or for them", () => {
    // `_userPicked` was a half-built version of the choice/reachability split: a
    // single flag standing in for "has a choice been made". A fallback chosen
    // *for* the user is still a choice — they were told about it — so nothing may
    // silently move them off it afterwards.
    let cd: any;

    beforeEach(() => {
        document.body.innerHTML = "";
        localStorage.clear();
        vi.mocked(toast.show).mockClear();
        cd = new CardDashboard() as any;
    });

    afterEach(() => {
        try {
            cd.destroy();
        } catch {
            /* ignore */
        }
    });

    it("opens on main when there is one", () => {
        cd.show([agent("main"), agent("catalog")]);
        cd._setView("chat");

        expect(cd._chat.chatTarget).toBe("main");
    });

    it("opens on the only agent there is when main is absent, and keeps it", () => {
        // First resolution sticks. Main arriving later does not displace a
        // target the user has already seen named on screen.
        cd.show([agent("catalog")]);
        cd._setView("chat");
        expect(cd._chat.chatTarget).toBe("catalog");

        cd.addAgent(agent("main"));
        cd._setView("overview");
        cd._setView("chat");

        expect(cd._chat.chatTarget).toBe("catalog");
    });

    it("does not move you again once a fallback has been chosen for you", () => {
        // No main: pick weather-agent, lose it to a reset, get moved to catalog
        // and told so. Main registering afterwards must not quietly take over —
        // that is the snap-back, arriving through the one path still open to it.
        cd.show([agent("weather-agent"), agent("catalog")]);
        cd._setView("chat");
        cd._chat._selectAgent("weather-agent");

        cd.agents.delete("weather-agent");
        cd._chat.dropTargetIfGone("reset");
        expect(cd._chat.chatTarget).toBe("catalog");

        cd.addAgent(agent("main"));
        cd._setView("overview");
        cd._setView("chat");

        expect(cd._chat.chatTarget).toBe("catalog");
    });
});

describe("deleting the agent you are chatting with", () => {
    // A deletion is more clear-cut than a reset — no churn, no ambiguity — but
    // it had none of the handling, because the fallback was wired to the reset
    // signal alone. The rule is the same: the target left the list for good, so
    // move, rather than leaving the user parked on a conversation that cannot
    // resume. (A *stopped* agent is the other branch: it stays, and the send
    // blocks — see "sending to an agent that cannot answer".)
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

    /** What the `delete_agent` frame drives: remove, then judge. */
    const deleteAgent = (name: string) => {
        cd.removeAgent(name);
        cd._chat.dropTargetIfGone("deleted");
    };

    it("moves the conversation off the deleted agent", () => {
        deleteAgent("weather-agent");

        expect(cd._chat.chatTarget).toBe("main");
        expect(cd.root.querySelector(".af-chat-pane-title")?.textContent).toBe("@main");
    });

    it("says it was deleted, not that it failed to survive a reset", () => {
        deleteAgent("weather-agent");

        const msg = vi.mocked(toast.show).mock.calls[0]![0].message;
        expect(msg).toContain("weather-agent");
        expect(msg).toContain("deleted");
        expect(msg).not.toContain("reset");
    });

    it("moves silently when the chat view is not open", () => {
        // The toast explains a change the user can see. On another view there is
        // nothing on screen to explain, and the chat will simply be correct when
        // they next open it — but the move itself still has to happen.
        cd._setView("overview");

        deleteAgent("weather-agent");

        expect(cd._chat.chatTarget).toBe("main");
        expect(toast.show).not.toHaveBeenCalled();
    });

    it("leaves the conversation alone when a different agent is deleted", () => {
        cd.agents.set("other", agent("other"));
        deleteAgent("other");

        expect(cd._chat.chatTarget).toBe("weather-agent");
        expect(toast.show).not.toHaveBeenCalled();
    });
});

describe("the composer names the target on a fresh tab", () => {
    // Through the whole dashboard, not DashboardChat alone: the bug this covers
    // lived in the seam between them. The composer is built in the constructor,
    // before any chat view exists and so before there is a target, and only the
    // chat view's mount can tell it what that target turned out to be.
    beforeEach(() => {
        document.body.innerHTML = "";
        localStorage.clear();
    });

    it("names it when the agents arrive over the socket, on the overview", () => {
        // The reported path, and the one every earlier attempt missed: agents
        // land through addAgent, not a view render. That already refreshed the
        // composer — with the empty target nothing had resolved yet, so it
        // rewrote "Message…" and stayed there until chat was opened by hand.
        localStorage.setItem("wactorz-chat-target", "home-assistant-agent");
        const cd = new CardDashboard() as any;
        cd.agents.clear();
        cd.show([]); // fresh tab: visible, on the overview, nobody here yet

        cd.addAgent(agent("main"));
        cd.addAgent(agent("home-assistant-agent"));

        expect(cd.view).not.toBe("chat");
        expect(_placeholder(cd)).toBe("Message @home-assistant-agent…");
    });

    it("names it on the overview, where chat was never opened", () => {
        // Where this was caught. The composer is outside the view body, so it
        // is on screen from the first paint — and nothing on the overview
        // builds a chat view, so a fix that runs on that mount never fires.
        localStorage.setItem("wactorz-chat-target", "home-assistant-agent");
        const cd = new CardDashboard() as any;
        cd.agents.clear();
        cd.agents.set("1", agent("main"));
        cd.agents.set("2", agent("home-assistant-agent"));

        cd._renderView(); // still on the overview

        expect(cd.view).not.toBe("chat");
        expect(_placeholder(cd)).toBe("Message @home-assistant-agent…");
    });

    it("names the remembered agent when the list is already loaded", () => {
        localStorage.setItem("wactorz-chat-target", "home-assistant-agent");
        const cd = new CardDashboard() as any;
        cd.agents.clear();
        cd.agents.set("1", agent("main"));
        cd.agents.set("2", agent("home-assistant-agent"));

        cd._setView("chat");

        expect(_placeholder(cd)).toBe("Message @home-assistant-agent…");
    });

    it("names it even when the agents arrive after the view is open", () => {
        // The fresh-tab order: the socket has not delivered anyone yet when the
        // user lands on chat, so the first resolve has nothing to work with.
        localStorage.setItem("wactorz-chat-target", "home-assistant-agent");
        const cd = new CardDashboard() as any;
        cd.agents.clear();

        cd._setView("chat");
        cd.agents.set("1", agent("main"));
        cd.agents.set("2", agent("home-assistant-agent"));
        cd._renderView();

        expect(_placeholder(cd)).toBe("Message @home-assistant-agent…");
    });
    it("remembers an agent reached by @mention, not the one left behind", () => {
        // Sending is a choice: `@catalog …` routes the message there and the
        // view follows, so reopening on main contradicts what was last shown.
        const cd = new CardDashboard() as any;
        cd.agents.clear();
        cd.show([agent("main"), agent("catalog")]);
        cd._setView("chat");
        const input = cd.root.querySelector("#af-iobar-input") as HTMLTextAreaElement;
        input.value = "@catalog spawn weather-agent";

        cd._chat._sendMessage(input);

        expect(cd._chat.chatTarget).toBe("catalog");
        expect(localStorage.getItem("wactorz-chat-target")).toBe("catalog");
    });

    it("does not move the conversation under someone already reading it", () => {
        // The cost of the load-window exception, kept in bounds: once the chat
        // view has shown a real target, a remembered agent registering late is
        // just another agent. Moving the conversation then is the bug the
        // target split removed.
        localStorage.setItem("wactorz-chat-target", "home-assistant-agent");
        const cd = new CardDashboard() as any;
        cd.agents.clear();
        cd.show([agent("main")]);
        cd._setView("chat"); // opens on main, and they are looking at it

        cd.addAgent(agent("home-assistant-agent")); // registers late

        expect(cd._chat.chatTarget).toBe("main");
        expect(_placeholder(cd)).toBe("Message @main…");
    });
});

function _placeholder(cd: any): string {
    const input = cd.root.querySelector("#af-iobar-input") as HTMLTextAreaElement;
    return input.placeholder;
}
