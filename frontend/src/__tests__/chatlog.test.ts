/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { ChatLog, MAX_PER_THREAD, isOpenQuestion, threadOf } from "../chat/chatLog";
import type { ChatMessage } from "../types/agent";

/** A message; its thread, unless given, is its agent's: whom the person spoke to, or the sender.
 *  An agent's id here is its name. */
function msg(over: Partial<ChatMessage>): ChatMessage {
    const m = { id: "x", from: "main", to: "user", content: "hi", timestampMs: 1000, ...over };
    return { threadId: m.from === "user" ? m.to : m.from, ...m };
}

function question(id: string, over: Partial<ChatMessage> = {}): ChatMessage {
    return msg({
        id,
        from: "triage",
        content: `${id}?`,
        agentId: "triage-id",
        options: ["Yes", "No"],
        ...over,
    });
}

const ids = (list: readonly ChatMessage[]) => list.map(m => m.id);

describe("which thread a message is in", () => {
    it("is the agent the server named, by its id, whatever the names say", () => {
        expect(threadOf(msg({ from: "worker", to: "user", threadId: "w-1" }))).toBe("w-1");
        expect(threadOf(msg({ from: "user", to: "worker", threadId: "w-2" }))).toBe("w-2");
    });

    it("tells a question still open from one answered or a plain message", () => {
        expect(isOpenQuestion(question("q"))).toBe(true);
        expect(isOpenQuestion(question("q", { answer: "Yes" }))).toBe(false);
        expect(isOpenQuestion(msg({}))).toBe(false);
    });
});

describe("taking messages as they arrive", () => {
    it("keeps one copy per id, in the order they came", () => {
        const log = new ChatLog();

        expect(log.add(msg({ id: "a", timestampMs: 5 }))).toBe(true);
        expect(log.add(msg({ id: "b", timestampMs: 1 }))).toBe(true);
        expect(log.add(msg({ id: "a", content: "again" }))).toBe(false);

        // Arrival order: the page and the server stamp time on different clocks.
        expect(ids(log.thread("main"))).toEqual(["a", "b"]);
    });

    it("gives each thread its own cap, so one busy agent leaves another's alone", () => {
        const log = new ChatLog();
        log.addHistory("worker", [msg({ id: "hist-worker-1", from: "worker", timestampMs: 1 })]);

        for (let i = 0; i <= MAX_PER_THREAD; i++) {
            log.add(msg({ id: `m${i}`, timestampMs: 10 + i }));
        }

        expect(log.thread("main")).toHaveLength(MAX_PER_THREAD);
        expect(ids(log.thread("main"))[0]).toBe("m1");
        expect(ids(log.thread("worker"))).toEqual(["hist-worker-1"]);
        expect(log.historyLoaded("worker")).toBe(true);
    });

    it("replaces the list on every write rather than changing the one read before", () => {
        const log = new ChatLog();
        const before = log.all;

        log.add(msg({ id: "a" }));

        expect(before).toEqual([]);
        expect(log.all).not.toBe(before);
    });
});

describe("restoring history", () => {
    it("puts each restored message where its time is, and marks the thread loaded", () => {
        const log = new ChatLog();
        log.add(msg({ id: "live", content: "now", timestampMs: 300 }));

        const added = log.addHistory("main", [
            msg({ id: "hist-main-1", content: "first", timestampMs: 100 }),
            msg({ id: "hist-main-2", content: "second", timestampMs: 200 }),
        ]);

        expect(added).toBe(2);
        expect(ids(log.thread("main"))).toEqual(["hist-main-1", "hist-main-2", "live"]);
        expect(log.historyLoaded("main")).toBe(true);
    });

    it("takes a restored copy of a message on screen as that message, under the stored id", () => {
        const log = new ChatLog();
        const echo = msg({
            id: "user-1",
            from: "user",
            to: "main",
            threadId: "main",
            content: "deploy",
            timestampMs: 5000,
        });
        log.add(echo);

        const added = log.addHistory("main", [
            msg({
                id: "hist-main-9",
                from: "user",
                to: "main",
                threadId: "main",
                content: "@main deploy",
                timestampMs: 5050,
            }),
        ]);

        expect(added).toBe(0);
        expect(ids(log.all)).toEqual(["hist-main-9"]);
        // Copied, not changed where it stood.
        expect(echo.id).toBe("user-1");
    });

    it("pairs a second restored copy of the same words with the next message shown", () => {
        const log = new ChatLog();
        log.add(
            msg({
                id: "user-1",
                from: "user",
                to: "main",
                threadId: "main",
                content: "again",
                timestampMs: 1000,
            }),
        );
        log.add(
            msg({
                id: "user-2",
                from: "user",
                to: "main",
                threadId: "main",
                content: "again",
                timestampMs: 1100,
            }),
        );

        log.addHistory("main", [
            msg({
                id: "hist-main-1",
                from: "user",
                to: "main",
                threadId: "main",
                content: "again",
                timestampMs: 1000,
            }),
            msg({
                id: "hist-main-2",
                from: "user",
                to: "main",
                threadId: "main",
                content: "again",
                timestampMs: 1100,
            }),
        ]);

        expect(ids(log.all)).toEqual(["hist-main-1", "hist-main-2"]);
    });

    it("skips a restored message whose id is held", () => {
        const log = new ChatLog();
        log.add(msg({ id: "q-1" }));

        expect(log.addHistory("main", [msg({ id: "q-1" })])).toBe(0);
        expect(log.all).toHaveLength(1);
    });
});

describe("restoring history: messages already on screen", () => {
    // A message that arrived while the page was open has an id the page made up;
    // its persisted copy has another. These pin how the two are told to be one.
    const restore = (shown: ChatMessage[], incoming: ChatMessage[]) => {
        const log = new ChatLog();
        shown.forEach(m => log.add(m));
        const added = log.addHistory("main", incoming);
        return { log, added };
    };

    it("strips the @agent routing prefix without changing the restored message", () => {
        const incoming = msg({
            id: "hist-main-2",
            from: "user",
            to: "main",
            threadId: "main",
            content: "@main hello",
        });

        const { log } = restore([], [incoming]);

        expect(log.all[0]?.content).toBe("hello");
        expect(incoming.content).toBe("@main hello");
    });

    it("does not show a reply twice when history arrives after the reply did", () => {
        // An agent that goes and comes back under its name has its history
        // fetched again, with its replies already in the thread.
        const reply = msg({
            id: "stream-7",
            from: "counter",
            to: "counter",
            threadId: "counter",
            content: "counted 1",
            timestampMs: 5000,
        });
        const log = new ChatLog();
        log.add(
            msg({
                id: "hist-counter-1",
                from: "user",
                to: "counter",
                threadId: "counter",
                content: "one",
                timestampMs: 4900,
            }),
        );
        log.add(reply);

        const added = log.addHistory("counter", [
            msg({
                id: "hist-counter-1",
                from: "user",
                to: "counter",
                threadId: "counter",
                content: "one",
                timestampMs: 4900,
            }),
            msg({
                id: "hist-counter-2",
                from: "counter",
                to: "user",
                threadId: "counter",
                content: "counted 1",
                timestampMs: 5020,
            }),
        ]);

        expect(added).toBe(0);
        expect(ids(log.thread("counter"))).toEqual(["hist-counter-1", "hist-counter-2"]);
    });

    it("pairs two replies with the same words each with its own persisted copy", () => {
        const { log, added } = restore(
            [
                msg({ id: "stream-1", content: "done", timestampMs: 1000 }),
                msg({ id: "stream-2", content: "done", timestampMs: 3000 }),
            ],
            [
                msg({ id: "hist-main-1", content: "done", timestampMs: 1010 }),
                msg({ id: "hist-main-2", content: "done", timestampMs: 3010 }),
            ],
        );

        expect(added).toBe(0);
        expect(ids(log.all)).toEqual(["hist-main-1", "hist-main-2"]);
    });

    it("still tells the person's turns apart by whom they were to", () => {
        const log = new ChatLog();
        log.add(
            msg({
                id: "user-1",
                from: "user",
                to: "main",
                threadId: "main",
                content: "status",
                timestampMs: 1000,
            }),
        );

        log.addHistory("other", [
            msg({
                id: "hist-other-1",
                from: "user",
                to: "other",
                threadId: "other",
                content: "status",
                timestampMs: 1010,
            }),
        ]);

        expect(ids(log.thread("main"))).toEqual(["user-1"]);
        expect(ids(log.thread("other"))).toEqual(["hist-other-1"]);
    });

    it("keeps a persisted reply from long before the one on screen with the same words", () => {
        const { log, added } = restore(
            [msg({ id: "stream-9", content: "done", timestampMs: 10_000_000 })],
            [msg({ id: "hist-main-1", content: "done", timestampMs: 1000 })],
        );

        expect(added).toBe(1);
        expect(ids(log.all)).toEqual(["hist-main-1", "stream-9"]);
    });

    it("adds replies and the person's turns that match nothing on screen", () => {
        const { log } = restore(
            [],
            [
                msg({ id: "hist-main-4", content: "reply" }),
                msg({
                    id: "hist-main-5",
                    from: "user",
                    to: "main",
                    threadId: "main",
                    content: "new question",
                }),
            ],
        );

        expect(ids(log.all)).toEqual(["hist-main-4", "hist-main-5"]);
    });
});

describe("questions", () => {
    it("marks one answered, as a copy, and says nothing for what is no question here", () => {
        const log = new ChatLog();
        const asked = question("q1");
        log.add(asked);
        log.add(msg({ id: "plain" }));

        expect(log.answer("q1", "No")?.answer).toBe("No");
        expect(asked.answer).toBeUndefined();
        expect(log.answer("plain", "No")).toBeNull();
        expect(log.answer("nope", "No")).toBeNull();
    });

    it("takes the ledger's open questions where their time is, once", () => {
        const log = new ChatLog();
        log.add(msg({ id: "late", from: "triage", timestampMs: 900 }));

        const first = log.reconcile([question("q1", { timestampMs: 100 })], []);
        const again = log.reconcile([question("q1", { timestampMs: 100 })], []);

        expect(ids(log.thread("triage"))).toEqual(["q1", "late"]);
        expect(first.changed).toBe(true);
        expect(again).toEqual({ changed: false, closed: [] });
    });

    it("marks a question answered meanwhile, and removes one the ledger no longer lists", () => {
        const log = new ChatLog();
        log.add(question("answered-elsewhere"));
        log.add(question("gone"));
        log.add(question("still-open"));

        const result = log.reconcile(
            [question("still-open")],
            [question("answered-elsewhere", { answer: "Yes" })],
        );

        expect(result.closed.sort()).toEqual(["answered-elsewhere", "gone"]);
        expect(log.all.map(m => [m.id, m.answer])).toEqual([
            ["answered-elsewhere", "Yes"],
            ["still-open", undefined],
        ]);
    });

    it("leaves the questions of an agent the ledger does not know", () => {
        // An agent that keeps no book asks only live: the ledger never lists it.
        const log = new ChatLog();
        log.add(question("install?", { from: "installer", agentId: "installer-id" }));

        const result = log.reconcile([question("q1")], []);

        expect(result.closed).toEqual([]);
        expect(ids(log.thread("installer"))).toEqual(["install?"]);
    });
});

describe("what one message costs", () => {
    it("leaves every other thread as it was: only the thread written to is copied", () => {
        const log = new ChatLog();
        for (let t = 0; t < 20; t++) {
            log.add(msg({ id: `t${t}`, from: `agent${t}` }));
        }
        const others = Array.from({ length: 19 }, (_, t) => log.thread(`agent${t + 1}`));

        log.add(msg({ id: "new", from: "agent0" }));
        log.answer("t5", "x");

        // The same arrays, not copies: the cost of a message is one thread's.
        others.forEach((before, t) => {
            if (t + 1 !== 5) {
                expect(log.thread(`agent${t + 1}`)).toBe(before);
            }
        });
    });

    it("finds a message by id, and keeps no id it dropped", () => {
        const log = new ChatLog();
        for (let i = 0; i <= MAX_PER_THREAD; i++) {
            log.add(msg({ id: `m${i}`, timestampMs: i }));
        }

        expect(log.get("m1")?.id).toBe("m1");
        // Capped out: gone from the index too, so it may be taken again.
        expect(log.get("m0")).toBeUndefined();
        expect(log.add(msg({ id: "m0" }))).toBe(true);
    });

    it("knows who is asking without reading every message, as questions come and go", () => {
        const log = new ChatLog();
        log.add(question("q1"));
        log.add(question("q2", { from: "installer", agentId: "installer-id" }));
        log.add(msg({ id: "plain", from: "worker" }));
        expect([...log.asking()].sort()).toEqual(["installer", "triage"]);

        log.answer("q1", "Yes");
        expect([...log.asking()]).toEqual(["installer"]);

        log.reset("installer");
        expect(log.asking().size).toBe(0);
    });
});

describe("taking messages away", () => {
    it("removes the newest message a test picks", () => {
        const log = new ChatLog();
        log.add(msg({ id: "1", from: "user", to: "main", threadId: "main", content: "ping" }));
        log.add(msg({ id: "2", from: "user", to: "main", threadId: "main", content: "ping" }));

        expect(log.removeLast("main", m => m.content === "ping")).toBe(true);
        expect(ids(log.all)).toEqual(["1"]);
        expect(log.removeLast("main", m => m.content === "nothing")).toBe(false);
        expect(log.removeLast("worker", m => m.content === "ping")).toBe(false);
    });

    it("resets one agent's thread, keeping the person's turns to others", () => {
        const log = new ChatLog();
        log.add(msg({ id: "w", from: "worker" }));
        log.add(msg({ id: "m", from: "main" }));
        log.add(msg({ id: "u-w", from: "user", to: "worker" }));
        log.add(msg({ id: "u-m", from: "user", to: "main" }));
        log.addHistory("main", []);

        log.reset("worker");

        expect(ids(log.all)).toEqual(["m", "u-m"]);
        // The server's copy may have been reset with it: fetched again.
        expect(log.historyLoaded("main")).toBe(false);
    });

    it("resets or clears everything, and forgets one thread's history on its own", () => {
        const log = new ChatLog();
        log.add(msg({ id: "a" }));
        log.addHistory("main", []);
        log.addHistory("worker", []);

        log.forgetHistory("worker");
        expect([log.historyLoaded("main"), log.historyLoaded("worker")]).toEqual([true, false]);

        log.reset(null);
        expect(log.all).toEqual([]);
        log.add(msg({ id: "b" }));
        log.addHistory("main", []);
        log.clear();
        expect([log.all.length, log.historyLoaded("main")]).toEqual([0, false]);
    });
});

describe("two agents of one name", () => {
    it("keep a thread each", () => {
        const log = new ChatLog();

        log.add(msg({ id: "a", from: "counter", threadId: "on-main" }));
        log.add(msg({ id: "b", from: "counter", threadId: "on-the-pi" }));

        expect(ids(log.thread("on-main"))).toEqual(["a"]);
        expect(ids(log.thread("on-the-pi"))).toEqual(["b"]);
    });

    it("are both cleared by a reset of the name, as the server clears them", () => {
        const log = new ChatLog();
        log.add(msg({ id: "a", from: "counter", threadId: "on-main" }));
        log.add(msg({ id: "b", from: "user", to: "counter", threadId: "on-the-pi" }));
        log.add(msg({ id: "c", from: "weather", threadId: "weather-id" }));

        log.reset("counter");

        expect(ids(log.all)).toEqual(["c"]);
    });

    it("say by id which is asking something", () => {
        const log = new ChatLog();
        log.add(question("q", { threadId: "on-the-pi" }));

        expect(log.asking()).toEqual(new Set(["on-the-pi"]));
    });
});

describe("moving a message to the agent it reached", () => {
    it("files it in that thread where its time puts it, named for that agent", () => {
        const log = new ChatLog();
        log.add(msg({ id: "early", from: "catalog", threadId: "catalog-id", timestampMs: 1 }));
        log.add(msg({ id: "late", from: "catalog", threadId: "catalog-id", timestampMs: 3 }));
        log.add(msg({ id: "sent", from: "user", to: "main", threadId: "main-id", timestampMs: 2 }));

        const moved = log.move("sent", "catalog-id", "catalog");

        expect(moved).toMatchObject({ threadId: "catalog-id", to: "catalog" });
        expect(ids(log.thread("main-id"))).toEqual([]);
        expect(ids(log.thread("catalog-id"))).toEqual(["early", "sent", "late"]);
        expect(log.get("sent")?.threadId).toBe("catalog-id");
    });

    it("leaves one already there as it is, and knows nothing of one it does not hold", () => {
        const log = new ChatLog();
        log.add(msg({ id: "here", threadId: "main-id" }));

        expect(log.move("here", "main-id", "main")).toBe(log.get("here"));
        expect(log.move("absent", "main-id", "main")).toBeNull();
    });
});
