/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** The avatar in each place that shows it: overview cards, the chat, Wiring, and the Inbox. */
import { afterEach, describe, expect, it, vi } from "vitest";

import { QUIET_MS } from "../../agents/liveness";
import type { Answer } from "../../io/api";
import type { AgentInfo, ChatMessage, LedgerQuestion } from "../../types/agent";
import type { Wiring } from "../../types/wiring";
import { avatar } from "../../ui/avatars";
import { lookFor, type Mood } from "../../ui/avatars/cast";
import { avatarUrl } from "../../ui/avatars/draw";
import { renderPaneHeader } from "../../ui/chat/chatLayout";
import { renderChatSidebar } from "../../ui/chat/chatSidebar";
import { ChatStreamUI } from "../../ui/chat/chatStreaming";
import { buildChatMessageEl } from "../../ui/chat/chatThread";
import { answeredCard, QuestionCard } from "../../ui/inbox/questionCard";
import { buildWactorCard, paintCard, paintCardMood } from "../../ui/overview/cards";
import { Heartbeats } from "../../ui/overview/heartbeats";
import { WiringData } from "../../ui/wiring/wiringData";
import { WiringView } from "../../ui/wiring/WiringView";
import { agent } from "../dashboard";
import { allByRole, byTestId, nameOf } from "../queries";
import { chunk } from "../streamFeed";
import { agent as wired, wiring } from "../wiring/fixtures";

const face = (root: ParentNode): HTMLElement => root.querySelector<HTMLElement>(".af-avatar")!;
const src = (node: ParentNode): string => node.querySelector("img")?.getAttribute("src") ?? "";
const shows = (who: AgentInfo, mood: Mood, crop: "full" | "head"): string =>
    avatarUrl(lookFor(who.name, who.avatar, mood), crop);

const weather = agent("weather", { avatar: { kind: "weather-agent" } });

afterEach(() => {
    localStorage.clear();
    document.body.replaceChildren();
    vi.unstubAllGlobals();
});

describe("an overview card", () => {
    const callbacks = { onChat: vi.fn(), onCommand: vi.fn(), onHistory: vi.fn() };

    it("has its agent's avatar beside the dot, before the lines that name it", () => {
        const card = buildWactorCard(weather, 0, callbacks);
        const head = card.firstElementChild!;
        expect(head.className).toBe("af-card-head");
        const [faced, who] = [...head.children];
        expect([...faced!.children].map(child => child.className.split(" ")[0])).toEqual([
            "af-avatar",
            "af-card-state-dot",
        ]);
        expect([...who!.children].map(child => child.className)).toEqual([
            "af-card-name",
            "af-card-node",
            "af-card-state-label",
        ]);
        expect(src(face(card))).toBe(shows(weather, "running", "full"));
        // The avatar adds nothing to what the card is called or says.
        expect(card.getAttribute("aria-label")).toBe("weather");
        expect(byTestId(card, "agent-state").textContent).toBe("running");
    });

    it("follows its agent's state and avatar when it is painted again", () => {
        const card = buildWactorCard(weather, 0, callbacks);
        paintCard(card, { ...weather, state: "stopped" }, "weather");
        expect(face(card).dataset["mood"]).toBe("stopped");
        paintCard(card, { ...weather, state: { failed: "boom" }, avatar: { kind: "rule" } }, "weather");
        expect(src(face(card))).toBe(shows({ ...weather, avatar: { kind: "rule" } }, "failed", "full"));
    });

    it("looks busy once its agent has gone quiet, and awake again when it is heard from", () => {
        const root = document.body.appendChild(document.createElement("div"));
        const card = root.appendChild(buildWactorCard(weather, Date.now(), callbacks));
        const beats = new Heartbeats(root);

        beats.record("weather", Date.now() - QUIET_MS * 2, { skip: true });
        beats.refresh();
        expect(face(card).dataset["mood"]).toBe("late");
        expect(src(face(card))).toBe(shows(weather, "late", "full"));
        // Painted again while quiet, it stays late.
        paintCard(card, weather, "weather");
        expect(face(card).dataset["mood"]).toBe("late");

        beats.record("weather", Date.now());
        expect(face(card).dataset["mood"]).toBe("running");
    });

    it("keeps a stopped agent asleep however long it is quiet", () => {
        const root = document.body.appendChild(document.createElement("div"));
        const stopped = { ...weather, state: "stopped" as const };
        const card = root.appendChild(buildWactorCard(stopped, Date.now(), callbacks));
        const beats = new Heartbeats(root);
        beats.record("weather", Date.now() - QUIET_MS * 2, { skip: true });
        beats.refresh();
        expect(face(card).dataset["mood"]).toBe("stopped");
    });

    it("has no mood to paint without an avatar or a dot", () => {
        const card = buildWactorCard(weather, 0, callbacks);
        face(card).remove();
        expect(() => paintCardMood(card)).not.toThrow();
        expect(() => paintCard(card, weather, "weather")).not.toThrow();
        const bare = buildWactorCard(weather, 0, callbacks);
        bare.querySelector(".af-card-state-dot")!.remove();
        paintCardMood(bare);
        paintCard(bare, weather, "weather");
        expect(face(bare).dataset["mood"]).toBe("running");
    });
});

describe("the chat", () => {
    it("has each agent's avatar in the list, with its dot, and follows its state", () => {
        const list = document.createElement("div");
        renderChatSidebar(list, [weather], "weather", vi.fn());
        const row = list.querySelector<HTMLElement>(".af-chat-agent-row")!;
        const faced = row.firstElementChild!;
        expect(faced.className).toBe("af-chat-agent-face");
        expect([...faced.children].map(child => child.className.split(" ")[0])).toEqual([
            "af-avatar",
            "af-chat-agent-dot",
        ]);
        expect(src(face(row))).toBe(shows(weather, "running", "head"));

        renderChatSidebar(list, [{ ...weather, state: "stopped" }], "weather", vi.fn());
        expect(list.querySelectorAll(".af-chat-agent-row")).toHaveLength(1);
        expect(face(row).dataset["mood"]).toBe("stopped");
    });

    it("draws a row whose avatar is gone without it", () => {
        const list = document.createElement("div");
        renderChatSidebar(list, [weather], "weather", vi.fn());
        face(list).remove();
        expect(() => renderChatSidebar(list, [weather], "weather", vi.fn())).not.toThrow();
    });

    it("has the open agent's avatar in the header, and none for a target that is no agent", () => {
        const header = document.createElement("div");
        renderPaneHeader(header, "weather", { ...weather, state: "stopped" }, vi.fn());
        expect(face(header).dataset["mood"]).toBe("stopped");
        expect(face(header).parentElement!.className).toBe("af-chat-agent-face");
        expect(byTestId(header, "chat-title").textContent).toBe("@weather");

        renderPaneHeader(header, "gone", undefined, vi.fn());
        expect(header.querySelector(".af-avatar")).toBeNull();
    });

    const message = (over: Partial<ChatMessage>): ChatMessage => ({
        id: "m1",
        from: "weather",
        to: "user",
        threadId: "weather",
        content: "Sunny.",
        timestampMs: 1_700_000_000_000,
        ...over,
    });

    it("puts the sender's avatar before its name on a reply, and none on the person's own words", () => {
        const faceOf = vi.fn((name: string) => avatar({ name }, "running", "small"));
        const reply = buildChatMessageEl(message({}), faceOf);
        const from = byTestId(reply, "message-from");
        expect(from.firstElementChild).toBe(face(reply));
        expect(from.textContent).toBe("weather");
        expect(faceOf).toHaveBeenCalledWith("weather");

        const mine = buildChatMessageEl(message({ from: "user", to: "weather" }), faceOf);
        expect(mine.querySelector(".af-avatar")).toBeNull();
        expect(buildChatMessageEl(message({})).querySelector(".af-avatar")).toBeNull();
    });

    it("puts it on the row of a reply still arriving, and on the row that waits for one", () => {
        const thread = document.body.appendChild(document.createElement("div"));
        const asked: string[] = [];
        const ui = new ChatStreamUI({
            isChatView: () => true,
            lastSentTarget: () => ({ name: "weather", id: "weather" }),
            isOpenThread: () => true,
            thread: () => thread,
            scrollThread: () => {},
            commit: () => {},
            senderAvatar: name => {
                asked.push(name);
                return avatar({ name }, "running", "small");
            },
        });

        ui.awaiting("weather", "weather", "weather");
        expect(thread.querySelector(".af-chat-waiting")).not.toBeNull();
        expect(byTestId(thread, "message-from").firstElementChild).toBe(face(thread));

        chunk(ui, "Sun", "weather");
        const from = byTestId(thread, "message-from");
        expect(from.firstElementChild?.classList.contains("af-avatar")).toBe(true);
        expect(from.textContent).toBe("weather");
        expect(asked).toEqual(["weather", "weather"]);
    });
});

describe("Wiring", () => {
    const views: WiringView[] = [];

    async function open(value: Wiring): Promise<HTMLElement> {
        vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query }));
        const answer: Answer<Wiring> = { ok: true, value };
        const data = new WiringData({ isShowing: () => true }, () => Promise.resolve(answer));
        const view = new WiringView(data);
        views.push(view);
        const root = document.body.appendChild(view.build());
        await vi.waitFor(() => expect(data.state.reading).toBe(false));
        return root;
    }

    afterEach(() => {
        views.splice(0).forEach(view => view.destroy());
    });

    it("has an avatar on each agent's box and none on a topic's, and in an agent's details", async () => {
        const graph = wiring();
        graph.agents[0] = { ...graph.agents[0]!, avatar: { kind: "flic" } };
        const root = await open(graph);
        const boxes = allByRole(root, "button").filter(box => box.classList.contains("af-wire-node"));
        const agents = boxes.filter(box => box.classList.contains("agent"));
        // The view leaves system agents out until asked: those it shows all have one.
        expect(agents.length).toBeGreaterThan(1);
        expect(agents.every(box => box.querySelector(".af-avatar") !== null)).toBe(true);
        expect(
            boxes
                .filter(box => !box.classList.contains("agent"))
                .some(box => box.querySelector(".af-avatar")),
        ).toBe(false);

        const detect = agents.find(box => nameOf(box).includes("detect"))!;
        expect(src(face(detect))).toBe(avatarUrl(lookFor("detect", { kind: "flic" }, "running"), "head"));
        // The avatar is not part of what the box is called.
        expect(nameOf(detect)).toContain("detect");

        detect.click();
        const title = root.querySelector<HTMLElement>(".af-wire-detail-title")!;
        expect(title.firstElementChild).toBe(face(title));
        expect(title.textContent).toBe("detect");

        boxes.find(box => box.classList.contains("topic"))!.click();
        expect(root.querySelector(".af-wire-detail-title .af-avatar")).toBeNull();
    });

    it("draws an agent that says nothing of its avatar, or nonsense, from its name", async () => {
        const root = await open({
            ...wiring(),
            agents: [wired("plain"), wired("odd", { avatar: "main" })],
        });
        for (const name of ["plain", "odd"]) {
            const box = allByRole(root, "button").find(b => nameOf(b).includes(name))!;
            expect(src(face(box))).toBe(avatarUrl(lookFor(name, undefined, "running"), "head"));
        }
    });
});

describe("an Inbox card", () => {
    const question: LedgerQuestion = {
        id: "q1",
        agent_id: "weather",
        agent: "weather",
        text: "Send the forecast?",
        options: ["Yes", "No"],
        channel: "dashboard",
        asked_at: 1_700_000_000,
    };

    it("has the avatar of the agent that asked before its name, waiting and answered", () => {
        const waiting = new QuestionCard(question, {
            label: () => "weather",
            face: () => avatar(weather, "running", "small"),
            answer: () => Promise.resolve(""),
        });
        const who = waiting.element.querySelector<HTMLElement>(".af-inbox-who")!;
        expect(who.firstElementChild).toBe(face(who));
        expect(who.querySelector("a")?.textContent).toBe("weather");

        const answered = answeredCard(
            { ...question, answer: "Yes", answered_at: 1_700_000_100 },
            "weather",
            avatar(weather, "running", "small"),
        );
        expect(answered.querySelector(".af-inbox-who")!.firstElementChild).toBe(face(answered));
        expect(answered.getAttribute("aria-label")).toBe("weather: Send the forecast?");
    });
});
