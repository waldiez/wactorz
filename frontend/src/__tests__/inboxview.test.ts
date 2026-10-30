/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The Inbox as a person uses it: the address opens a list, a question is
 * answered from its card with an optional note, a refusal is said, what is typed
 * survives what arrives, and the count follows the question to the tabs.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import type { AgentStore } from "../agents/AgentStore";
import type { CardDashboard } from "../ui/CardDashboard";
import { emit, listen } from "../events";
import type { Route } from "../router";
import type { LedgerQuestion } from "../types/agent";
import { toast } from "../ui/ToastManager";
import { agent, openDashboard, shownView } from "./dashboard";
import { allByRole, byRole } from "./queries";

function asked(id: string, over: Partial<LedgerQuestion> = {}): LedgerQuestion {
    return {
        id,
        agent_id: "a1",
        agent: "asker",
        text: `Is ${id} in place?`,
        options: ["Yes", "No"],
        channel: "dashboard",
        asked_at: 1_700_000_000,
        ...over,
    };
}

function book(...questions: LedgerQuestion[]): void {
    emit("af-question-book", { agentId: "a1", questions });
}

describe("the Inbox", () => {
    let dashboard: CardDashboard;
    let store: AgentStore;
    let fetched: ReturnType<typeof vi.fn>;

    function open(path: string[] = []): void {
        emit("af-route", { view: "inbox", path });
    }

    /** The questions waiting, as they are named. */
    function cards(): string[] {
        const waiting = byRole(document.body, "list", "Questions waiting for you");
        return allByRole(waiting, "listitem").map(card => card.getAttribute("aria-label") ?? "");
    }

    beforeEach(() => {
        ({ dashboard, store } = openDashboard([agent("asker", { id: "a1" })]));
        fetched = vi.fn(async () => Response.json({ answer: {} }));
        vi.stubGlobal("fetch", fetched);
    });
    afterEach(() => {
        dashboard.destroy();
        vi.unstubAllGlobals();
    });

    it("is a view with an address of its own, and the answered list one level in", () => {
        const changes: Route[] = [];
        const stop = listen("af-view-changed", route => changes.push(route));

        open();
        expect(shownView()).toBe("inbox");
        expect(changes.at(-1)).toEqual({ view: "inbox", path: [] });

        open(["answered"]);
        expect(changes.at(-1)).toEqual({ view: "inbox", path: ["answered"] });
        expect(byRole(document.body, "link", "Answered").getAttribute("aria-current")).toBe("page");

        open(["no-such-list"]);
        expect(changes.at(-1)).toEqual({ view: "inbox", path: [] });
        document.removeEventListener("af-view-changed", stop);
    });

    it("says what an empty list means: reading, nothing, or could not read", () => {
        open();
        expect(document.body.textContent).toContain("Reading the questions");

        emit("af-questions-listed", { open: [], answered: [] });
        expect(document.body.textContent).toContain("Could not read the questions");

        emit("af-questions-listed", { open: [], answered: [], listed: { questions: [], askedAt: 0 } });
        expect(document.body.textContent).toContain("Nothing is waiting for you");

        open(["answered"]);
        expect(document.body.textContent).toContain("Nothing answered yet");
    });

    it("says a later read failed rather than that nothing is waiting", () => {
        open();
        emit("af-questions-listed", { open: [], answered: [], listed: { questions: [], askedAt: 0 } });

        emit("af-questions-listed", { open: [], answered: [] });

        expect(document.body.textContent).toContain("Could not read the questions");
        expect(document.body.textContent).not.toContain("Nothing is waiting");
    });

    it("shows who asked, why, and where else it went, and links to their thread", () => {
        open();
        book(asked("probe", { context: "The reading jumped.", channel: "telegram" }));

        expect(cards()).toEqual(["asker: Is probe in place?"]);
        expect(document.body.textContent).toContain("The reading jumped.");
        expect(document.body.textContent).toContain("asked on telegram");
        expect(byRole(document.body, "link", "asker").getAttribute("href")).toBe("#/chat/asker");
    });

    it("names an agent whose name is shared by its address", () => {
        store.addOrUpdateAgent(agent("asker", { id: "a1", node: "edge" }));
        store.addOrUpdateAgent(agent("asker", { id: "a2", node: "rpi" }));
        open();
        book(asked("probe"));

        const link = allByRole(document.body, "link", "asker@edge");
        expect(link.at(-1)?.getAttribute("href")).toBe("#/chat/asker@edge");
    });

    it("sends the pick with the note, then moves the question to Answered", async () => {
        open();
        book(asked("probe"));
        const note = byRole(document.body, "textbox", "A note with your answer") as HTMLTextAreaElement;
        note.value = "  checked by hand ";

        byRole(document.body, "button", "No").click();

        await vi.waitFor(() => expect(cards()).toEqual([]));
        const calls = fetched.mock.calls as unknown as [string, RequestInit][];
        expect(calls[0]?.[0]).toBe("/api/questions/a1/probe/answer");
        expect(JSON.parse(String(calls[0]?.[1].body))).toEqual({ option: "No", note: "checked by hand" });

        open(["answered"]);
        expect(document.body.textContent).toContain("Answered: No");
        expect(document.body.textContent).toContain("checked by hand");
    });

    it("says a refusal on the card, and offers the options again", async () => {
        fetched.mockResolvedValue(Response.json({ error: "was already answered" }, { status: 409 }));
        open();
        book(asked("probe"));

        const yes = byRole(document.body, "button", "Yes") as HTMLButtonElement;
        yes.click();
        expect(yes.disabled).toBe(true);

        await vi.waitFor(() => expect(byRole(document.body, "alert").textContent).toContain("already"));
        expect(yes.disabled).toBe(false);
        expect(cards()).toHaveLength(1);
    });

    it("keeps a half-typed note and its place when another question arrives", () => {
        open();
        book(asked("later", { asked_at: 20 }));
        const note = byRole(document.body, "textbox", "A note with your answer") as HTMLTextAreaElement;
        note.value = "half a tho";

        book(asked("earlier", { asked_at: 10 }), asked("later", { asked_at: 20 }));

        expect(cards()).toEqual(["asker: Is earlier in place?", "asker: Is later in place?"]);
        const notes = allByRole(document.body, "textbox", "A note with your answer");
        expect((notes[1] as HTMLTextAreaElement).value).toBe("half a tho");
        expect(notes[1]).toBe(note);
    });

    it("takes a question off when it is answered elsewhere, and says where in Answered", () => {
        open();
        book(asked("probe"));

        book(
            asked("probe", {
                answer: "Yes",
                answer_channel: "telegram",
                answered_at: 1_700_000_100,
                answer_note: "on site",
            }),
        );

        expect(cards()).toEqual([]);
        open(["answered"]);
        expect(document.body.textContent).toContain("Answered: Yes (on telegram)");
    });

    it("closes a question's toast while the Inbox is open, and leaves it elsewhere", () => {
        const closed = vi.spyOn(toast, "close");
        book(asked("waiting"));
        expect(closed).not.toHaveBeenCalled();

        open();
        expect(closed).toHaveBeenCalledWith("waiting");
        expect(dashboard.inboxOpen).toBe(true);
        closed.mockRestore();
    });

    it("counts what waits on the tabs of the header and the phone bar", () => {
        open();
        const tabs = () => document.querySelectorAll<HTMLElement>('.af-view-btn[data-view="inbox"]');
        expect(tabs()).toHaveLength(2);

        book(asked("one"), asked("two"));
        tabs().forEach(tab => {
            expect(tab.textContent).toMatch(/Inbox 2 waiting$/);
            expect(tab.hasAttribute("aria-label")).toBe(false);
        });

        book();
        tabs().forEach(tab => {
            expect(tab.querySelector(".af-nav-count")).toBeNull();
        });
    });

    it("keeps the count when the nav is rebuilt for an extension's view", () => {
        book(asked("one"));
        dashboard.registerView("extra", "grid", "Extra", () => document.createElement("div"));
        const counts = document.querySelectorAll('.af-view-btn[data-view="inbox"] .af-nav-count');
        expect(counts).toHaveLength(2);
    });
});
