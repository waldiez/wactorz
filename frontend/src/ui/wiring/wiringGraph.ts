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

/** The classes of a line: what it means, and whether it is a problem, on trial, or the selection's. */
export function lineClasses(r: Routed, selected: string): string {
    const classes = ["af-wire-line", r.edge.kind];
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

/** What a line says, written just before where it ends, on its own band of colour. */
function lineLabel(r: Routed, row: number): SVGTextElement {
    const text = document.createElementNS(SVG, "text");
    text.setAttribute("class", `af-wire-line-label${r.edge.problem ? " problem" : ""}`);
    text.setAttribute("x", String(r.x2 - 6));
    text.setAttribute("y", String(r.y2 - 8 - row * 14));
    text.setAttribute("text-anchor", "end");
    text.textContent = r.edge.label;
    return text;
}

function lines(layout: Layout, selected: string): SVGSVGElement {
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", "af-wire-lines");
    svg.setAttribute("width", String(layout.width));
    svg.setAttribute("height", String(layout.height));
    svg.setAttribute("aria-hidden", "true");
    // A label is written once for each end it says something about: three
    // topics missing the same field for one agent say it once, there.
    const said = new Map<string, Set<string>>();
    for (const r of layout.routed) {
        const path = document.createElementNS(SVG, "path");
        path.setAttribute("class", lineClasses(r, selected));
        path.setAttribute("d", linePath(r));
        svg.appendChild(path);
        const labels = said.get(r.edge.to) ?? new Set<string>();
        if (r.edge.label && !labels.has(r.edge.label)) {
            svg.appendChild(lineLabel(r, labels.size));
            labels.add(r.edge.label);
            said.set(r.edge.to, labels);
        }
    }
    return svg;
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
 * The drawing of `layout`, in a frame that scrolls when it is larger than the
 * screen. While a box is dragged its lines follow it, and the drawing grows
 * to hold it.
 */
export function buildGraph(layout: Layout, host: GraphHost): HTMLElement {
    const frame = el("div", "af-wire-frame");
    const canvas = el("div", "af-wire-canvas");
    const size = (width: number, height: number): void => {
        canvas.style.width = `${width}px`;
        canvas.style.height = `${height}px`;
    };
    size(layout.width, layout.height);
    let drawn = lines(layout, host.selected());
    canvas.appendChild(drawn);
    const redraw = (): void => {
        const grown = extent(layout.placed, layout.width, layout.height);
        size(grown.width, grown.height);
        const again = lines({ ...layout, ...grown, routed: reroute(layout) }, host.selected());
        canvas.replaceChild(again, drawn);
        drawn = again;
    };
    if (layout.lanes.length > 1) {
        canvas.append(...layout.lanes.map(laneBand));
    }
    // In reading order, column by column, so Tab walks the graph left to right.
    const reading = [...layout.placed].sort((a, b) => a.column - b.column || a.y - b.y);
    canvas.append(...reading.map(p => placedButton(p, host, redraw)));
    frame.appendChild(canvas);
    return frame;
}
