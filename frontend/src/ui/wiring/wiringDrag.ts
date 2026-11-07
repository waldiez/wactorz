/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Moving a box of the wiring by hand: dragged with the pointer, or with Alt
 * and an arrow key while it has the focus.
 *
 * A press that moves less than `DRAG_THRESHOLD` is a click and chooses the
 * box; one that moves further drags it, the lines following, and chooses
 * nothing. Where it is let go is handed on to be kept as a pin. The keys move
 * it `KEY_STEP` at a time, so a box can be moved without a pointer.
 */
import type { Pin, Placed } from "../../wiring/layout";

/** How far a press must move, in pixels, before it is a drag rather than a click. */
export const DRAG_THRESHOLD = 4;
/** How far Alt and an arrow key move a box, in pixels. */
export const KEY_STEP = 16;

/** The keys that move a box, and which way. */
const STEPS: Record<string, [number, number]> = {
    ArrowLeft: [-1, 0],
    ArrowRight: [1, 0],
    ArrowUp: [0, -1],
    ArrowDown: [0, 1],
};

/** What moving a box tells the drawing. */
export interface Mover {
    /** The box stands somewhere new, mid-drag: draw the lines to it again. */
    moving(placed: Placed): void;
    /** The box was let go, or stepped by a key, at `pin`: keep it there. */
    moved(placed: Placed, pin: Pin): void;
}

/** Whether a press just dragged `box`, so the click it ends in chooses nothing. Asked once. */
export function wasDragged(box: HTMLElement): boolean {
    const dragged = box.dataset["dragged"] === "true";
    delete box.dataset["dragged"];
    return dragged;
}

function place(box: HTMLElement, placed: Placed, x: number, y: number): void {
    placed.x = Math.max(0, x);
    placed.y = Math.max(0, y);
    box.style.left = `${placed.x}px`;
    box.style.top = `${placed.y}px`;
}

/** Let `box`, standing at `placed`, be dragged and stepped. */
export function movable(box: HTMLElement, placed: Placed, mover: Mover): void {
    box.setAttribute("aria-keyshortcuts", "Alt+ArrowUp Alt+ArrowDown Alt+ArrowLeft Alt+ArrowRight");
    box.addEventListener("pointerdown", down => {
        if (down.button !== 0) {
            return;
        }
        const start = { x: down.clientX, y: down.clientY, left: placed.x, top: placed.y };
        let dragging = false;
        box.setPointerCapture?.(down.pointerId);
        const move = (e: PointerEvent): void => {
            const dx = e.clientX - start.x;
            const dy = e.clientY - start.y;
            if (!dragging && Math.hypot(dx, dy) < DRAG_THRESHOLD) {
                return;
            }
            dragging = true;
            box.classList.add("dragging");
            place(box, placed, start.left + dx, start.top + dy);
            mover.moving(placed);
        };
        const end = (): void => {
            box.removeEventListener("pointermove", move);
            box.removeEventListener("pointerup", end);
            box.removeEventListener("pointercancel", end);
            box.classList.remove("dragging");
            if (dragging) {
                box.dataset["dragged"] = "true";
                mover.moved(placed, { x: placed.x, y: placed.y });
            }
        };
        box.addEventListener("pointermove", move);
        box.addEventListener("pointerup", end);
        box.addEventListener("pointercancel", end);
    });
    box.addEventListener("keydown", e => {
        const step = STEPS[e.key];
        if (!e.altKey || !step) {
            return;
        }
        e.preventDefault();
        place(box, placed, placed.x + step[0] * KEY_STEP, placed.y + step[1] * KEY_STEP);
        mover.moved(placed, { x: placed.x, y: placed.y });
    });
}
