/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Zooming a chart into a window of its time: the arithmetic, the drag that
 * selects a window with a mouse, and the buttons that do the same from a
 * keyboard or a phone, where a drag would be a scroll.
 */
import { button, el } from "../dom";
import type { TimeWindow } from "./types";

/** A drag narrower than this share of the plot is a click, not a selection. */
export const LEAST_DRAG = 0.02;

/** The narrowest window a zoom goes to, as a share of the whole: past it nothing more is shown. */
export const NARROWEST = 0.001;

/** Nor narrower than a second, the finest step the time axis labels. */
export const NARROWEST_MS = 1000;

/** ``window`` kept inside ``whole``, and no narrower than `NARROWEST` of it or `NARROWEST_MS`. */
export function clamp(window: TimeWindow, whole: TimeWindow): TimeWindow {
    const span = whole.to - whole.from;
    const least = Math.max(span * NARROWEST, NARROWEST_MS);
    let from = Math.max(window.from, whole.from);
    let to = Math.min(window.to, whole.to);
    if (to - from < least) {
        const middle = (from + to) / 2;
        from = Math.max(whole.from, middle - least / 2);
        to = Math.min(whole.to, from + least);
    }
    return { from, to };
}

/** The middle half of ``window``. */
export function zoomedIn(window: TimeWindow, whole: TimeWindow): TimeWindow {
    const quarter = (window.to - window.from) / 4;
    return clamp({ from: window.from + quarter, to: window.to - quarter }, whole);
}

/** ``window`` twice as wide around its middle, or null once it covers ``whole``: no zoom left. */
export function zoomedOut(window: TimeWindow, whole: TimeWindow): TimeWindow | null {
    const half = window.to - window.from;
    const wider = clamp({ from: window.from - half / 2, to: window.to + half / 2 }, whole);
    return wider.from <= whole.from && wider.to >= whole.to ? null : wider;
}

/** The window between two shares of the plot's width, in either order, of ``frame``. */
export function windowBetween(a: number, b: number, frame: TimeWindow): TimeWindow {
    const [left, right] = a < b ? [a, b] : [b, a];
    const span = frame.to - frame.from;
    return { from: frame.from + left * span, to: frame.from + right * span };
}

/**
 * Let a mouse drag across ``canvas`` select a window: ``shading`` shows the
 * stretch while it is dragged, and ``select`` gets the two shares of the width
 * when it is let go. A touch is left alone, to scroll the page.
 */
export function wireDrag(
    canvas: HTMLElement,
    shading: HTMLElement,
    select: (a: number, b: number) => void,
): void {
    let start: number | null = null;
    const share = (x: number): number => {
        const box = canvas.getBoundingClientRect();
        return box.width > 0 ? Math.max(0, Math.min(1, (x - box.left) / box.width)) : 0;
    };
    canvas.addEventListener("pointerdown", e => {
        if (e.pointerType !== "mouse" || e.button !== 0) {
            return;
        }
        start = share(e.clientX);
        canvas.setPointerCapture?.(e.pointerId);
    });
    canvas.addEventListener("pointermove", e => {
        if (start === null) {
            return;
        }
        const now = share(e.clientX);
        shading.style.left = `${(Math.min(start, now) * 100).toFixed(2)}%`;
        shading.style.width = `${(Math.abs(now - start) * 100).toFixed(2)}%`;
        shading.hidden = false;
    });
    canvas.addEventListener("pointerup", e => {
        if (start === null) {
            return;
        }
        const end = share(e.clientX);
        const began = start;
        start = null;
        shading.hidden = true;
        if (Math.abs(end - began) >= LEAST_DRAG) {
            select(began, end);
        }
    });
}

/** What the zoom buttons do. */
export interface ZoomControls {
    zoomIn(): void;
    zoomOut(): void;
    reset(): void;
}

/** The zoom buttons: in, out, and back to everything; out and back only while zoomed. */
export function zoomButtons(zoomed: boolean, controls: ZoomControls): HTMLElement {
    const bar = el("div", "af-chart-zoom");
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", "Zoom");
    const zoomIn = button("af-chart-zoom-btn", "+");
    zoomIn.setAttribute("aria-label", "Zoom in");
    zoomIn.addEventListener("click", () => controls.zoomIn());
    const zoomOut = button("af-chart-zoom-btn", "−");
    zoomOut.setAttribute("aria-label", "Zoom out");
    zoomOut.disabled = !zoomed;
    zoomOut.addEventListener("click", () => controls.zoomOut());
    const reset = button("af-chart-zoom-btn", "Reset");
    reset.setAttribute("aria-label", "Reset zoom");
    reset.disabled = !zoomed;
    reset.addEventListener("click", () => controls.reset());
    bar.append(zoomIn, zoomOut, reset);
    return bar;
}
