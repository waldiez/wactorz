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
import {
    HANDLE_RADIUS,
    HANDLE_RADIUS_LIT,
    joinedTo,
    labelSpots,
    linePath,
} from "../../ui/wiring/wiringGraph";
import { modelLine, type GraphEdge } from "../../wiring/graph";
import type { Routed } from "../../wiring/layout";
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

/** What was drawn for the lines of the box `id`: paths or handles, by `kind`. */
function marksOf(root: HTMLElement, kind: "line" | "handle", id: string): SVGElement[] {
    return [...root.querySelectorAll<SVGElement>(`.af-wire-${kind}`)].filter(
        m => m.dataset["from"] === id || m.dataset["to"] === id,
    );
}

function routedTo(to: string, label: string, y2: number): Routed {
    const edge: GraphEdge = {
        from: `agent:${label}${y2}`,
        to,
        kind: "asks",
        problem: false,
        label,
        shadow: false,
    };
    return { edge, x1: 0, y1: 0, x2: 100, y2, backwards: false };
}

describe("the handles", () => {
    it("mark both ends of every line, a dot out and a ring in, in the line's own meaning", async () => {
        narrow(false);
        const { root } = await open();
        const lines = root.querySelectorAll(".af-wire-line");
        const handles = [...root.querySelectorAll(".af-wire-handle")];

        expect(handles).toHaveLength(lines.length * 2);
        expect(handles.filter(h => h.classList.contains("out"))).toHaveLength(lines.length);
        expect(handles.filter(h => h.classList.contains("in"))).toHaveLength(lines.length);
        expect(handles.every(h => h.getAttribute("r") === String(HANDLE_RADIUS))).toBe(true);
        // Drawing only, over the boxes.
        const layer = root.querySelector(".af-wire-handles")!;
        expect(layer.getAttribute("aria-hidden")).toBe("true");
        expect(layer.previousElementSibling?.classList.contains("af-wire-node")).toBe(true);
        // A reader missing a field has a problem's handles; what a trial publishes, a trial's.
        const comfort = marksOf(root, "handle", "agent:comfort");
        expect(comfort.length).toBeGreaterThan(0);
        expect(comfort.every(h => h.classList.contains("problem"))).toBe(true);
        const trial = marksOf(root, "handle", "topic:shadow/air/kitchen/score");
        expect(trial.map(h => h.classList.contains("shadow"))).toEqual([true, true]);
        expect(marksOf(root, "handle", "operator").every(h => h.classList.contains("asks"))).toBe(true);
    });

    it("light at both ends of a chosen box's lines, while what it is not joined to steps back", async () => {
        narrow(false);
        const { root } = await open();
        expect(root.querySelector(".af-wire-canvas")!.classList.contains("choosing")).toBe(false);

        node(root, "Topic air/kitchen/score").click();

        expect(root.querySelector(".af-wire-canvas")!.classList.contains("choosing")).toBe(true);
        const mine = marksOf(root, "handle", "topic:air/kitchen/score");
        // Published by one agent and read by one: two lines, four handles.
        expect(mine).toHaveLength(4);
        expect(mine.every(h => h.classList.contains("selected"))).toBe(true);
        expect(mine.every(h => h.getAttribute("r") === String(HANDLE_RADIUS_LIT))).toBe(true);
        const others = [...root.querySelectorAll(".af-wire-handle")].filter(
            h => !mine.includes(h as SVGElement),
        );
        expect(others.length).toBeGreaterThan(0);
        expect(others.some(h => h.classList.contains("selected"))).toBe(false);
        // The chosen box and the two it is joined to stay; the rest step back.
        const apart = (name: string): boolean => node(root, name).classList.contains("apart");
        expect([apart("Topic air/kitchen/score"), apart("Agent detect"), apart("Agent watch")]).toEqual([
            false,
            false,
            false,
        ]);
        expect([apart("Agent planner"), apart("Operator"), apart("Topic air/alarms")]).toEqual([
            true,
            true,
            true,
        ]);

        node(root, "Topic air/kitchen/score").click();

        expect(root.querySelector(".apart")).toBeNull();
        expect(root.querySelector(".af-wire-canvas")!.classList.contains("choosing")).toBe(false);
    });

    it("light for the box pointed at or holding the focus, and no longer once it is left", async () => {
        narrow(false);
        const { root } = await open();
        const planner = node(root, "Agent planner");
        const near = (): Element[] => [...root.querySelectorAll(".near")];

        planner.dispatchEvent(new Event("pointerenter"));

        // Its one line and that line's two handles.
        expect(near()).toHaveLength(3);
        expect(near()).toEqual(expect.arrayContaining(marksOf(root, "line", "agent:planner")));
        expect(near()).toEqual(expect.arrayContaining(marksOf(root, "handle", "agent:planner")));

        planner.dispatchEvent(new Event("pointerleave"));
        expect(near()).toHaveLength(0);

        planner.focus();
        expect(near()).toHaveLength(3);
        planner.blur();
        expect(near()).toHaveLength(0);
    });

    it("stay lit for a focused box the pointer leaves, and for a pointed box the focus leaves", async () => {
        narrow(false);
        const { root } = await open();
        const planner = node(root, "Agent planner");
        const near = (): Element[] => [...root.querySelectorAll(".near")];

        planner.focus();
        planner.dispatchEvent(new Event("pointerenter"));
        planner.dispatchEvent(new Event("pointerleave"));
        expect(document.activeElement).toBe(planner);
        expect(near()).toHaveLength(3);

        planner.dispatchEvent(new Event("pointerenter"));
        planner.blur();
        expect(near()).toHaveLength(3);

        planner.dispatchEvent(new Event("pointerleave"));
        expect(near()).toHaveLength(0);
    });

    it("light for the focused box and the one pointed at together", async () => {
        narrow(false);
        const { root } = await open();
        const planner = node(root, "Agent planner");
        const comfort = node(root, "Agent comfort");

        planner.focus();
        comfort.dispatchEvent(new Event("pointerenter"));

        const lit = [...marksOf(root, "line", "agent:planner"), ...marksOf(root, "line", "agent:comfort")];
        expect(lit.length).toBeGreaterThan(1);
        expect(lit.every(m => m.classList.contains("near"))).toBe(true);

        comfort.dispatchEvent(new Event("pointerleave"));

        expect(marksOf(root, "line", "agent:planner").every(m => m.classList.contains("near"))).toBe(true);
        expect(marksOf(root, "line", "agent:comfort").some(m => m.classList.contains("near"))).toBe(false);
    });

    it("stay lit for a box while it is dragged, its lines drawn again under it", async () => {
        narrow(false);
        const { root } = await open();
        const planner = node(root, "Agent planner");
        const before = marksOf(root, "handle", "agent:planner").map(h => h.getAttribute("cy"));

        planner.dispatchEvent(new Event("pointerenter"));
        planner.dispatchEvent(new MouseEvent("pointerdown", { button: 0, clientX: 0, clientY: 0 }));
        planner.dispatchEvent(new MouseEvent("pointermove", { clientX: 0, clientY: 200 }));

        const during = marksOf(root, "handle", "agent:planner");
        expect(during.map(h => h.getAttribute("cy"))).not.toEqual(before);
        expect(during.every(h => h.classList.contains("near"))).toBe(true);
        planner.dispatchEvent(new MouseEvent("pointerup", { clientX: 0, clientY: 200 }));
    });

    it("are named in the legend", async () => {
        narrow(false);
        const { root } = await open();

        const legend = root.querySelector('[aria-label="Legend"]')!.textContent;
        expect(legend).toContain("leaves");
        expect(legend).toContain("arrives");
    });
});

describe("what a line says", () => {
    it("is written above the handle it arrives at, once for each box it is said of", () => {
        const first = routedTo("operator", "dashboard", 100);
        const same = routedTo("operator", "dashboard", 140);
        const elsewhere = routedTo("agent:other", "dashboard", 300);
        const silent = routedTo("operator", "", 50);

        const spots = labelSpots([first, same, elsewhere, silent]);

        expect([...spots.keys()]).toEqual([first, elsewhere]);
        expect(spots.get(first)).toBeLessThan(100);
        expect(spots.get(elsewhere)).toBeLessThan(300);
    });

    it("moves up, one label above the next, where handles are too close to be read apart", () => {
        const [low, mid, high] = [
            routedTo("operator", "telegram", 106),
            routedTo("operator", "dashboard", 103),
            routedTo("operator", "whatsapp", 40),
        ];

        const spots = labelSpots([low, mid, high]);

        expect(spots.get(low)).toBe(100);
        // Too close to the one below it: moved up a row. The far one stays by its own handle.
        expect(spots.get(mid)).toBe(87);
        expect(spots.get(high)).toBe(34);
    });

    it("joins a chosen box to both ends of its lines, and nothing to no choice", () => {
        const routed = [routedTo("operator", "x", 1), routedTo("agent:other", "y", 2)];
        routed[1]!.edge.from = "operator";

        expect(joinedTo(routed, "")).toEqual(new Set());
        expect(joinedTo(routed, "operator")).toEqual(new Set(["operator", "agent:x1", "agent:other"]));
    });
});

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
