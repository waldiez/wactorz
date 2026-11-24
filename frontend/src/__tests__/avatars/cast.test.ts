/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** Which character an agent gets: by its kind, by the parts it asks for, or from its name; and its mood. */
import { describe, expect, it } from "vitest";

import { KINDS, lookFor, moodOf } from "../../ui/avatars/cast";

describe("the character an agent gets", () => {
    it("is the ready-made one its kind names, whatever it is called", () => {
        const main = lookFor("main", { kind: "main" }, "running");
        expect(main).toMatchObject({ outfit: "suit", neck: "tie", top: "captain" });
        expect(lookFor("someone-else", { kind: "main" }, "running")).toEqual(main);
    });

    it("draws the manual's agent as a librarian with an open book", () => {
        expect(lookFor("manual-agent", { kind: "manual-agent" }, "running")).toMatchObject({
            specs: "square",
            neck: "bowtie",
            face: "smile",
            prop: "book",
        });
    });

    it("has a ready-made character for every kind the server names", () => {
        // What `Actor.AVATAR_KIND` is set to on the shipped agents, and each dynamic catalogue recipe's name.
        const named = [
            "main",
            "planner",
            "llm",
            "monitor",
            "home-assistant",
            "ha-actuator",
            "ha-map",
            "ha-state-bridge",
            "installer",
            "train",
            "recorder",
            "rule",
            "scheduled",
            "function",
            "dynamic",
            "catalog",
            "gmail-agent",
            "google-calendar-agent",
            "weather-agent",
            "flic",
            "doc-to-pptx-agent",
            "anomaly-detector",
            "smart-energy",
            "manual-agent",
            "reachy-mini",
            "timeseries-collector",
        ];
        expect([...KINDS].sort()).toEqual([...named].sort());
    });

    it("draws rule as a referee, and no ready-made character with a beard or a moustache", () => {
        const rule = lookFor("rule", { kind: "rule" }, "running");
        expect(rule).toMatchObject({ outfit: "stripes", top: "refcap", prop: "whistle" });
        for (const kind of KINDS) {
            expect(["beard", "mustache"], kind).not.toContain(lookFor(kind, { kind }, "running").face);
        }
    });

    it("is made up from its name when it names no kind, the same every time", () => {
        const first = lookFor("pump-watcher", undefined, "running");
        expect(lookFor("pump-watcher", {}, "running")).toEqual(first);
        expect(lookFor("pump-watcher", { kind: "no-such-kind" }, "running")).toEqual(first);
        expect(lookFor("pump-watcher", { kind: "toString" }, "running")).toEqual(first);
        const looks = new Set(
            ["a", "b", "c", "d", "e", "f", "g", "h", "pump-1", "pump-2", "pump-3", "pump-4"].map(name =>
                JSON.stringify(lookFor(name, undefined, "running")),
            ),
        );
        expect(looks.size).toBeGreaterThan(6);
    });

    it("differs between agents of a kind many share, and keeps what the kind holds", () => {
        const names = ["meter-1", "meter-2", "meter-3", "meter-4", "meter-5", "meter-6"];
        const looks = names.map(name => lookFor(name, { kind: "function" }, "running"));
        expect(looks.every(look => look.prop === "function")).toBe(true);
        expect(new Set(looks.map(look => JSON.stringify(look))).size).toBeGreaterThan(2);
    });

    it("gives a generated agent a screen of code, and the rest from its name", () => {
        const looks = ["reminder-bot", "translator", "stock-tracker"].map(name => [
            lookFor(name, { kind: "dynamic" }, "running"),
            lookFor(name, undefined, "running"),
        ]);
        for (const [generated, unnamed] of looks) {
            expect(generated).toEqual({ ...unnamed, prop: "code" });
        }
    });

    it("takes the parts a spawn asks for, each only when it is one there is", () => {
        const asked = lookFor(
            "custom",
            {
                kind: "main",
                accent: "#123456",
                outfit: "stripes",
                neck: "bowtie",
                specs: "square",
                face: "open",
                top: "tophat",
                ant: 2,
                prop: "clock",
            },
            "running",
        );
        expect(asked).toMatchObject({
            accent: "#123456",
            outfit: "stripes",
            neck: "bowtie",
            specs: "square",
            face: "open",
            top: "tophat",
            ant: 2,
            prop: "clock",
        });

        const main = lookFor("custom", { kind: "main" }, "running");
        const refused = lookFor(
            "custom",
            {
                kind: "main",
                accent: 'red"/><script>',
                outfit: "cape",
                neck: 3,
                specs: "<svg>",
                face: "frown",
                top: "crown",
                ant: 7,
                prop: "sword",
            },
            "running",
        );
        expect(refused).toEqual(main);
    });

    it("lets a spawn ask for no prop and no hat", () => {
        const bare = lookFor("x", { kind: "weather-agent", prop: "", top: "" }, "running");
        expect(bare.prop).toBe("");
        expect(bare.top).toBe("");
    });

    it("shows its mood in its eyes, and in its mouth once it has failed", () => {
        const of = (mood: Parameters<typeof lookFor>[2]) => lookFor("x", { kind: "scheduled" }, mood);
        expect(of("running").look).toBe("awake");
        expect(of("initializing").look).toBe("happy");
        expect(of("late").look).toBe("busy");
        expect(of("stopped").look).toBe("asleep");
        expect(of("failed")).toMatchObject({ look: "crashed", face: "open" });
    });

    it("takes dark glasses off an agent that is stopped or has failed", () => {
        const of = (mood: Parameters<typeof lookFor>[2]) =>
            lookFor("x", { kind: "smart-energy" }, mood).specs;
        expect(of("running")).toBe("shades");
        expect(of("late")).toBe("shades");
        expect(of("stopped")).toBe("round");
        expect(of("failed")).toBe("round");
    });
});

describe("the mood a state puts a character in", () => {
    it("follows the state, and is late only for an agent that runs", () => {
        expect(moodOf("running")).toBe("running");
        expect(moodOf("running", true)).toBe("late");
        expect(moodOf("initializing", true)).toBe("initializing");
        expect(moodOf("stopped", true)).toBe("stopped");
        expect(moodOf({ failed: "boom" })).toBe("failed");
    });
});
