/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The wiring drawn: each node a real button placed where the layout put it,
 * over an SVG of the lines between them.
 *
 * Each box is exactly as tall as the layout made it, from how many lines it
 * shows; a line too long for its box is cut short, never wrapped, so no box
 * grows over the next. The whole of it is still read out, and is in the
 * details.
 *
 * The buttons are HTML, so each takes focus, answers Enter and Space, and is
 * named for a screen reader as any button is; they come in reading order,
 * column by column, so Tab walks the graph left to right. The lines are
 * drawing only, hidden from assistive technology: what joins what is said in
 * the selection's details, and in full by the list view. The drawing keeps
 * its size and scrolls inside its frame when it is larger than the screen.
 *
 * Each line ends in a handle on the side of the box it meets, drawn over the
 * boxes: a filled dot where it leaves, a ring where it arrives, in the line's
 * own colour. With a box chosen, its lines and their handles at both ends
 * are lit and whatever it is not joined to steps back; pointing at a box, or
 * giving it the focus, lights its lines the same way without choosing it.
 *
 * A box can be moved by hand (`wiringDrag`): dragged, or stepped with Alt and
 * an arrow key. The view keeps where it was put as a pin.
 */
import { boxLines, type GraphNode } from "../../wiring/graph";
import {
    extent,
    reroute,
    type Lane,
    type Layout,
    type Pin,
    type Placed,
    type Routed,
} from "../../wiring/layout";
import { el, pressed } from "../dom";
import { movable, wasDragged } from "./wiringDrag";

const SVG = "http://www.w3.org/2000/svg";

/** The name of the band, grouped by machine, for what runs on none: outside sources and their topics, the operator. */
export const OUTSIDE_LANE = "outside";

/** What the drawing tells the view. */
export interface GraphHost {
    /** The node selected, by id; "" for none. */
    selected(): string;
    /** A node was chosen. */
    select(id: string): void;
    /** A node was moved by hand, to `pin`: keep it there. */
    moved(id: string, pin: Pin): void;
}

/** The word a screen reader is told before a node's name: what it is. */
const KIND_WORD = { agent: "Agent", topic: "Topic", source: "Outside source", operator: "" } as const;

/** What a screen reader is told after a node's lines: how many warnings it has, if any. */
export function warningWords(node: GraphNode): string {
    const n = node.warnings.length;
    return n ? `, ${n} warning${n === 1 ? "" : "s"}` : "";
}

/** Text only a screen reader is given, inside what is shown. */
function spoken(text: string): HTMLElement {
    return el("span", "af-screen-reader", text);
}

/** The classes of a node's box: its kind, and whether it is warned of or on trial. */
function nodeClasses(node: GraphNode): string {
    const warned = node.warnings.length ? " warned" : "";
    const trial = node.topic?.shadow ? " shadow" : "";
    return `af-wire-node ${node.kind}${warned}${trial}`;
}

/** The nodes, by id, that `selected` is joined to by a line, itself among them; none when nothing is selected. */
export function joinedTo(routed: readonly Routed[], selected: string): Set<string> {
    const joined = new Set<string>();
    if (!selected) {
        return joined;
    }
    joined.add(selected);
    for (const { edge } of routed) {
        if (edge.from === selected) {
            joined.add(edge.to);
        } else if (edge.to === selected) {
            joined.add(edge.from);
        }
    }
    return joined;
}

function nodeButton(node: GraphNode, host: GraphHost): HTMLButtonElement {
    const box = el("button", nodeClasses(node));
    box.type = "button";
    box.dataset["node"] = node.id;
    const lines = boxLines(node);
    const [first, ...rest] = lines;
    // A line cut short is still whole when pointed at.
    box.title = lines.join(" · ");
    // Named by what it shows, with its kind and warnings in words only a
    // screen reader is given: a name that left out shown text would not match it.
    const kind = KIND_WORD[node.kind];
    if (kind) {
        box.append(spoken(`${kind} `));
    }
    box.append(el("span", "af-wire-node-name", first ?? ""));
    for (const line of rest) {
        const problem = line.startsWith("missing: ") ? " problem" : "";
        box.append(spoken(", "), el("span", `af-wire-node-line${problem}`, line));
    }
    const warned = warningWords(node);
    if (warned) {
        box.append(spoken(warned));
    }
    pressed(box, host.selected() === node.id);
    box.addEventListener("click", () => {
        // The click a drag ends in is the drag's, not a choice.
        if (!wasDragged(box)) {
            host.select(node.id);
        }
    });
    return box;
}

/** The path of a line: a curve forwards, or a loop beneath for one running back. */
export function linePath(r: Pick<Routed, "x1" | "y1" | "x2" | "y2" | "backwards">): string {
    if (!r.backwards) {
        const bend = Math.max(40, (r.x2 - r.x1) / 2);
        return `M ${r.x1} ${r.y1} C ${r.x1 + bend} ${r.y1}, ${r.x2 - bend} ${r.y2}, ${r.x2} ${r.y2}`;
    }
    const drop = Math.max(r.y1, r.y2) + 60;
    return `M ${r.x1} ${r.y1} C ${r.x1 + 80} ${drop}, ${r.x2 - 80} ${drop}, ${r.x2} ${r.y2}`;
}

/** How large a handle is drawn, in pixels across half of it: at rest, and on a line of the selection. */
export const HANDLE_RADIUS = 3.5;
export const HANDLE_RADIUS_LIT = 4.5;

/** The classes of a line: what it means, and whether it is a problem, on trial, or the selection's. */
export function lineClasses(r: Routed, selected: string): string {
    return meaning("af-wire-line", r, selected);
}

/** The classes of the handle at one end of a line: which end, and all that the line's own say. */
export function handleClasses(r: Routed, end: "out" | "in", selected: string): string {
    return meaning(`af-wire-handle ${end}`, r, selected);
}

function meaning(base: string, r: Routed, selected: string): string {
    const classes = [base, r.edge.kind];
    if (r.edge.problem) {
        classes.push("problem");
    }
    if (r.edge.shadow) {
        classes.push("shadow");
    }
    if (selected && (r.edge.from === selected || r.edge.to === selected)) {
        classes.push("selected");
    }
    return classes.join(" ");
}

/** The room one line's label takes above the next, in pixels. */
const LABEL_ROW = 13;

/**
 * Where each line's words are written, as the `y` of their baseline by line:
 * just above the handle the line arrives at. A label is written once for each
 * end it says something about, so three lines saying the same of one box say
 * it once; labels whose handles are too close to be read apart are moved up,
 * one above the next.
 */
export function labelSpots(routed: readonly Routed[]): Map<Routed, number> {
    const byEnd = new Map<string, Routed[]>();
    for (const r of routed) {
        const there = byEnd.get(r.edge.to) ?? [];
        if (r.edge.label && !there.some(o => o.edge.label === r.edge.label)) {
            byEnd.set(r.edge.to, [...there, r]);
        }
    }
    const spots = new Map<Routed, number>();
    for (const labelled of byEnd.values()) {
        let below = Number.POSITIVE_INFINITY;
        for (const r of [...labelled].sort((a, b) => b.y2 - a.y2)) {
            below = Math.min(r.y2 - 6, below - LABEL_ROW);
            spots.set(r, below);
        }
    }
    return spots;
}

/** What a line says, written just before the handle it arrives at, on its own band of colour. */
function lineLabel(r: Routed, y: number): SVGTextElement {
    const text = document.createElementNS(SVG, "text");
    text.setAttribute("class", `af-wire-line-label${r.edge.problem ? " problem" : ""}`);
    text.setAttribute("x", String(r.x2 - 8));
    text.setAttribute("y", String(y));
    text.setAttribute("text-anchor", "end");
    text.textContent = r.edge.label;
    return text;
}

/** A layer of the drawing as large as the whole of it, for the lines or the handles: drawing only. */
function layer(layout: Pick<Layout, "width" | "height">, name: string): SVGSVGElement {
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", name);
    svg.setAttribute("width", String(layout.width));
    svg.setAttribute("height", String(layout.height));
    svg.setAttribute("aria-hidden", "true");
    return svg;
}

/** Mark what was drawn for the line `r` with the boxes at its ends, so pointing at one finds it. */
function between(mark: SVGElement, r: Routed): void {
    mark.dataset["from"] = r.edge.from;
    mark.dataset["to"] = r.edge.to;
}

function lines(layout: Layout, selected: string): SVGSVGElement {
    const svg = layer(layout, "af-wire-lines");
    const spots = labelSpots(layout.routed);
    for (const r of layout.routed) {
        const path = document.createElementNS(SVG, "path");
        path.setAttribute("class", lineClasses(r, selected));
        path.setAttribute("d", linePath(r));
        between(path, r);
        svg.appendChild(path);
        const spot = spots.get(r);
        if (spot !== undefined) {
            svg.appendChild(lineLabel(r, spot));
        }
    }
    return svg;
}

/** The handle at one end of the line `r`: a dot at `x`, `y`. */
function handle(r: Routed, end: "out" | "in", x: number, y: number, selected: string): SVGCircleElement {
    const dot = document.createElementNS(SVG, "circle");
    const lit = selected !== "" && (r.edge.from === selected || r.edge.to === selected);
    dot.setAttribute("class", handleClasses(r, end, selected));
    dot.setAttribute("cx", String(x));
    dot.setAttribute("cy", String(y));
    dot.setAttribute("r", String(lit ? HANDLE_RADIUS_LIT : HANDLE_RADIUS));
    between(dot, r);
    return dot;
}

/** The handles of every line, on a layer of their own over the boxes: a dot sits on a box's border, not under it. */
function handles(layout: Layout, selected: string): SVGSVGElement {
    const svg = layer(layout, "af-wire-handles");
    for (const r of layout.routed) {
        svg.append(handle(r, "out", r.x1, r.y1, selected), handle(r, "in", r.x2, r.y2, selected));
    }
    return svg;
}

/**
 * The lines of a drawing and their handles, as its two layers: the lines
 * under the boxes, the handles over them. Drawn again when a box moves, and
 * lit for the box pointed at and for the one holding the focus: each is held
 * apart, so leaving a box with the pointer does not put out the lines of one
 * that still has the focus, nor the other way round.
 */
class Marks {
    lines: SVGSVGElement;
    handles: SVGSVGElement;
    /** The box pointed at, and the one holding the focus, by id; "" for none. */
    private readonly _lit = { pointer: "", focus: "" };

    constructor(
        layout: Layout,
        private readonly _selected: string,
    ) {
        this.lines = lines(layout, _selected);
        this.handles = handles(layout, _selected);
    }

    /** Draw both layers again for `layout`, in place, keeping what was lit. */
    redraw(layout: Layout): void {
        const [under, over] = [lines(layout, this._selected), handles(layout, this._selected)];
        this.lines.replaceWith(under);
        this.handles.replaceWith(over);
        [this.lines, this.handles] = [under, over];
        this._light();
    }

    /** Light `box`'s lines while it is pointed at or holds the focus. */
    follow(box: HTMLElement, id: string): void {
        for (const [by, on, off] of [
            ["pointer", "pointerenter", "pointerleave"],
            ["focus", "focus", "blur"],
        ] as const) {
            box.addEventListener(on, () => this._hold(by, id));
            box.addEventListener(off, () => this._hold(by, ""));
        }
    }

    /** Note which box the pointer or the focus is on now, "" for none, and light what follows from both. */
    private _hold(by: "pointer" | "focus", id: string): void {
        this._lit[by] = id;
        this._light();
    }

    /** Light the lines and handles of the box pointed at and of the one holding the focus, and no others. */
    private _light(): void {
        const lit = [this._lit.pointer, this._lit.focus].filter(id => id !== "");
        for (const layer of [this.lines, this.handles]) {
            for (const mark of layer.querySelectorAll<SVGElement>(".af-wire-line, .af-wire-handle")) {
                const near = lit.some(id => mark.dataset["from"] === id || mark.dataset["to"] === id);
                mark.classList.toggle("near", near);
            }
        }
    }
}

/** A machine's band, labelled, behind its nodes. */
function laneBand(lane: Lane): HTMLElement {
    const band = el("div", "af-wire-lane");
    band.style.top = `${lane.y}px`;
    band.style.height = `${lane.h}px`;
    band.append(el("span", "af-wire-lane-name", lane.machine || OUTSIDE_LANE));
    return band;
}

/** A node's button, where the layout put it, to be moved by hand. */
function placedButton(p: Placed, host: GraphHost, redraw: () => void): HTMLButtonElement {
    const box = nodeButton(p.node, host);
    box.classList.toggle("pinned", p.pinned);
    box.style.left = `${p.x}px`;
    box.style.top = `${p.y}px`;
    box.style.width = `${p.w}px`;
    box.style.height = `${p.h}px`;
    movable(box, p, { moving: redraw, moved: (_p, pin) => host.moved(p.node.id, pin) });
    return box;
}

/**
 * Every box of `layout`, in reading order, column by column, so Tab walks the
 * graph left to right. With a box chosen, one it is not joined to is marked
 * as standing apart.
 */
function boxes(layout: Layout, host: GraphHost, marks: Marks, redraw: () => void): HTMLButtonElement[] {
    const selected = host.selected();
    const joined = joinedTo(layout.routed, selected);
    const reading = [...layout.placed].sort((a, b) => a.column - b.column || a.y - b.y);
    return reading.map(p => {
        const box = placedButton(p, host, redraw);
        box.classList.toggle("apart", selected !== "" && !joined.has(p.node.id));
        marks.follow(box, p.node.id);
        return box;
    });
}

/**
 * The drawing of `layout`, in a frame that scrolls when it is larger than the
 * screen. While a box is dragged its lines follow it, and the drawing grows
 * to hold it.
 */
export function buildGraph(layout: Layout, host: GraphHost): HTMLElement {
    const frame = el("div", "af-wire-frame");
    const canvas = el("div", "af-wire-canvas");
    const selected = host.selected();
    canvas.classList.toggle("choosing", selected !== "");
    const size = (width: number, height: number): void => {
        canvas.style.width = `${width}px`;
        canvas.style.height = `${height}px`;
    };
    size(layout.width, layout.height);
    const marks = new Marks(layout, selected);
    const redraw = (): void => {
        const grown = extent(layout.placed, layout.width, layout.height);
        size(grown.width, grown.height);
        marks.redraw({ ...layout, ...grown, routed: reroute(layout) });
    };
    canvas.appendChild(marks.lines);
    if (layout.lanes.length > 1) {
        canvas.append(...layout.lanes.map(laneBand));
    }
    canvas.append(...boxes(layout, host, marks, redraw));
    canvas.appendChild(marks.handles);
    frame.appendChild(canvas);
    return frame;
}
