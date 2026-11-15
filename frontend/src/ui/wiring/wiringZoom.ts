/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The view of the wiring's drawing: how large it is shown and where it
 * stands in its frame, and the zooming and moving about that change them.
 *
 * The drawing is laid out and drawn at its own size, and shown through its
 * frame as a whole: scaled, and shifted by an offset. The frame does not
 * scroll; what lies outside it is cut off, and is brought in by moving the
 * drawing. Everything the layout says stays in the drawing's own pixels, so
 * a box dragged while zoomed moves by what the pointer moved, divided by
 * `scale()`.
 *
 * **Fitted, until a person says otherwise.** The drawing is shown whole,
 * from the frame's own corner, where it begins and is read from: at its own
 * size when it fits, smaller when it does not, though never so small its
 * words cannot be read (`FIT_FLOOR`); past that the rest is moved to. It is fitted again whenever the drawing or the frame changes size,
 * so agents coming and going, or a window resized, never leave it cut off.
 *
 * **Except under a person's own hand.** A box moved past the drawing's edge
 * makes the drawing larger, and fitting it again would move everything the
 * moment the box is let go: what was just put in its place would move. So
 * the view holds still for as long as a box is being dragged, and a drawing
 * a moved box made larger stays as it was shown, until something else
 * changes its size or the frame's, or Fit is pressed.
 *
 * **A view set by hand is kept**, in this browser and for each grouping, as
 * the pins are. Zooming: the buttons, the slider between them, or the wheel,
 * about the pointer; a pinch on a trackpad is the wheel with Ctrl. Moving
 * about: a drag on the empty background. It stays as set whatever the
 * drawing becomes, though never with the drawing out of sight: part of it
 * is always kept in the frame (`IN_VIEW`). Fit forgets it, and the drawing
 * is fitted again.
 *
 * **A box given the focus from the keyboard is brought into the frame**, if
 * it lies outside it, so the focus can be seen wherever Tab takes it.
 */
import { safeStorage } from "../../safeStorage";
import { button, el, labelled, pressed } from "../dom";

/** Where the view set by hand is kept. */
export const VIEW_KEY = "wactorz-wiring-view";
/** The smallest and largest a person can make the drawing, and how much one press changes it. */
export const MIN_ZOOM = 0.3;
export const MAX_ZOOM = 2;
export const ZOOM_STEP = 1.25;
/** The smallest the drawing is made to fit its frame: smaller, and its words cannot be read. */
export const FIT_FLOOR = 0.5;
/** How much of the drawing is kept in the frame, in pixels, however it is moved about. */
export const IN_VIEW = 80;
/** The room left around a box brought into the frame for the focus, in pixels. */
const REVEAL_ROOM = 16;
/**
 * How much the wheel changes the zoom, as a power of e per pixel turned: a
 * wheel's notch is many pixels, a pinch on a trackpad, which comes with
 * Ctrl, a few at a time. And the most one turn may change it, either way.
 */
const WHEEL_RATE = 0.002;
const PINCH_RATE = 0.01;
const WHEEL_MOST = 0.2;
/** How many pixels a line is, for a wheel that counts its turns in lines. */
const WHEEL_LINE = 33;

/** A width and a height, in pixels. */
export interface Size {
    width: number;
    height: number;
}

/**
 * What a drawing covers, in its own pixels: from `left`, `top`, which are
 * nothing unless a box stands before the drawing's own corner, to `width`,
 * `height`.
 */
export interface Bounds extends Size {
    left: number;
    top: number;
}

/** A view of the drawing: how much larger than life it is shown, and where its own corner stands in the frame. */
export interface View {
    scale: number;
    x: number;
    y: number;
}

/** Each grouping's view set by hand: `machine` with lanes, `none` without; absent when fitted. */
export type StoredViews = Partial<Record<"machine" | "none", View>>;

/** The drawing as it stands in the page: its frame and itself, and what it covers. */
export interface Shown extends Bounds {
    frame: HTMLElement;
    canvas: HTMLElement;
    /** Set while a box is being dragged, the drawing growing under it: the view holds until it is let go. */
    moving: boolean;
}

/** `scale` within what a person may set, to the hundredth. */
export function clampZoom(scale: number): number {
    return Math.round(Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, scale)) * 100) / 100;
}

function finite(value: unknown): value is number {
    return typeof value === "number" && Number.isFinite(value);
}

/** What storage holds, as views: anything that is not a view a person could have set is left out. */
export function parseViews(text: string | null): StoredViews {
    const kept: StoredViews = {};
    try {
        const parsed: unknown = JSON.parse(text ?? "{}");
        if (!parsed || typeof parsed !== "object") {
            return kept;
        }
        for (const grouping of ["machine", "none"] as const) {
            const view = (parsed as Record<string, unknown>)[grouping];
            const { scale, x, y } = (view && typeof view === "object" ? view : {}) as Partial<View>;
            if (finite(scale) && scale > 0 && finite(x) && finite(y)) {
                kept[grouping] = { scale: clampZoom(scale), x: Math.round(x), y: Math.round(y) };
            }
        }
    } catch {
        // Whatever it was, it was not a view: fitted.
    }
    return kept;
}

/**
 * How large to show a `drawing` so the whole of it is seen in `frame`: its
 * own size when it fits, never larger, and never smaller than `FIT_FLOOR`.
 * A frame with no size yet, as before it is in the page, fits anything.
 */
export function fitScale(frame: Size, drawing: Size): number {
    if (frame.width <= 0 || frame.height <= 0 || drawing.width <= 0 || drawing.height <= 0) {
        return 1;
    }
    const whole = Math.min(frame.width / drawing.width, frame.height / drawing.height);
    // Rounded down, so a drawing made to fit is never a fraction of a pixel too large.
    return Math.max(FIT_FLOOR, Math.min(1, Math.floor(whole * 100) / 100));
}

/**
 * The view that fits `drawing` in `frame`: as large as `fitScale` says, with
 * where the drawing begins at the frame's own corner. A drawing reads from
 * its left, so that is where it stands, whatever room is left over.
 */
export function fitView(frame: Size, drawing: Bounds): View {
    const scale = fitScale(frame, {
        width: drawing.width - drawing.left,
        height: drawing.height - drawing.top,
    });
    // Not -0, where the drawing begins at its own corner.
    return {
        scale,
        x: Math.round(-drawing.left * scale) || 0,
        y: Math.round(-drawing.top * scale) || 0,
    };
}

/**
 * `view`, moved no further than leaves `IN_VIEW` of `drawing` inside
 * `frame` along each side, or the whole of it where it is smaller than
 * that. A frame with no size yet leaves the view as it is.
 */
export function keptInView(view: View, frame: Size, drawing: Bounds): View {
    if (frame.width <= 0 || frame.height <= 0) {
        return view;
    }
    const within = (at: number, start: number, end: number, room: number): number => {
        const keep = Math.min(IN_VIEW, (end - start) * view.scale, room);
        return Math.min(room - keep - start * view.scale, Math.max(keep - end * view.scale, at));
    };
    return {
        scale: view.scale,
        x: within(view.x, drawing.left, drawing.width, frame.width),
        y: within(view.y, drawing.top, drawing.height, frame.height),
    };
}

/** A slider over every zoom a person may set, in hundredths: as a percentage. */
function zoomSlider(): HTMLInputElement {
    const slider = labelled(el("input", "af-wire-zoom-slider"), "Zoom level");
    slider.type = "range";
    slider.min = String(MIN_ZOOM * 100);
    slider.max = String(MAX_ZOOM * 100);
    slider.step = "1";
    return slider;
}

/** How far to move what runs from `start` to `end` so it lies between `from` and `to`, with room to spare. */
function shiftInto(start: number, end: number, from: number, to: number): number {
    if (start < from + REVEAL_ROOM) {
        return from + REVEAL_ROOM - start;
    }
    return end > to - REVEAL_ROOM ? to - REVEAL_ROOM - end : 0;
}

/** The view of the wiring's drawing: fitted or set by hand, its controls, and the wheel and the drag that go with it. */
export class WiringZoom {
    private _stored: StoredViews = parseViews(safeStorage.get(VIEW_KEY));
    private _grouped = false;
    /** The view that fits the drawing shown now; what the last drawing took until this one is measured. */
    private _fitted: View = { scale: 1, x: 0, y: 0 };
    /** The sizes of the drawing and of its frame that `_fitted` stands for; "" when it must be worked out. */
    private _settled = "";
    /** Set when a person moved a box: the drawing as it is next is taken as it is shown, not fitted. */
    private _keep = false;
    private _shown: Shown | null = null;
    private _observer: ResizeObserver | null = null;
    /** The frame in which the drawing is fitted after its frame changed size; null when none waits. */
    private _refitting: number | null = null;
    /** Whether the last thing a person did anywhere was press a key: the focus then moved by the keyboard. */
    private _keys = false;
    private _watching = false;
    /** Whether the drawing was fitted when that was last said; null before it was said at all. */
    private _told: boolean | null = null;

    /** @param _changed Called when the drawing goes from fitted to a view set by hand, or back. */
    constructor(private readonly _changed: () => void = () => undefined) {}

    private readonly _onKey = (): void => {
        this._keys = true;
    };
    private readonly _onPointer = (): void => {
        this._keys = false;
    };
    private _controls: {
        out: HTMLButtonElement;
        slider: HTMLInputElement;
        level: HTMLElement;
        in: HTMLButtonElement;
        fit: HTMLButtonElement;
    } | null = null;

    /** The view the drawing is shown at: set by hand, or fitted; kept in sight of its frame. */
    view(): View {
        const view = this._stored[this._key()] ?? this._fitted;
        return this._shown ? keptInView(view, this._inside(this._shown.frame), this._shown) : view;
    }

    /** How much larger than its own size the drawing is shown. */
    scale(): number {
        return this.view().scale;
    }

    /** Whether the drawing is fitted to its frame, rather than at a view a person set. */
    fitted(): boolean {
        return this._stored[this._key()] === undefined;
    }

    /** The grouping drawn now: each keeps a view of its own. */
    group(grouped: boolean): void {
        this._grouped = grouped;
    }

    /** Forget the view set by hand for the grouping drawn now, and fit the drawing again. */
    fit(): void {
        delete this._stored[this._key()];
        this._save();
        this._settled = "";
        this._keep = false;
        this.refit();
    }

    /** A person moved a box: the drawing as it is drawn next keeps the view it is shown at. */
    moved(): void {
        this._keep = true;
    }

    /**
     * The controls, to stand over a corner of the drawing: smaller, a slider
     * from the smallest to the largest, larger, how large it is now, and
     * Fit, which is pressed while the drawing is fitted. They follow the
     * zoom however it changes, until the next set is built.
     */
    controls(): HTMLElement {
        const group = labelled(el("div", "af-wire-zoom"), "Zoom");
        group.setAttribute("role", "group");
        const out = labelled(button("af-wire-zoom-btn", "−"), "Zoom out");
        const zoomIn = labelled(button("af-wire-zoom-btn", "+"), "Zoom in");
        const fit = button("af-wire-zoom-btn fit", "Fit");
        const level = el("span", "af-wire-zoom-level");
        const slider = zoomSlider();
        out.dataset["key"] = "zoom:out";
        slider.dataset["key"] = "zoom:level";
        zoomIn.dataset["key"] = "zoom:in";
        fit.dataset["key"] = "zoom:fit";
        out.addEventListener("click", () => this._zoomTo(this.scale() / ZOOM_STEP));
        slider.addEventListener("input", () => this._zoomTo(Number(slider.value) / 100));
        zoomIn.addEventListener("click", () => this._zoomTo(this.scale() * ZOOM_STEP));
        fit.addEventListener("click", () => this.fit());
        group.append(out, slider, zoomIn, level, fit);
        this._controls = { out, slider, level, in: zoomIn, fit };
        this._say();
        return group;
    }

    /**
     * The drawing stands in the page as `shown`, or has grown to it: show it
     * at its view. A frame not seen before is listened to, for the wheel, a
     * drag on its background, the focus and its own size, in place of the
     * one before.
     */
    show(shown: Shown): void {
        if (this._shown?.frame !== shown.frame) {
            this._observer?.disconnect();
            this._listen(shown.frame);
        }
        this._shown = shown;
        this.refit();
    }

    /** Stop following the frame's size and how a person moves the focus. */
    destroy(): void {
        this._observer?.disconnect();
        this._observer = null;
        if (this._refitting !== null) {
            cancelAnimationFrame(this._refitting);
            this._refitting = null;
        }
        if (this._watching) {
            window.removeEventListener("keydown", this._onKey, true);
            window.removeEventListener("pointerdown", this._onPointer, true);
            this._watching = false;
        }
        this._shown = null;
        this._controls = null;
    }

    /**
     * Show the drawing at its view, working out again what fits when the
     * drawing or its frame is another size than the fit stands for. Asked for
     * once the drawing is in the page, where its frame has a size, and
     * whenever that size changes.
     */
    refit(): void {
        const shown = this._shown;
        // With a box in hand, or before the frame is in the page: as it is.
        if (shown && !shown.moving && shown.frame.clientWidth > 0) {
            const inside = this._inside(shown.frame);
            const now = [shown.left, shown.top, shown.width, shown.height, inside.width, inside.height].join(
                " ",
            );
            if (!this._keep && now !== this._settled) {
                this._fitted = fitView(inside, shown);
            }
            this._keep = false;
            this._settled = now;
        }
        this._apply();
    }

    private _key(): "machine" | "none" {
        return this._grouped ? "machine" : "none";
    }

    private _save(): void {
        safeStorage.set(VIEW_KEY, JSON.stringify(this._stored));
    }

    private _inside(frame: HTMLElement): Size {
        return { width: frame.clientWidth, height: frame.clientHeight };
    }

    /** Scale and shift the drawing to its view; say it on the controls. */
    private _apply(): void {
        if (this._shown) {
            const { scale, x, y } = this.view();
            this._shown.canvas.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
        }
        this._say();
    }

    private _say(): void {
        const fitted = this.fitted();
        const turned = this._told !== null && this._told !== fitted;
        this._told = fitted;
        if (turned) {
            this._changed();
        }
        if (!this._controls) {
            return;
        }
        const scale = this.scale();
        const percent = String(Math.round(scale * 100));
        this._controls.level.textContent = `${percent}%`;
        this._controls.slider.value = percent;
        // Said as a person reads it, not as the bare number a slider gives.
        this._controls.slider.setAttribute("aria-valuetext", `${percent}%`);
        this._controls.out.disabled = scale <= MIN_ZOOM;
        this._controls.in.disabled = scale >= MAX_ZOOM;
        pressed(this._controls.fit, this.fitted());
    }

    /**
     * Take `view` as set by hand, no further off than keeps the drawing in
     * sight, and show it; kept in this browser unless more of the same
     * follows at once.
     */
    private _byHand(view: View, keep = true): void {
        const { scale, x, y } = this._shown
            ? keptInView(view, this._inside(this._shown.frame), this._shown)
            : view;
        this._stored[this._key()] = { scale, x: Math.round(x), y: Math.round(y) };
        if (keep) {
            this._save();
        }
        this._apply();
    }

    /**
     * Zoom to `scale` by hand. What was under `at`, a place in the frame,
     * stays under it; with none given, what was at the frame's middle.
     */
    private _zoomTo(scale: number, at?: { x: number; y: number }): void {
        const before = this.view();
        const frame = this._shown ? this._inside(this._shown.frame) : { width: 0, height: 0 };
        const about = at ?? { x: frame.width / 2, y: frame.height / 2 };
        const grew = clampZoom(scale) / before.scale;
        this._byHand({
            scale: clampZoom(scale),
            x: about.x - (about.x - before.x) * grew,
            y: about.y - (about.y - before.y) * grew,
        });
    }

    /** Move the drawing by hand, `dx` across and `dy` down the frame. */
    private _moveBy(dx: number, dy: number, keep = true): void {
        const before = this.view();
        this._byHand({ scale: before.scale, x: before.x + dx, y: before.y + dy }, keep);
    }

    /**
     * Fit the drawing in the next frame, once however many times it is asked
     * for before then. Not at once: a size observer that changes the layout
     * as it is told of a change is reported by the browser as a loop.
     */
    private _refitSoon(): void {
        this._refitting ??= requestAnimationFrame(() => {
            this._refitting = null;
            this.refit();
        });
    }

    /** Bring `box` into `frame`, if any of it lies outside, with a little room around it. */
    private _reveal(frame: HTMLElement, box: Element): void {
        const outer = frame.getBoundingClientRect();
        const inner = box.getBoundingClientRect();
        const dx = shiftInto(inner.left, inner.right, outer.left, outer.right);
        const dy = shiftInto(inner.top, inner.bottom, outer.top, outer.bottom);
        if (dx !== 0 || dy !== 0) {
            this._moveBy(dx, dy);
        }
    }

    private _listen(frame: HTMLElement): void {
        frame.addEventListener("wheel", e => this._wheel(frame, e), { passive: false });
        frame.addEventListener("pointerdown", down => this._drag(frame, down));
        frame.addEventListener("focusin", e => {
            const box = e.target instanceof Element ? e.target.closest("[data-node]") : null;
            if (box && this._keys) {
                this._reveal(frame, box);
            }
        });
        if (!this._watching) {
            window.addEventListener("keydown", this._onKey, true);
            window.addEventListener("pointerdown", this._onPointer, true);
            this._watching = true;
        }
        if (typeof ResizeObserver === "function") {
            this._observer = new ResizeObserver(() => this._refitSoon());
            this._observer.observe(frame);
        }
    }

    /** The wheel zooms about the pointer, as a pinch does. Never the page. */
    private _wheel(frame: HTMLElement, e: WheelEvent): void {
        e.preventDefault();
        const turned =
            e.deltaY * (e.deltaMode === 1 ? WHEEL_LINE : 1) * (e.ctrlKey ? PINCH_RATE : WHEEL_RATE);
        const box = frame.getBoundingClientRect();
        this._zoomTo(this.scale() * Math.exp(Math.max(-WHEEL_MOST, Math.min(WHEEL_MOST, -turned))), {
            x: e.clientX - box.left,
            y: e.clientY - box.top,
        });
    }

    /**
     * Drag the drawing about in `frame` by its empty background, from the
     * press `down`. A press on a box is the box's, to choose or move it.
     * Where it is let go is what is kept.
     */
    private _drag(frame: HTMLElement, down: PointerEvent): void {
        const on = down.target instanceof Element ? down.target.closest("[data-node]") : null;
        if (down.button !== 0 || on) {
            return;
        }
        let last = { x: down.clientX, y: down.clientY };
        frame.setPointerCapture?.(down.pointerId);
        frame.classList.add("panning");
        const move = (e: PointerEvent): void => {
            this._moveBy(e.clientX - last.x, e.clientY - last.y, false);
            last = { x: e.clientX, y: e.clientY };
        };
        const end = (): void => {
            frame.removeEventListener("pointermove", move);
            frame.removeEventListener("pointerup", end);
            frame.removeEventListener("pointercancel", end);
            frame.classList.remove("panning");
            this._save();
        };
        frame.addEventListener("pointermove", move);
        frame.addEventListener("pointerup", end);
        frame.addEventListener("pointercancel", end);
    }
}
