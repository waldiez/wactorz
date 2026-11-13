/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The view of the wiring's drawing: fitted in the middle of its frame until
 * a person zooms or moves it, fitted again when agents come and go or the
 * frame changes size, a view set by hand kept in this browser until Fit and
 * never with the drawing out of sight, and zooming and moving about by the
 * controls, the wheel and a drag on the background.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Answer } from "../../io/api";
import { safeStorage } from "../../safeStorage";
import type { Wiring } from "../../types/wiring";
import { WiringData } from "../../ui/wiring/wiringData";
import { WiringView } from "../../ui/wiring/WiringView";
import {
    clampZoom,
    FIT_FLOOR,
    fitScale,
    fitView,
    IN_VIEW,
    keptInView,
    MAX_ZOOM,
    MIN_ZOOM,
    parseViews,
    VIEW_KEY,
    WiringZoom,
    ZOOM_STEP,
    type View,
} from "../../ui/wiring/wiringZoom";
import { allByRole, byRole, nameOf } from "../queries";
import { agent, topic, wiring } from "./fixtures";

const views: WiringView[] = [];
/** What the frame's size is watched with, in place of the browser's: called by hand. */
let resized: (() => void)[] = [];
/** What waits for the next frame, in place of the browser's: run by hand. */
let frames: (() => void)[] = [];

/** The frame changed size, and the next frame came. */
function resize(): void {
    resized.forEach(changed => changed());
    nextFrame();
}

function nextFrame(): void {
    frames.splice(0).forEach(run => run());
}

/** Give every element a size, as a browser's layout would: the frame is what asks. */
function frameSize(width: number, height: number): void {
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(width);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(height);
}

async function open(
    served: { wiring: Wiring } = { wiring: wiring() },
    narrow = false,
): Promise<{ root: HTMLElement; data: WiringData }> {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: narrow, media: query }));
    vi.stubGlobal(
        "ResizeObserver",
        class {
            constructor(private readonly _changed: () => void) {}
            observe(): void {
                resized.push(this._changed);
            }
            disconnect(): void {
                resized = resized.filter(r => r !== this._changed);
            }
        },
    );
    vi.stubGlobal("requestAnimationFrame", (run: () => void) => frames.push(run));
    vi.stubGlobal("cancelAnimationFrame", () => frames.splice(0));
    const answer = (): Promise<Answer<Wiring>> => Promise.resolve({ ok: true, value: served.wiring });
    const data = new WiringData({ isShowing: () => true }, answer);
    const view = new WiringView(data);
    views.push(view);
    const root = view.build();
    document.body.append(root);
    await vi.waitFor(() => expect(data.state.reading).toBe(false));
    return { root, data };
}

/** A wiring with `count` more topics published by the detector: a taller drawing. */
function taller(count: number): Wiring {
    const more = wiring();
    for (let i = 0; i < count; i++) {
        more.topics.push(topic(`extra/${i}`, { producers: ["detect"], consumers: ["watch"] }));
    }
    more.agents.push(agent("late", { subscribes: ["extra/0"] }));
    return more;
}

// A drawing's parts are found by class: they are drawing only, with no role or name to find them by.
const frameOf = (root: HTMLElement): HTMLElement => root.querySelector<HTMLElement>(".af-wire-frame")!;
const canvasOf = (root: HTMLElement): HTMLElement => root.querySelector<HTMLElement>(".af-wire-canvas")!;
const px = (value: string): number => Number.parseFloat(value);

/** The view the drawing is shown at, read off the drawing itself. */
function viewOf(root: HTMLElement): View {
    const [x, y, scale] = (canvasOf(root).style.transform.match(/-?[\d.]+/g) ?? []).map(Number);
    return { x: x!, y: y!, scale: scale! };
}

/** The drawing's own size. */
function sizeOf(root: HTMLElement): { width: number; height: number } {
    return { width: px(canvasOf(root).style.width), height: px(canvasOf(root).style.height) };
}

/** The place in the drawing that `view` shows at `x`, `y` of the frame; and where in the frame it shows a place. */
const placeAt = (view: View, x: number, y: number): { x: number; y: number } => ({
    x: (x - view.x) / view.scale,
    y: (y - view.y) / view.scale,
});
const seenAt = (view: View, place: { x: number; y: number }): { x: number; y: number } => ({
    x: place.x * view.scale + view.x,
    y: place.y * view.scale + view.y,
});

const shownAt = (root: HTMLElement): number => viewOf(root).scale;
const level = (root: HTMLElement): string => byRole(root, "group", "Zoom").querySelector("span")!.textContent;
const fitPressed = (root: HTMLElement): string | null =>
    byRole(root, "button", "Fit").getAttribute("aria-pressed");
const node = (root: HTMLElement, name: string): HTMLElement =>
    allByRole(root, "button").find(b => nameOf(b).includes(name))!;

/** Turn the wheel over `frame`; says whether the page was kept from taking it, as its own zoom or scroll. */
function wheel(frame: HTMLElement, turned: { deltaY: number; deltaMode?: number }, ctrlKey = false): boolean {
    const event = new WheelEvent("wheel", { cancelable: true });
    // Set on the event itself: the test DOM's wheel event takes neither the key nor the place from what it is made with.
    const said = { deltaMode: 0, ...turned, ctrlKey, clientX: 100, clientY: 50 };
    for (const [name, value] of Object.entries(said)) {
        Object.defineProperty(event, name, { value });
    }
    const kept = vi.spyOn(event, "preventDefault");
    frame.dispatchEvent(event);
    return kept.mock.calls.length > 0;
}

function press(on: HTMLElement, type: string, x: number, y: number): void {
    on.dispatchEvent(
        new PointerEvent(type, { clientX: x, clientY: y, button: 0, bubbles: true, pointerId: 1 }),
    );
}

/** Say where `element` stands on the screen, as a browser's layout would. */
function standsAt(element: Element, left: number, top: number, width: number, height: number): void {
    vi.spyOn(element, "getBoundingClientRect").mockReturnValue(new DOMRect(left, top, width, height));
}

afterEach(() => {
    localStorage.clear();
    views.splice(0).forEach(v => v.destroy());
    document.body.replaceChildren();
    resized = [];
    frames = [];
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
});

describe("the scale that fits", () => {
    it("is the drawing's own size when it fits, and never larger", () => {
        expect(fitScale({ width: 1000, height: 800 }, { width: 500, height: 400 })).toBe(1);
        expect(fitScale({ width: 1000, height: 800 }, { width: 1000, height: 800 })).toBe(1);
    });

    it("is what shows the whole drawing, by whichever side is tighter, rounded down", () => {
        expect(fitScale({ width: 800, height: 600 }, { width: 1000, height: 600 })).toBe(0.8);
        expect(fitScale({ width: 800, height: 300 }, { width: 1000, height: 400 })).toBe(0.75);
        expect(fitScale({ width: 2, height: 600 }, { width: 3, height: 600 })).toBe(0.66);
    });

    it("stops where words could no longer be read, and fits anything in a frame with no size yet", () => {
        expect(fitScale({ width: 100, height: 100 }, { width: 1000, height: 1000 })).toBe(FIT_FLOOR);
        expect(fitScale({ width: 0, height: 0 }, { width: 1000, height: 1000 })).toBe(1);
        expect(fitScale({ width: 500, height: 500 }, { width: 0, height: 0 })).toBe(1);
    });
});

describe("the view that fits", () => {
    const frame = { width: 1000, height: 600 };

    it("stands the drawing at the frame's own corner, where it begins, whatever room is left over", () => {
        expect(fitView(frame, { left: 0, top: 0, width: 400, height: 200 })).toEqual({
            scale: 1,
            x: 0,
            y: 0,
        });
        expect(fitView(frame, { left: 0, top: 0, width: 2000, height: 400 })).toEqual({
            scale: 0.5,
            x: 0,
            y: 0,
        });
        expect(fitView(frame, { left: 0, top: 0, width: 4000, height: 3000 })).toEqual({
            scale: FIT_FLOOR,
            x: 0,
            y: 0,
        });
    });

    it("counts what stands left of and above the drawing's corner, which is then further in", () => {
        expect(fitView(frame, { left: -100, top: -50, width: 400, height: 200 })).toEqual({
            scale: 1,
            x: 100,
            y: 50,
        });
        // 2000 wide in all, at half size: its corner half of 1000 in.
        expect(fitView(frame, { left: -1000, top: 0, width: 1000, height: 400 })).toEqual({
            scale: 0.5,
            x: 500,
            y: 0,
        });
    });
});

describe("a view kept in sight", () => {
    const frame = { width: 1000, height: 600 };
    const drawing = { left: 0, top: 0, width: 400, height: 200 };

    it("is left as it is while enough of the drawing shows", () => {
        const view = { scale: 1, x: -300, y: 500 };
        expect(keptInView(view, frame, drawing)).toEqual(view);
        expect(keptInView(view, { width: 0, height: 0 }, drawing)).toEqual(view);
    });

    it("is moved back until part of the drawing shows, along each side", () => {
        expect(keptInView({ scale: 1, x: -5000, y: -5000 }, frame, drawing)).toEqual({
            scale: 1,
            x: IN_VIEW - 400,
            y: IN_VIEW - 200,
        });
        expect(keptInView({ scale: 1, x: 5000, y: 5000 }, frame, drawing)).toEqual({
            scale: 1,
            x: 1000 - IN_VIEW,
            y: 600 - IN_VIEW,
        });
    });

    it("keeps the whole of a drawing smaller than what is kept, and counts its scale and where it begins", () => {
        const small = { left: 0, top: 0, width: 40, height: 20 };
        expect(keptInView({ scale: 1, x: -5000, y: 5000 }, frame, small)).toEqual({ scale: 1, x: 0, y: 580 });
        // At half size a 400-wide drawing is 200 on the screen; from -100 it begins 50 before its corner.
        const before = { left: -100, top: 0, width: 400, height: 200 };
        expect(keptInView({ scale: 0.5, x: -5000, y: 0 }, frame, drawing).x).toBe(IN_VIEW - 200);
        expect(keptInView({ scale: 0.5, x: 5000, y: 0 }, frame, before).x).toBe(1000 - IN_VIEW + 50);
    });
});

describe("a view kept in storage", () => {
    it("is held within what a person may set, to the hundredth", () => {
        expect(clampZoom(0.01)).toBe(MIN_ZOOM);
        expect(clampZoom(9)).toBe(MAX_ZOOM);
        expect(clampZoom(1.23456)).toBe(1.23);
    });

    it("is read whatever junk is there", () => {
        expect(parseViews(null)).toEqual({});
        expect(parseViews("not json")).toEqual({});
        expect(parseViews("null")).toEqual({});
        expect(
            parseViews(
                JSON.stringify({ none: { scale: 1.5, x: 10.4, y: -20.6 }, machine: 2, other: { scale: 1 } }),
            ),
        ).toEqual({ none: { scale: 1.5, x: 10, y: -21 } });
        expect(
            parseViews(
                JSON.stringify({ none: { scale: -1, x: 0, y: 0 }, machine: { scale: 99, x: 0, y: 0 } }),
            ),
        ).toEqual({ machine: { scale: MAX_ZOOM, x: 0, y: 0 } });
        expect(parseViews(JSON.stringify({ none: { scale: 1, x: "0", y: 0 }, machine: null }))).toEqual({});
    });
});

describe("the drawing's view", () => {
    it("shows a drawing that fits at its own size, from the frame's corner, with Fit pressed", async () => {
        frameSize(4000, 3000);
        const { root } = await open();

        expect(viewOf(root)).toEqual({ scale: 1, x: 0, y: 0 });
        expect(level(root)).toBe("100%");
        expect(fitPressed(root)).toBe("true");
        expect(safeStorage.get(VIEW_KEY)).toBeNull();
    });

    it("opens a drawing larger than its frame smaller, so the whole of it is seen", async () => {
        frameSize(1200, 4000);
        const { root } = await open();
        const { scale, x } = viewOf(root);
        const { width } = sizeOf(root);

        expect(width).toBeGreaterThan(1200);
        expect(scale).toBeLessThan(1);
        expect(scale).toBeGreaterThan(FIT_FLOOR);
        expect(x).toBeGreaterThanOrEqual(0);
        expect(x + width * scale).toBeLessThanOrEqual(1200);
        expect(level(root)).toBe(`${Math.round(scale * 100)}%`);
    });

    it("fits again when the frame changes size", async () => {
        frameSize(1000, 4000);
        const { root } = await open();
        const before = shownAt(root);

        frameSize(1400, 4000);
        // Told several times before the next frame, it is fitted once, in that frame: never as it is told.
        resized.forEach(changed => changed());
        resized.forEach(changed => changed());
        expect(shownAt(root)).toBe(before);
        expect(frames).toHaveLength(1);
        nextFrame();

        expect(shownAt(root)).toBeGreaterThan(before);
        expect(viewOf(root).x + sizeOf(root).width * shownAt(root)).toBeLessThanOrEqual(1400);

        // A frame too small to show it readably: no smaller than the floor, from the frame's corner.
        frameSize(200, 4000);
        resize();

        expect(viewOf(root)).toMatchObject({ scale: FIT_FLOOR, x: 0 });
    });

    it("fits again when agents come, and when they go", async () => {
        frameSize(4000, 420);
        const served = { wiring: wiring() };
        const { root, data } = await open(served);
        const before = viewOf(root);

        served.wiring = taller(8);
        await data.refresh();

        const grown = viewOf(root);
        expect(node(root, "Agent late")).toBeTruthy();
        expect(grown.scale).toBeLessThan(before.scale);
        expect(grown.y + sizeOf(root).height * grown.scale).toBeLessThanOrEqual(420);

        served.wiring = wiring();
        await data.refresh();

        expect(viewOf(root)).toEqual(before);
    });

    it("zooms by the buttons, about the middle of the frame, and keeps what was set in this browser", async () => {
        // A frame no taller than keeps the drawing's middle in it: nothing is held back to keep it in sight.
        frameSize(800, 200);
        const { root } = await open();
        const before = viewOf(root);

        byRole(root, "button", "Zoom in").click();

        const after = viewOf(root);
        expect(after.scale).toBe(clampZoom(before.scale * ZOOM_STEP));
        expect(fitPressed(root)).toBe("false");
        expect(parseViews(safeStorage.get(VIEW_KEY))).toEqual({ none: after });
        // What was at the middle of the frame is still there, to the pixel a view is kept at.
        expect(Math.abs(seenAt(after, placeAt(before, 400, 100)).x - 400)).toBeLessThanOrEqual(1);
        expect(Math.abs(seenAt(after, placeAt(before, 400, 100)).y - 100)).toBeLessThanOrEqual(1);

        byRole(root, "button", "Zoom out").click();
        byRole(root, "button", "Zoom out").click();
        expect(shownAt(root)).toBeLessThan(before.scale);

        // A new visit opens as it was left.
        const again = await open();
        expect(viewOf(again.root)).toEqual(viewOf(root));
    });

    it("keeps a view set by hand whatever the drawing becomes, until Fit", async () => {
        frameSize(4000, 420);
        const served = { wiring: wiring() };
        const { root, data } = await open(served);
        byRole(root, "button", "Zoom in").click();
        press(canvasOf(root), "pointerdown", 150, 150);
        press(frameOf(root), "pointermove", 120, 110);
        press(frameOf(root), "pointerup", 120, 110);
        const set = viewOf(root);

        served.wiring = taller(8);
        await data.refresh();

        expect(viewOf(root)).toEqual(set);
        expect(fitPressed(root)).toBe("false");

        // A read that changes nothing moves nothing.
        await data.refresh();
        expect(viewOf(root)).toEqual(set);

        byRole(root, "button", "Fit").click();

        expect(shownAt(root)).toBeLessThan(set.scale);
        expect(viewOf(root).y + sizeOf(root).height * shownAt(root)).toBeLessThanOrEqual(420);
        expect(fitPressed(root)).toBe("true");
        expect(parseViews(safeStorage.get(VIEW_KEY))).toEqual({});
    });

    it("keeps a view for each grouping", async () => {
        frameSize(4000, 4000);
        const { root } = await open();
        byRole(root, "button", "Zoom in").click();
        expect(shownAt(root)).toBe(ZOOM_STEP);

        byRole(root, "button", "Machine").click();
        expect(shownAt(root)).toBe(1);
        expect(fitPressed(root)).toBe("true");

        byRole(root, "button", "None").click();
        expect(shownAt(root)).toBe(ZOOM_STEP);
    });

    it("stops at the smallest and the largest, and says so on the buttons", async () => {
        frameSize(4000, 4000);
        const { root } = await open();
        const zoomIn = byRole(root, "button", "Zoom in") as HTMLButtonElement;
        const out = byRole(root, "button", "Zoom out") as HTMLButtonElement;

        for (let i = 0; i < 6; i++) {
            zoomIn.click();
        }
        expect(shownAt(root)).toBe(MAX_ZOOM);
        expect([zoomIn.disabled, out.disabled]).toEqual([true, false]);

        for (let i = 0; i < 12; i++) {
            out.click();
        }
        expect(shownAt(root)).toBe(MIN_ZOOM);
        expect([zoomIn.disabled, out.disabled]).toEqual([false, true]);
        expect(level(root)).toBe("30%");
    });

    it("zooms by the slider between the buttons, which follows the zoom however it is set", async () => {
        frameSize(4000, 4000);
        const { root } = await open();
        const slider = byRole(root, "slider", "Zoom level") as HTMLInputElement;
        expect([slider.min, slider.max, slider.value]).toEqual(["30", "200", "100"]);
        expect(slider.getAttribute("aria-valuetext")).toBe("100%");

        slider.value = "150";
        slider.dispatchEvent(new Event("input"));

        expect(shownAt(root)).toBe(1.5);
        expect(level(root)).toBe("150%");
        expect(parseViews(safeStorage.get(VIEW_KEY)).none?.scale).toBe(1.5);
        // The same slider, still in the page: a drag on it is not cut short by a redraw.
        expect(byRole(root, "slider", "Zoom level")).toBe(slider);

        byRole(root, "button", "Zoom out").click();
        expect(slider.value).toBe("120");
        byRole(root, "button", "Fit").click();
        expect(slider.value).toBe("100");
        expect(slider.getAttribute("aria-valuetext")).toBe("100%");
    });

    it("zooms about the pointer with the wheel, and never lets the page take the wheel", async () => {
        frameSize(800, 600);
        const { root } = await open();
        const frame = frameOf(root);
        const before = viewOf(root);

        expect(wheel(frame, { deltaY: -100 })).toBe(true);

        const after = viewOf(root);
        expect(after.scale).toBeGreaterThan(before.scale);
        expect(fitPressed(root)).toBe("false");
        expect(parseViews(safeStorage.get(VIEW_KEY))).toEqual({ none: after });
        // What was under the pointer, 100 px across the frame and 50 down, is still under it, to the pixel.
        expect(Math.abs(seenAt(after, placeAt(before, 100, 50)).x - 100)).toBeLessThanOrEqual(1);
        expect(Math.abs(seenAt(after, placeAt(before, 100, 50)).y - 50)).toBeLessThanOrEqual(1);

        wheel(frame, { deltaY: 300 });
        wheel(frame, { deltaY: 300 });
        expect(shownAt(root)).toBeLessThan(after.scale);
    });

    it("takes a pinch, a wheel that counts in lines and a hard turn alike, no turn changing it by too much", async () => {
        frameSize(800, 600);
        const { root } = await open();
        const frame = frameOf(root);
        const step = (turn: () => void): number => {
            const before = shownAt(root);
            turn();
            return shownAt(root) / before;
        };

        // A pinch comes with Ctrl, a few pixels at a time: it counts for more than a wheel's pixel.
        const pinch = step(() => wheel(frame, { deltaY: -8 }, true));
        const turn = step(() => wheel(frame, { deltaY: -8 }));
        expect(pinch).toBeGreaterThan(turn);
        // Three lines are one notch.
        const lines = step(() => wheel(frame, { deltaY: 3, deltaMode: 1 }));
        expect(lines).toBeLessThan(0.9);
        // However hard it is turned, one turn is a step, not a leap.
        expect(step(() => wheel(frame, { deltaY: -5000 }))).toBeLessThan(1.3);
        expect(step(() => wheel(frame, { deltaY: 5000 }, true))).toBeGreaterThan(0.75);
    });

    it("moves about by a drag on the background, a fitted drawing too, and never by one on a box", async () => {
        frameSize(4000, 3000);
        const { root } = await open();
        const frame = frameOf(root);
        const before = viewOf(root);
        expect(fitPressed(root)).toBe("true");

        press(canvasOf(root), "pointerdown", 150, 150);
        expect(frame.classList.contains("panning")).toBe(true);
        press(frame, "pointermove", 110, 120);
        press(frame, "pointermove", 90, 180);

        expect(viewOf(root)).toEqual({ scale: 1, x: before.x - 60, y: before.y + 30 });
        // Kept once it is let go, not at every step of the way.
        expect(safeStorage.get(VIEW_KEY)).toBeNull();
        press(frame, "pointerup", 90, 180);
        expect(parseViews(safeStorage.get(VIEW_KEY))).toEqual({ none: viewOf(root) });
        expect(frame.classList.contains("panning")).toBe(false);
        expect(fitPressed(root)).toBe("false");
        press(frame, "pointermove", 0, 0);
        expect(viewOf(root)).toEqual({ scale: 1, x: before.x - 60, y: before.y + 30 });

        press(node(root, "Agent planner"), "pointerdown", 150, 150);
        expect(frame.classList.contains("panning")).toBe(false);
        press(node(root, "Agent planner"), "pointerup", 150, 150);

        // A press on the background that goes nowhere sets nothing by hand.
        byRole(root, "button", "Fit").click();
        press(canvasOf(root), "pointerdown", 150, 150);
        press(frame, "pointerup", 150, 150);
        expect(fitPressed(root)).toBe("true");
    });

    it("is never moved so far that the drawing is out of sight", async () => {
        frameSize(800, 600);
        const { root } = await open();
        const frame = frameOf(root);
        const { width, height } = sizeOf(root);

        press(canvasOf(root), "pointerdown", 0, 0);
        press(frame, "pointermove", -99_999, -99_999);
        const far = viewOf(root);
        expect(far.x + width * far.scale).toBe(IN_VIEW);
        expect(far.y + height * far.scale).toBe(IN_VIEW);

        press(frame, "pointermove", 999_999, 999_999);
        press(frame, "pointerup", 999_999, 999_999);
        expect(viewOf(root)).toMatchObject({ x: 800 - IN_VIEW, y: 600 - IN_VIEW });
        // What is kept is where it stands, not how far it was asked to go.
        expect(parseViews(safeStorage.get(VIEW_KEY))).toEqual({ none: viewOf(root) });

        // A view kept from a larger drawing, or a larger window, is brought back the same way.
        safeStorage.set(VIEW_KEY, JSON.stringify({ none: { scale: 1, x: 50_000, y: -50_000 } }));
        const again = await open();
        expect(viewOf(again.root)).toEqual({ scale: 1, x: 800 - IN_VIEW, y: IN_VIEW - height });
    });

    it("moves a dragged box by what the pointer moved on the screen, at any zoom", async () => {
        frameSize(4000, 4000);
        const { root } = await open();
        byRole(root, "button", "Zoom in").click();
        byRole(root, "button", "Zoom in").click();
        const scale = shownAt(root);
        const planner = node(root, "Agent planner");
        const top = px(planner.style.top);

        press(planner, "pointerdown", 10, 10);
        press(planner, "pointermove", 10, 110);
        press(planner, "pointerup", 10, 110);

        expect(px(node(root, "Agent planner").style.top)).toBeCloseTo(top + 100 / scale, 0);
    });

    it("lets a box be put left of and above where the drawing began, and fits what it then covers", async () => {
        frameSize(4000, 3000);
        const { root } = await open();
        const before = viewOf(root);
        const planner = node(root, "Agent planner");
        const [left, top] = [px(planner.style.left), px(planner.style.top)];

        press(planner, "pointerdown", 1000, 1000);
        press(planner, "pointermove", 400, 700);
        press(planner, "pointerup", 400, 700);

        // Where it was put, before the drawing's own corner, and kept there; the view as it was.
        const put = node(root, "Agent planner");
        expect([px(put.style.left), px(put.style.top)]).toEqual([left - 600, top - 300]);
        expect(px(put.style.left)).toBeLessThan(0);
        expect(viewOf(root)).toEqual(before);

        byRole(root, "button", "Fit").click();

        // Fitted, the drawing's corner stands further in, by what now lies before it.
        const fitted = viewOf(root);
        expect(fitted.scale).toBe(1);
        expect(fitted.x + px(put.style.left)).toBeGreaterThan(0);
        expect(fitted.y + px(put.style.top)).toBeGreaterThan(0);
        expect(fitted.x).toBeGreaterThan(before.x);
    });

    it("offers Reset layout once the drawing is zoomed or moved about, and no longer once it is fitted", async () => {
        frameSize(4000, 4000);
        const { root } = await open();
        const offered = (): number => allByRole(root, "button", "Reset layout").length;
        expect(offered()).toBe(0);

        press(canvasOf(root), "pointerdown", 150, 150);
        press(frameOf(root), "pointermove", 120, 110);
        expect(offered()).toBe(1);
        press(frameOf(root), "pointerup", 120, 110);

        byRole(root, "button", "Reset layout").click();
        expect(viewOf(root)).toEqual({ scale: 1, x: 0, y: 0 });
        expect(offered()).toBe(0);

        byRole(root, "button", "Zoom in").click();
        expect(offered()).toBe(1);
        byRole(root, "button", "Fit").click();
        expect(offered()).toBe(0);

        // Kept from a visit before, it is offered from the start.
        byRole(root, "button", "Zoom in").click();
        const again = await open();
        expect(allByRole(again.root, "button", "Reset layout")).toHaveLength(1);
    });

    it("is fitted again by Reset layout, with the boxes put back", async () => {
        frameSize(4000, 4000);
        const { root } = await open();
        const opened = viewOf(root);
        byRole(root, "button", "Zoom in").click();
        const planner = node(root, "Agent planner");
        press(planner, "pointerdown", 10, 10);
        press(planner, "pointermove", 10, 110);
        press(planner, "pointerup", 10, 110);
        expect(shownAt(root)).toBe(ZOOM_STEP);

        byRole(root, "button", "Reset layout").click();

        expect(viewOf(root)).toEqual(opened);
        expect(parseViews(safeStorage.get(VIEW_KEY))).toEqual({});
    });

    it("holds the view while a box is dragged, however far the drawing grows, and once it is let go", async () => {
        const served = { wiring: wiring() };
        const { root, data } = await open(served);
        // A frame the drawing just fits: at its own size until it grows.
        frameSize(sizeOf(root).width, sizeOf(root).height);
        resize();
        const opened = viewOf(root);
        expect(opened).toEqual({ scale: 1, x: 0, y: 0 });
        const planner = node(root, "Agent planner");
        const left = px(planner.style.left);
        const wide = sizeOf(root).width;

        press(planner, "pointerdown", 10, 10);
        press(planner, "pointermove", 2010, 10);

        // The drawing grew under the box, past its frame, and the box is still under the pointer.
        expect(sizeOf(root).width).toBeGreaterThan(wide);
        expect(viewOf(root)).toEqual(opened);
        expect(px(planner.style.left)).toBe(left + 2000);
        // The frame told of a change mid-drag changes nothing either.
        resize();
        expect(viewOf(root)).toEqual(opened);
        press(planner, "pointermove", 2110, 10);
        expect(px(planner.style.left)).toBe(left + 2100);

        press(planner, "pointerup", 2110, 10);

        // Let go, what was just put in its place does not move: the drawing is larger than its frame now.
        expect(px(node(root, "Agent planner").style.left)).toBe(left + 2100);
        expect(viewOf(root)).toEqual(opened);
        expect(fitPressed(root)).toBe("true");
        // Nor when the frame is told of a change that is none, or the same wiring is read again.
        resize();
        await data.refresh();
        expect(viewOf(root)).toEqual(opened);

        // Fit shows the whole of it.
        byRole(root, "button", "Fit").click();
        const fitted = viewOf(root);
        expect(fitted.scale).toBeLessThan(1);

        // A box stepped by a key leaves it be as a drag does.
        const again = node(root, "Agent planner");
        again.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", altKey: true, bubbles: true }));
        expect(viewOf(root)).toEqual(fitted);
    });

    it("fits a drawing a box made larger once something else changes its size, or its frame's", async () => {
        const served = { wiring: wiring() };
        const { root, data } = await open(served);
        const frame = sizeOf(root);
        frameSize(frame.width, frame.height);
        resize();
        const planner = node(root, "Agent planner");
        press(planner, "pointerdown", 10, 10);
        press(planner, "pointermove", 2010, 10);
        press(planner, "pointerup", 2010, 10);
        expect(shownAt(root)).toBe(1);

        // Agents come: the drawing is another size, and is fitted.
        served.wiring = taller(8);
        await data.refresh();
        const grown = shownAt(root);
        expect(grown).toBeLessThan(1);
        expect(viewOf(root).x + sizeOf(root).width * grown).toBeLessThanOrEqual(frame.width);

        // The window is made larger: fitted to that.
        frameSize(frame.width * 2, frame.height * 4);
        resize();
        expect(shownAt(root)).toBeGreaterThan(grown);
    });

    it("is not redrawn from under a held slider: a wiring read meanwhile is drawn once it is let go", async () => {
        frameSize(4000, 4000);
        const served = { wiring: wiring() };
        const { root, data } = await open(served);
        const slider = byRole(root, "slider", "Zoom level") as HTMLInputElement;

        press(slider, "pointerdown", 0, 0);
        served.wiring = taller(2);
        await data.refresh();
        slider.value = "140";
        slider.dispatchEvent(new Event("input"));

        // Still the slider being held, working, and the drawing as it was.
        expect(byRole(root, "slider", "Zoom level")).toBe(slider);
        expect(shownAt(root)).toBe(1.4);
        expect(node(root, "Agent late")).toBeUndefined();

        window.dispatchEvent(new Event("pointerup"));

        expect(node(root, "Agent late")).toBeTruthy();
        expect(shownAt(root)).toBe(1.4);
        // With nothing held, a read is drawn at once.
        served.wiring = wiring();
        await data.refresh();
        expect(node(root, "Agent late")).toBeUndefined();
    });

    it("is not redrawn from under a box being dragged either, nor left waiting when the page is left", async () => {
        frameSize(4000, 4000);
        const served = { wiring: wiring() };
        const { root, data } = await open(served);
        const planner = node(root, "Agent planner");
        const left = px(planner.style.left);

        press(planner, "pointerdown", 10, 10);
        press(planner, "pointermove", 110, 10);
        served.wiring = taller(2);
        await data.refresh();
        press(planner, "pointermove", 210, 10);

        expect(node(root, "Agent planner")).toBe(planner);
        expect(px(planner.style.left)).toBe(left + 200);

        // The window losing the pointer ends the hold as letting go does.
        window.dispatchEvent(new Event("blur"));

        expect(node(root, "Agent late")).toBeTruthy();
    });

    it("brings a box into the frame when the keyboard gives it the focus, never when a pointer does", async () => {
        frameSize(800, 600);
        const { root } = await open();
        const planner = node(root, "Agent planner");
        const watch = node(root, "Agent watch");
        const before = viewOf(root);
        standsAt(frameOf(root), 100, 100, 800, 600);
        // One box off the right and the bottom of the frame, one off its left and its top.
        standsAt(planner, 850, 656, 200, 48);
        standsAt(watch, 40, 60, 200, 48);

        // Pressed, as a pointer focuses what it presses: the view is the pointer's to move, not the focus's.
        window.dispatchEvent(new PointerEvent("pointerdown"));
        planner.focus();
        expect(viewOf(root)).toEqual(before);
        planner.blur();

        window.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab" }));
        planner.focus();

        // Moved by just what brings it in, with a little room: 166 left, 20 up.
        expect(viewOf(root)).toEqual({ scale: before.scale, x: before.x - 166, y: before.y - 20 });
        expect(fitPressed(root)).toBe("false");

        watch.focus();
        expect(viewOf(root)).toEqual({ scale: before.scale, x: before.x - 166 + 76, y: before.y - 20 + 56 });

        // A box already in the frame moves nothing.
        const comfort = node(root, "Agent comfort");
        standsAt(comfort, 300, 300, 200, 48);
        const settled = viewOf(root);
        comfort.focus();
        expect(viewOf(root)).toEqual(settled);
    });

    it("drops a fit that waits for the next frame when the view goes, and stops watching the keyboard", async () => {
        frameSize(1000, 4000);
        const { root } = await open();
        const before = viewOf(root);
        const unheard = vi.spyOn(window, "removeEventListener");

        frameSize(1400, 4000);
        resized.forEach(changed => changed());
        views.splice(0).forEach(v => v.destroy());
        nextFrame();

        expect(frames).toHaveLength(0);
        expect(viewOf(root)).toEqual(before);
        expect(unheard.mock.calls.map(call => call[0])).toEqual(
            expect.arrayContaining(["keydown", "pointerdown"]),
        );
    });

    it("has its controls over the frame, never inside what is moved about", async () => {
        frameSize(4000, 4000);
        const { root } = await open();
        const zoom = byRole(root, "group", "Zoom");

        expect(frameOf(root).contains(zoom)).toBe(false);
        expect(zoom.parentElement).toBe(frameOf(root).parentElement);
        expect(byRole(root, "group", "Wiring options").contains(zoom)).toBe(false);
    });

    it("has no controls while nothing is drawn, and takes a press with no drawing to show", async () => {
        frameSize(4000, 4000);
        const served: { wiring: Wiring } = { wiring: { agents: [], topics: [], sources: [], warnings: [] } };
        const { root, data } = await open(served);
        expect(frameOf(root)).toBeNull();
        expect(allByRole(root, "group", "Zoom")).toHaveLength(0);

        // The controls alone, before any drawing is theirs to show: the zoom is kept for when one is.
        const controls = new WiringZoom().controls();
        byRole(controls, "button", "Zoom in").click();
        expect(controls.querySelector("span")!.textContent).toBe("125%");
        expect(parseViews(safeStorage.get(VIEW_KEY))).toEqual({ none: { scale: ZOOM_STEP, x: 0, y: 0 } });

        // A drawing arriving brings its controls with it.
        served.wiring = wiring();
        await data.refresh();
        expect(allByRole(root, "group", "Zoom")).toHaveLength(1);
    });

    it("asks nothing of a narrow screen, where the list is shown and there is nothing to zoom", async () => {
        frameSize(300, 600);
        const { root } = await open({ wiring: wiring() }, true);

        expect(frameOf(root)).toBeNull();
        expect(allByRole(root, "group", "Zoom")).toHaveLength(0);
        expect(safeStorage.get(VIEW_KEY)).toBeNull();
    });

    it("has no controls where there is no drawing: in the list", async () => {
        frameSize(4000, 4000);
        const { root } = await open();
        expect(allByRole(root, "group", "Zoom")).toHaveLength(1);

        byRole(root, "button", "View as list").click();

        expect(allByRole(root, "group", "Zoom")).toHaveLength(0);
    });
});
