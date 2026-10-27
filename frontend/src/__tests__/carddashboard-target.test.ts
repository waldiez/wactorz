/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import { toast } from "../ui/ToastManager";
import type { AgentStore } from "../agents/AgentStore";
import type { CardDashboard } from "../ui/CardDashboard";
import { emit } from "../events";
import type { AgentInfo } from "../types/agent";
import { addressee, agent, composer, goTo, openDashboard, settled, shownView } from "./dashboard";
import { byRole, byTestId } from "./queries";

// Who the chat talks to: the person's choice, kept through churn, moved only
// when the agent is really gone, and named the same everywhere on screen.

let store: AgentStore;
let cd: CardDashboard;

function open(agents: AgentInfo[]): void {
    ({ store, dashboard: cd } = openDashboard(agents));
}

/** The chat's target dropdown. */
const dropdown = (): HTMLSelectElement =>
    byRole(document, "combobox", "Chat target agent") as HTMLSelectElement;

/** Pick `name` in the dropdown, as a person does. */
function pick(name: string): void {
    dropdown().value = name;
    dropdown().dispatchEvent(new Event("change"));
}

/** Open `name`'s conversation from the chat's agent list. */
function select(name: string): void {
    byRole(byTestId(document, "chat-agent-list"), "button", name).click();
}

const paneTitle = (): string => byTestId(document, "chat-title").textContent ?? "";

/** Type `text` and press Send; the send events that went out. */
function send(text: string): ReturnType<typeof vi.fn> {
    composer().value = text;
    const sent = vi.fn();
    document.addEventListener("af-send-message", sent);
    byRole(document, "button", "Send message").click();
    document.removeEventListener("af-send-message", sent);
    return sent;
}

const sentTo = (sent: ReturnType<typeof vi.fn>): string =>
    (sent.mock.calls[0]![0] as CustomEvent<{ target: string }>).detail.target;

beforeEach(() => {
    document.body.replaceChildren();
    localStorage.clear();
    history.replaceState(null, "", "#");
    vi.mocked(toast.show).mockClear();
});

afterEach(() => {
    cd?.destroy();
});

describe("the chat-target dropdown never shows blank", () => {
    // If the target matches no option -- main absent, or an agent that cannot
    // be messaged -- the control would come up empty, so it shows a messageable
    // agent instead, without changing whom the person is talking to.

    it("shows main, the default, and offers no system agent", () => {
        open([agent("main", { protected: true }), agent("monitor-agent", { protected: true })]);
        goTo("chat", "main");

        expect(dropdown().value).toBe("main");
        expect(addressee()).toBe("main");
        expect([...dropdown().options].map(o => o.value)).not.toContain("monitor-agent");
    });

    it("shows the first messageable agent when there is no main, keeping the target", () => {
        open([agent("catalog"), agent("monitor-agent", { protected: true })]);
        goTo("chat", "main");

        // The *dropdown* falls back so the control shows something real. The
        // target is the user's choice and drawing a dropdown must not change it
        // -- that is what let a reset hand the conversation to another agent.
        expect(dropdown().value).toBe("catalog");
        expect(addressee()).toBe("main");
    });

    it("shows an agent the person picked", () => {
        open([agent("main"), agent("home-assistant-agent")]);
        goTo("chat", "home-assistant-agent");

        expect(dropdown().value).toBe("home-assistant-agent");
    });
});

describe("a reset does not hand the conversation to another agent", () => {
    beforeEach(() => {
        open([agent("main"), agent("catalog")]);
        goTo("chat");
        select("catalog");
    });

    it("keeps the chosen agent while the list churns", async () => {
        // A reset removes agents one at a time. Once catalog is gone but main
        // remains, the old code fell through to main and never came back.
        store.removeAgent("catalog");
        await settled();

        expect(addressee()).toBe("catalog");
    });

    it("keeps every part of the screen naming the same agent", async () => {
        // The screenshot that started this: sidebar, select and placeholder said
        // main while the header and thread said catalog -- and the message would
        // have gone to main.
        store.removeAgent("catalog");
        await settled();

        expect(paneTitle()).toBe("@catalog");
        expect(addressee()).toBe("catalog");
    });

    it("still shows the choice after the agents come back", async () => {
        store.removeAgent("catalog");
        await settled();
        store.addOrUpdateAgent(agent("catalog"));
        await settled();

        expect(addressee()).toBe("catalog");
        expect(paneTitle()).toBe("@catalog");
    });
});

describe("choosing a different agent", () => {
    // Every way of choosing must move the whole screen, not just the part the
    // caller happened to remember.
    beforeEach(() => {
        open([agent("main"), agent("catalog")]);
        goTo("chat");
        select("main");
    });

    it("switches the visible conversation, from the dropdown", () => {
        pick("catalog");

        expect(addressee()).toBe("catalog");
        expect(paneTitle()).toBe("@catalog");
    });

    it("sends to the chosen agent, not to whatever the dropdown shows", () => {
        // The dropdown's value is set programmatically whenever the options are
        // redrawn, and it falls back to the first option when the choice is not
        // among them. Reading the send target from there made drawing a control
        // decide where a message went.
        select("catalog");
        dropdown().value = "main"; // programmatic: no change event, so no choice was made

        expect(sentTo(send("hello"))).toBe("catalog");
    });

    it("switches the visible conversation, from a link to it", () => {
        // The @mention panel and the wactor-card Chat button land on the same move.
        goTo("chat", "catalog");

        expect(paneTitle()).toBe("@catalog");
        expect(addressee()).toBe("catalog");
    });
});

describe("sending to an agent that cannot answer", () => {
    // The choice survives a stop -- that is the point of holding it separately
    // from reachability. The send is what has to be honest about it.
    beforeEach(() => {
        open([agent("main"), agent("worker")]);
        goTo("chat");
        select("worker");
    });

    /** `worker` is now in `state`, as a status frame would say. */
    const workerBecomes = (state: AgentInfo["state"]) => store.addOrUpdateAgent(agent("worker", { state }));

    it("blocks the send and says why", () => {
        workerBecomes("stopped");

        expect(send("are you there")).not.toHaveBeenCalled();
        expect(toast.show).toHaveBeenCalledTimes(1);
        expect(vi.mocked(toast.show).mock.calls[0]![0].message).toContain("worker");
    });

    it("keeps what the user typed, so it can be sent once the agent is back", () => {
        workerBecomes("stopped");

        send("are you there");

        expect(composer().value).toBe("are you there");
    });

    it("keeps the choice, and just works when the agent restarts", () => {
        workerBecomes("stopped");
        send("are you there");
        expect(addressee()).toBe("worker");

        workerBecomes("running");

        expect(sentTo(send("are you there"))).toBe("worker");
    });

    it("blocks a failed agent too", () => {
        workerBecomes({ failed: "boom" });

        expect(send("hello")).not.toHaveBeenCalled();
    });

    it("lets an initializing agent through", () => {
        // Transient. A false block during normal startup would be worse than
        // the problem the check solves.
        workerBecomes("initializing");

        expect(send("hello")).toHaveBeenCalledTimes(1);
    });
});

describe("an agent that does not survive a reset", () => {
    beforeEach(() => {
        open([agent("main"), agent("weather-agent")]);
        goTo("chat");
        select("weather-agent");
    });

    /** A reset: the survivors replace the list, then the settled signal fires. */
    const resetTo = (names: string[]) => {
        store.clearAll();
        names.forEach(n => store.addOrUpdateAgent(agent(n)));
        emit("af-agents-settled", { reason: "reset" });
    };

    it("moves to main, because a spawned agent is destroyed by the reset", () => {
        resetTo(["main", "catalog"]);

        expect(addressee()).toBe("main");
        expect(paneTitle()).toBe("@main");
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

        expect(addressee()).toBe("weather-agent");
        expect(toast.show).not.toHaveBeenCalled();
    });

    it("does nothing when the list is momentarily empty", () => {
        // Mid-churn, "no agents" means "not back yet", never "yours is gone".
        resetTo([]);

        expect(addressee()).toBe("weather-agent");
        expect(toast.show).not.toHaveBeenCalled();
    });
});

describe("the target once something has been chosen, by the user or for them", () => {
    // A fallback chosen *for* the user is still a choice -- they were told
    // about it -- so nothing may silently move them off it afterwards.

    it("opens on main when there is one", () => {
        open([agent("main"), agent("catalog")]);
        goTo("chat");

        expect(addressee()).toBe("main");
    });

    it("opens on the only agent there is when main is absent, and keeps it", async () => {
        // First resolution sticks. Main arriving later does not displace a
        // target the user has already seen named on screen.
        open([agent("catalog")]);
        goTo("chat");
        expect(addressee()).toBe("catalog");

        store.addOrUpdateAgent(agent("main"));
        await settled();
        goTo("overview");
        goTo("chat");

        expect(addressee()).toBe("catalog");
    });

    it("takes main back when it arrives before the chat has been opened", async () => {
        // Agents register one at a time, so the first resolution runs against a
        // partial list and can land on whatever sorts first. Nothing has been
        // shown yet, so main claiming the target it would have had is not a move
        // under the user.
        open([]);
        store.addOrUpdateAgent(agent("catalog"));
        await settled();
        expect(addressee()).toBe("catalog");

        store.addOrUpdateAgent(agent("main"));
        await settled();
        goTo("chat");

        expect(addressee()).toBe("main");
    });

    it("does not move you again once a fallback has been chosen for you", async () => {
        // No main: pick weather-agent, lose it to a reset, get moved to catalog
        // and told so. Main registering afterwards must not quietly take over.
        open([agent("weather-agent"), agent("catalog")]);
        goTo("chat");
        select("weather-agent");

        store.removeAgent("weather-agent");
        emit("af-agents-settled", { reason: "reset" });
        expect(addressee()).toBe("catalog");

        store.addOrUpdateAgent(agent("main"));
        await settled();
        goTo("overview");
        goTo("chat");

        expect(addressee()).toBe("catalog");
    });
});

describe("deleting the agent you are chatting with", () => {
    // The target left the list for good, so move, rather than leaving the user
    // parked on a conversation that cannot resume. (A *stopped* agent is the
    // other branch: it stays, and the send blocks.)
    beforeEach(() => {
        open([agent("main"), agent("weather-agent")]);
        goTo("chat");
        select("weather-agent");
    });

    /** What the `delete_agent` frame drives: remove, then judge. */
    const deleteAgent = (name: string) => {
        store.removeAgent(name);
        emit("af-agents-settled", { reason: "deleted" });
    };

    it("moves the conversation off the deleted agent", () => {
        deleteAgent("weather-agent");

        expect(addressee()).toBe("main");
        expect(paneTitle()).toBe("@main");
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
        // nothing on screen to explain -- but the move itself still has to happen.
        goTo("overview");

        deleteAgent("weather-agent");

        expect(addressee()).toBe("main");
        expect(toast.show).not.toHaveBeenCalled();
    });

    it("leaves the conversation alone when a different agent is deleted", () => {
        store.addOrUpdateAgent(agent("other"));
        deleteAgent("other");

        expect(addressee()).toBe("weather-agent");
        expect(toast.show).not.toHaveBeenCalled();
    });
});

describe("the composer names the target on a fresh tab", () => {
    // The composer is built before any chat view exists and so before there is
    // a target, and only something that runs later can tell it what that target
    // turned out to be.
    beforeEach(() => localStorage.setItem("wactorz-chat-target", "home-assistant-agent"));

    it("names it when the agents arrive over the socket, on the overview", async () => {
        open([]); // fresh tab: visible, on the overview, nobody here yet

        store.addOrUpdateAgent(agent("main"));
        store.addOrUpdateAgent(agent("home-assistant-agent"));
        await settled();

        expect(shownView()).toBe("overview");
        expect(addressee()).toBe("home-assistant-agent");
    });

    it("names it on the overview, where chat was never opened", () => {
        open([agent("main"), agent("home-assistant-agent")]);

        expect(shownView()).toBe("overview");
        expect(addressee()).toBe("home-assistant-agent");
    });

    it("names the remembered agent when the list is already loaded", () => {
        open([agent("main"), agent("home-assistant-agent")]);
        goTo("chat");

        expect(addressee()).toBe("home-assistant-agent");
    });

    it("names it even when the agents arrive after the view is open", async () => {
        // The fresh-tab order: the socket has not delivered anyone yet when the
        // user lands on chat, so the first resolve has nothing to work with.
        open([]);
        goTo("chat");

        store.addOrUpdateAgent(agent("main"));
        store.addOrUpdateAgent(agent("home-assistant-agent"));
        await settled();

        expect(addressee()).toBe("home-assistant-agent");
    });

    it("does not move the conversation under someone already reading it", async () => {
        // Once the chat view has shown a real target, a remembered agent
        // registering late is just another agent.
        open([agent("main")]);
        goTo("chat"); // opens on main, and they are looking at it

        store.addOrUpdateAgent(agent("home-assistant-agent")); // registers late
        await settled();

        expect(addressee()).toBe("main");
    });
});

describe("an @mention", () => {
    it("is remembered as the agent reached, not the one left behind", () => {
        // Sending is a choice: `@catalog …` routes the message there and the
        // view follows, so reopening on main contradicts what was last shown.
        open([agent("main"), agent("catalog")]);
        goTo("chat");

        send("@catalog spawn weather-agent");

        expect(addressee()).toBe("catalog");
        expect(localStorage.getItem("wactorz-chat-target")).toBe("catalog");
    });
});
