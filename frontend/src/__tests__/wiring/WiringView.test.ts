/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The Wiring view, as a person uses it: the graph and its warnings, choosing
 * a node by pointer or keyboard and reading its details, the list in its
 * place, the choices that hide and focus, and what it says when the wiring
 * cannot be read. Elements are found by role and name, as a person finds them.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Answer } from "../../io/api";
import type { Wiring } from "../../types/wiring";
import { WiringData } from "../../ui/wiring/wiringData";
import { KEY_STEP } from "../../ui/wiring/wiringDrag";
import { WiringPins } from "../../ui/wiring/wiringPins";
import { statusText, warningLabel, WiringView } from "../../ui/wiring/WiringView";
import { fieldsText, payloadText, warningSentence } from "../../ui/wiring/wiringDetail";
import { linePath } from "../../ui/wiring/wiringGraph";
import { modelLine } from "../../wiring/graph";
import { allByRole, byRole, nameOf } from "../queries";
import { agent, topic, wiring } from "./fixtures";

const views: WiringView[] = [];

async function open(
    answer: Answer<Wiring> = { ok: true, value: wiring() },
): Promise<{ root: HTMLElement; data: WiringData }> {
    const data = new WiringData({ isShowing: () => true }, () => Promise.resolve(answer));
    const view = new WiringView(data);
    views.push(view);
    const root = view.build();
    document.body.append(root);
    await vi.waitFor(() => expect(data.state.reading).toBe(false));
    return { root, data };
}

function narrow(on: boolean): void {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: on, media: query }));
}

const node = (root: HTMLElement, name: string): HTMLElement =>
    allByRole(root, "button").find(b => nameOf(b).includes(name))!;

afterEach(() => {
    localStorage.clear();
    views.splice(0).forEach(v => v.destroy());
    document.body.replaceChildren();
    vi.unstubAllGlobals();
});

describe("the Wiring view", () => {
    it("draws every node as a named button, and counts the warnings", async () => {
        narrow(false);
        const { root } = await open();

        expect(byRole(root, "heading", "Wiring")).toBeTruthy();
        for (const label of ["1 field mismatch", "1 topic nobody reads", "1 subscription with no producer"]) {
            expect(byRole(root, "button", label)).toBeTruthy();
        }
        expect(nameOf(node(root, "Agent detect"))).toContain("air-quality: active v1 · shadow v2");
        expect(nameOf(node(root, "Topic air/alarms"))).toContain("nobody reads this");
        expect(nameOf(node(root, "Agent comfort"))).toContain("missing: temperature_c");
        expect(nameOf(node(root, "Agent comfort"))).toContain("1 warning");
        expect(nameOf(node(root, "Topic shadow/air/kitchen/score"))).toContain("on trial");
        expect(node(root, "Operator")).toBeTruthy();
        expect(root.querySelectorAll(".af-wire-line").length).toBeGreaterThan(0);
        expect(root.querySelector(".af-wire-lines")?.getAttribute("aria-hidden")).toBe("true");
    });

    it("hides the system's own agents until asked to show them", async () => {
        narrow(false);
        const { root } = await open();
        expect(node(root, "Agent main")).toBeUndefined();

        const box = byRole(root, "checkbox", "System agents") as HTMLInputElement;
        box.click();

        expect(node(root, "Agent main")).toBeTruthy();
        expect(nameOf(node(root, "Topic homeassistant/#"))).toBeTruthy();
    });

    it("selects a node and says what it carries, keeping the focus on it across the redraw", async () => {
        narrow(false);
        const { root } = await open();
        const kitchen = node(root, "Topic air/kitchen/score");

        // A real button answers Enter and Space itself; the browser journey presses them.
        kitchen.focus();
        kitchen.click();

        const again = node(root, "Topic air/kitchen/score");
        expect(again.getAttribute("aria-pressed")).toBe("true");
        expect(document.activeElement).toBe(again);
        const detail = byRole(root, "region", "Selected");
        expect(detail.textContent).toContain("Published bydetect");
        expect(detail.textContent).toContain("Read bywatch");
        expect(detail.textContent).toContain('"score": 6.1');
    });

    it("says a mismatch in words beside the agent, and links an open question to the Inbox", async () => {
        narrow(false);
        const { root } = await open();

        node(root, "Agent comfort").click();
        expect(byRole(root, "region", "Selected").textContent).toContain(
            "comfort reads temperature_c, but messages on sensors/air/kitchen carry temperature.",
        );
        node(root, "Agent watch").click();
        expect(byRole(root, "link", "Answer in the Inbox").getAttribute("href")).toBe("#/inbox");
        node(root, "Agent planner").click();
        expect(byRole(root, "region", "Selected").textContent).toContain("plan: active not set");
    });

    it("says what a source groups, who waits on a person, and an agent's own words", async () => {
        narrow(false);
        const told = wiring();
        told.agents[0]!.description = "Scores each room.";
        told.agents[2]!.asks = { open: 0, answered: 4, channels: [] };
        const { root } = await open({ ok: true, value: told });

        node(root, "Outside source sensors").click();
        expect(byRole(root, "region", "Selected").textContent).toContain(
            "Topics no agent publishes under sensors/: sensors/air/kitchen.",
        );
        node(root, "Operator").click();
        expect(byRole(root, "region", "Selected").textContent).toContain("Waiting on a person: watch.");
        node(root, "Agent detect").click();
        const detail = byRole(root, "region", "Selected").textContent ?? "";
        expect(detail).toContain("DoesScores each room.");
        expect(detail).toContain("Model (model)air-quality: active v1 · shadow v2");
        node(root, "Agent comfort").click();
        expect(byRole(root, "region", "Selected").textContent).toContain("Asked a person0 open, 4 answered");
        expect(byRole(root, "region", "Selected").textContent).toContain("Reads fieldstemperature_c");
        node(root, "Topic sensors/air/kitchen").click();
        expect(byRole(root, "region", "Selected").textContent).toContain("Published byoutside the system");
        expect(byRole(root, "region", "Selected").textContent).toContain("… (cut)");
        node(root, "Topic shadow/air/kitchen/score").click();
        expect(byRole(root, "region", "Selected").textContent).toContain("On trial");
        // Choosing the selection again lets it go.
        node(root, "Topic shadow/air/kitchen/score").click();
        expect(byRole(root, "region", "Selected").textContent).toContain("Select an agent or a topic");
    });

    it("shows a warning kind alone, with what is joined to it, and back again", async () => {
        narrow(false);
        const { root } = await open();
        const chip = byRole(root, "button", "1 field mismatch");

        chip.click();

        expect(byRole(root, "button", "1 field mismatch").getAttribute("aria-pressed")).toBe("true");
        expect(node(root, "Topic air/alarms")).toBeUndefined();
        expect(node(root, "Topic sensors/air/kitchen")).toBeTruthy();
        byRole(root, "button", "1 field mismatch").click();
        expect(node(root, "Topic air/alarms")).toBeTruthy();
    });

    it("hides what comes from outside when asked", async () => {
        narrow(false);
        const { root } = await open();

        (byRole(root, "checkbox", "Outside sources") as HTMLInputElement).click();

        expect(node(root, "Topic sensors/air/kitchen")).toBeUndefined();
        expect(node(root, "Outside source sensors")).toBeUndefined();
    });

    it("groups by machine, with a band for each", async () => {
        narrow(false);
        const { root } = await open();

        byRole(root, "button", "Machine").click();

        expect([...root.querySelectorAll(".af-wire-lane-name")].map(n => n.textContent)).toEqual([
            "outside",
            "edge",
            "main",
        ]);
        expect(byRole(root, "button", "Machine").getAttribute("aria-pressed")).toBe("true");
    });

    it("shows the list in place of the graph, never both, and selects from it", async () => {
        narrow(false);
        const { root } = await open();

        byRole(root, "button", "View as list").click();

        expect(root.querySelector(".af-wire-frame")).toBeNull();
        expect(byRole(root, "list", "Warnings")).toBeTruthy();
        const row = byRole(root, "button", "detect → air/kitchen/score → watch");
        row.click();
        expect(
            byRole(root, "button", "detect → air/kitchen/score → watch").getAttribute("aria-pressed"),
        ).toBe("true");
        expect(byRole(root, "list", "Agents").textContent).toContain("detect on edge");
        byRole(root, "button", "View as graph").click();
        expect(root.querySelector(".af-wire-list")).toBeNull();
    });

    it("names in the list and the details only the agents the graph shows", async () => {
        narrow(false);
        const hidden = {
            agents: [
                agent("main", { publishes: ["x"] }),
                agent("app", { subscribes: ["x"] }),
                agent("solo", {}),
            ],
            topics: [
                topic("x", { producers: ["main"], consumers: ["app"] }),
                topic("main/only", { producers: ["main"] }),
            ],
            sources: [],
            warnings: [{ kind: "unread" as const, topic: "main/only", producers: ["main"] }],
        };
        const { root } = await open({ ok: true, value: hidden });

        byRole(root, "button", "View as list").click();

        expect(byRole(root, "button", "hidden → x → app")).toBeTruthy();
        expect(root.textContent).not.toContain("main → x");
        expect(root.querySelector('[aria-label="Warnings"]')).toBeNull();
        byRole(root, "button", "hidden → x → app").click();
        expect(byRole(root, "region", "Selected").textContent).toContain("Published by1 hidden");
        // The count above is of the whole wiring, hidden or not.
        expect(byRole(root, "button", "1 topic nobody reads")).toBeTruthy();
    });

    it("swaps graph and list at once when the screen narrows or widens", async () => {
        const listeners = new Set<() => void>();
        const media = {
            matches: false,
            addEventListener: (_: string, f: () => void) => listeners.add(f),
            removeEventListener: (_: string, f: () => void) => listeners.delete(f),
        };
        vi.stubGlobal("matchMedia", () => media);
        const { root } = await open();
        expect(root.querySelector(".af-wire-frame")).not.toBeNull();

        media.matches = true;
        listeners.forEach(f => f());
        expect(root.querySelector(".af-wire-frame")).toBeNull();
        expect(root.querySelector(".af-wire-list")).not.toBeNull();

        views.splice(0).forEach(v => v.destroy());
        expect(listeners.size).toBe(0);
    });

    it("shows only the list on a narrow screen", async () => {
        narrow(true);
        const { root } = await open();

        expect(root.querySelector(".af-wire-frame")).toBeNull();
        expect(root.querySelector(".af-wire-list")).not.toBeNull();
        expect(allByRole(root, "button").map(nameOf)).not.toContain("View as list");
    });

    it("says why there is nothing to show when the server has no registry", async () => {
        const { root } = await open({ ok: false, error: "no actor system runs here", status: 503 });

        expect(root.querySelector(".af-wire-status")?.textContent).toBe(
            "No wiring to show: no actor system runs here.",
        );
        expect(allByRole(root, "button").map(nameOf)).toEqual(["Refresh"]);
    });

    it("keeps the graph read before when reading again fails, and says it is stale", async () => {
        narrow(false);
        const answers: Answer<Wiring>[] = [
            { ok: true, value: wiring() },
            { ok: false, error: "gone", status: 502 },
        ];
        const data = new WiringData({ isShowing: () => true }, () => Promise.resolve(answers.shift()!));
        const view = new WiringView(data);
        views.push(view);
        const root = view.build();
        await vi.waitFor(() => expect(data.state.wiring).not.toBeNull());

        byRole(root, "button", "Refresh").click();
        await vi.waitFor(() => expect(data.state.error).toBe("gone"));

        expect(root.querySelector(".af-wire-status")?.textContent).toMatch(
            /^Could not read it again: gone\. Showing what was read/,
        );
        expect(node(root, "Agent detect")).toBeTruthy();
    });

    it("says what to do when nothing is drawn", async () => {
        narrow(false);
        const onlyMain = {
            ...wiring(),
            agents: wiring().agents.filter(a => a.name === "main"),
            warnings: [],
            topics: [],
        };
        const { root } = await open({ ok: true, value: onlyMain });

        expect(root.textContent).toContain("Only the system's own agents are running.");
        const { root: empty } = await open({
            ok: true,
            value: { agents: [], topics: [], sources: [], warnings: [] },
        });
        expect(empty.textContent).toContain("No agent publishes or reads anything yet.");
    });
});

function press(box: HTMLElement, type: string, x: number, y: number): void {
    box.dispatchEvent(
        new PointerEvent(type, { clientX: x, clientY: y, button: 0, bubbles: true, pointerId: 1 }),
    );
}

describe("moving a box", () => {
    it("drags it, keeps it there across a redraw, and does not choose it", async () => {
        narrow(false);
        const { root } = await open();
        const before = node(root, "Agent planner");
        const left = parseFloat(before.style.left);

        press(before, "pointerdown", 100, 100);
        press(before, "pointermove", 160, 140);
        expect(before.classList.contains("dragging")).toBe(true);
        press(before, "pointerup", 160, 140);
        before.click();

        const after = node(root, "Agent planner");
        expect(parseFloat(after.style.left)).toBe(left + 60);
        expect(after.classList.contains("pinned")).toBe(true);
        expect(after.getAttribute("aria-pressed")).toBe("false");
        expect(new WiringPins().of(false).get("agent:planner")).toBeDefined();
    });

    it("takes a press that hardly moves as a click", async () => {
        narrow(false);
        const { root } = await open();
        const box = node(root, "Agent planner");

        press(box, "pointerdown", 100, 100);
        press(box, "pointermove", 102, 101);
        press(box, "pointerup", 102, 101);
        box.click();

        expect(node(root, "Agent planner").getAttribute("aria-pressed")).toBe("true");
        expect(new WiringPins().any(false)).toBe(false);
    });

    it("steps it with Alt and an arrow key, keeping the focus on it", async () => {
        narrow(false);
        const { root } = await open();
        const box = node(root, "Agent planner");
        const top = parseFloat(box.style.top);
        box.focus();

        box.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", altKey: true, bubbles: true }));
        box.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));

        const moved = node(root, "Agent planner");
        expect(parseFloat(moved.style.top)).toBe(top + KEY_STEP);
        expect(document.activeElement).toBe(moved);
        expect(moved.getAttribute("aria-keyshortcuts")).toContain("Alt+ArrowDown");
    });

    it("puts every box back with Reset layout, which shows only while one was moved", async () => {
        narrow(false);
        const { root } = await open();
        expect(allByRole(root, "button").map(nameOf)).not.toContain("Reset layout");
        const box = node(root, "Agent planner");
        const top = parseFloat(box.style.top);
        box.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", altKey: true, bubbles: true }));

        byRole(root, "button", "Reset layout").click();

        expect(parseFloat(node(root, "Agent planner").style.top)).toBe(top);
        expect(allByRole(root, "button").map(nameOf)).not.toContain("Reset layout");
        expect(new WiringPins().any(false)).toBe(false);
    });
});

describe("what the view says", () => {
    it("of each kind of warning, in the singular and the plural", () => {
        expect(warningLabel("field_mismatch", 2)).toBe("2 field mismatches");
        expect(warningLabel("unread", 3)).toBe("3 topics nobody reads");
        expect(warningLabel("no_producer", 2)).toBe("2 subscriptions with no producer");
    });

    it("of where the reading stands", () => {
        const base = { wiring: null, error: "", readAt: null, reading: false } as const;
        expect(statusText({ ...base, status: "loading", reading: true })).toBe("Reading the wiring…");
        expect(statusText({ ...base, status: "error", error: "timed out" })).toBe(
            "Could not read the wiring: timed out",
        );
        expect(
            statusText({ ...base, status: "ready", wiring: wiring(), readAt: Date.now(), reading: true }),
        ).toBe("Reading again…");
        expect(statusText({ ...base, status: "ready", wiring: wiring(), readAt: Date.now() })).toBe(
            "Read now.",
        );
    });

    it("of a topic's fields and payload, whatever is known of them", () => {
        expect(fieldsText(topic("x"))).toBe("nothing seen yet");
        expect(fieldsText(topic("homeassistant/#", { sampled: false }))).toBe(
            "unknown: main keeps no payloads from homeassistant/",
        );
        expect(fieldsText(topic("x", { fields: { b: "int", a: "str" }, fields_from: "declared" }))).toBe(
            "a, b (declared, nothing seen yet)",
        );
        expect(payloadText(topic("x"))).toBeNull();
        expect(payloadText(topic("x", { example_text: '{"a": 1', example_cut: true }))).toBe(
            '{"a": 1… (cut)',
        );
    });

    it("of each warning, as a sentence", () => {
        const name = (id: string) => id.toUpperCase();
        const [unread, mismatch, none] = wiring().warnings;
        expect(warningSentence(unread!, name)).toBe("Nobody reads air/alarms: DETECT publishes it.");
        expect(warningSentence(none!, name)).toBe(
            "PLANNER reads plans/today, which nothing publishes and nothing was seen on.",
        );
        expect(
            warningSentence(
                {
                    ...mismatch!,
                    kind: "field_mismatch",
                    carried: [],
                    carried_from: "declared",
                } as typeof mismatch & object,
                name,
            ),
        ).toBe("COMFORT reads temperature_c, but the producer of sensors/air/kitchen declares no fields.");
    });

    it("of a model, and the path of a line running back", () => {
        expect(modelLine({ name: "m", active: 3, shadow: null })).toBe("m: active v3");
        expect(linePath({ x1: 0, y1: 0, x2: 100, y2: 0, backwards: true })).toContain("C 80 60");
    });
});
