/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Where each node of the wiring goes: columns from left to right, data flowing
 * the way the lines point. Our own small layered layout, kept apart from the
 * graph so positions can later be pinned by hand.
 *
 * **Columns.** A node is one column right of the furthest node that feeds it,
 * so a topic sits after its producers (the furthest, when there are several)
 * and an agent after the topics it reads. A node nothing feeds -- a source, a
 * subscription nothing fills, an agent that only publishes -- is in the first
 * column. The operator is always in the last.
 *
 * **Cycles** are allowed: an agent may read its own topic, or two agents each
 * other's. Columns are worked out with the lines that close a cycle set
 * aside: a depth-first walk, from agents before topics, meets them as lines
 * back to a node still being walked, so a cycle loses one of its reading
 * lines and a topic stays after its producer. Every line is still drawn; one that
 * points backwards is drawn as a loop.
 *
 * **Order within a column** starts from `order`, by kind and then id, and is
 * refined by two sweeps that move each node towards the mean row of its
 * neighbours in the column before, then after. Ties keep the fixed order, so
 * the same graph is always laid out the same: a refresh that changes nothing
 * moves nothing.
 *
 * **Pins.** A box a person moved stays where they put it (`Pins`); the lines
 * follow it.
 *
 * **Lanes.** Grouped by machine, each machine has its own band of rows across
 * every column, labelled, and a node stays in its machine's band; topics fed
 * from outside and the operator have a band of their own.
 */
import { boxLines, type Graph, type GraphEdge, type GraphNode, type NodeKind } from "./graph";

/** How wide each kind of box is, in pixels. */
export const WIDTH: Record<NodeKind, number> = { source: 170, topic: 200, agent: 200, operator: 150 };
/** How tall a box's first line is, and each line after it, and the room above and below them. */
export const FIRST_LINE = 18;
export const LINE = 16;
export const PADDING = 14;

/**
 * How tall `node`'s box is: as many lines as it shows (`boxLines`), each one
 * row, since a line too long for the box is cut short rather than wrapped.
 */
export function heightOf(node: GraphNode): number {
    const lines = boxLines(node).length;
    return PADDING + FIRST_LINE + Math.max(0, lines - 1) * LINE;
}
/** The room between columns, which the lines cross, and between rows. */
export const COLUMN_GAP = 84;
export const ROW_GAP = 22;
/** The room above a lane, for its label. */
export const LANE_HEADER = 28;
/** The margin around the whole drawing. */
export const MARGIN = 24;

/** A node, placed: its top-left corner. */
export interface Placed {
    node: GraphNode;
    x: number;
    y: number;
    w: number;
    h: number;
    column: number;
    /** Whether it stands where a person put it, rather than where the layout did. */
    pinned: boolean;
}

/** Where a person put a box: its top-left corner, in the drawing's pixels. */
export interface Pin {
    x: number;
    y: number;
}

/** The boxes a person put by hand, by node id. */
export type Pins = ReadonlyMap<string, Pin>;

/** One machine's band of rows. */
export interface Lane {
    /** The machine, or "" for the band of what runs nowhere in particular. */
    machine: string;
    y: number;
    h: number;
}

/** A line, placed: the points it runs between, and whether it runs backwards. */
export interface Routed {
    edge: GraphEdge;
    x1: number;
    y1: number;
    x2: number;
    y2: number;
    backwards: boolean;
}

/** The whole drawing. */
export interface Layout {
    placed: Placed[];
    routed: Routed[];
    lanes: Lane[];
    width: number;
    height: number;
}

const KIND_ORDER: Record<NodeKind, number> = { source: 0, topic: 1, agent: 2, operator: 3 };

/** The fixed order of nodes, before anything is refined: by kind, then id. */
export function order(a: GraphNode, b: GraphNode): number {
    return KIND_ORDER[a.kind] - KIND_ORDER[b.kind] || a.id.localeCompare(b.id);
}

/**
 * The order cycles are walked in: from an agent before a topic, so the line a
 * cycle loses is a reading one and a topic stays after the agent that
 * publishes it.
 */
const WALK_ORDER: Record<NodeKind, number> = { source: 0, agent: 1, topic: 2, operator: 3 };

function walkOrder(a: GraphNode, b: GraphNode): number {
    return WALK_ORDER[a.kind] - WALK_ORDER[b.kind] || a.id.localeCompare(b.id);
}

/** The lines that close a cycle, as `from>to`: met by a depth-first walk from agents first. */
export function backEdges(nodes: readonly GraphNode[], edges: readonly GraphEdge[]): Set<string> {
    const out = new Map<string, string[]>();
    for (const edge of edges) {
        out.set(edge.from, [...(out.get(edge.from) ?? []), edge.to]);
    }
    for (const targets of out.values()) {
        targets.sort();
    }
    const state = new Map<string, "walking" | "done">();
    const back = new Set<string>();
    const walk = (id: string): void => {
        state.set(id, "walking");
        for (const next of out.get(id) ?? []) {
            const seen = state.get(next);
            if (seen === "walking") {
                back.add(`${id}>${next}`);
            } else if (seen === undefined) {
                walk(next);
            }
        }
        state.set(id, "done");
    };
    for (const node of [...nodes].sort(walkOrder)) {
        if (!state.has(node.id)) {
            walk(node.id);
        }
    }
    return back;
}

/** Each node's column: one past the furthest node that feeds it, the lines closing a cycle aside. */
export function columns(graph: Graph): Map<string, number> {
    const back = backEdges(graph.nodes, graph.edges);
    const forward = graph.edges.filter(e => e.from !== e.to && !back.has(`${e.from}>${e.to}`));
    const into = new Map<string, string[]>();
    for (const edge of forward) {
        into.set(edge.to, [...(into.get(edge.to) ?? []), edge.from]);
    }
    const column = new Map<string, number>();
    const columnOf = (id: string, path: Set<string>): number => {
        const known = column.get(id);
        if (known !== undefined) {
            return known;
        }
        // The forward lines hold no cycle; the path guards against one all the same.
        path.add(id);
        const feeders = (into.get(id) ?? []).filter(f => !path.has(f));
        const value = feeders.length ? Math.max(...feeders.map(f => columnOf(f, path))) + 1 : 0;
        path.delete(id);
        column.set(id, value);
        return value;
    };
    for (const node of graph.nodes) {
        columnOf(node.id, new Set());
    }
    const last = Math.max(
        0,
        ...graph.nodes.filter(n => n.kind !== "operator").map(n => column.get(n.id) ?? 0),
    );
    for (const node of graph.nodes) {
        if (node.kind === "operator") {
            column.set(node.id, last + 1);
        }
    }
    return column;
}

/** The band a node is drawn in, grouped by machine: its machine's, or "" for the rest. */
function laneOf(node: GraphNode, grouped: boolean): string {
    return grouped ? node.machine : "";
}

/** Nodes in each column, in order, after the sweeps towards their neighbours. */
function ordered(graph: Graph, column: Map<string, number>, grouped: boolean): GraphNode[][] {
    const count = Math.max(0, ...column.values()) + 1;
    const cols: GraphNode[][] = Array.from({ length: count }, () => []);
    for (const node of [...graph.nodes].sort(order)) {
        cols[column.get(node.id) ?? 0]!.push(node);
    }
    const neighbours = new Map<string, string[]>();
    for (const edge of graph.edges) {
        neighbours.set(edge.from, [...(neighbours.get(edge.from) ?? []), edge.to]);
        neighbours.set(edge.to, [...(neighbours.get(edge.to) ?? []), edge.from]);
    }
    const sweep = (from: number, to: number, step: number): void => {
        for (let c = from; c !== to; c += step) {
            const reference = cols[c - step];
            if (!reference) {
                continue;
            }
            const row = new Map(reference.map((n, i) => [n.id, i]));
            const centre = (node: GraphNode): number => {
                const rows = (neighbours.get(node.id) ?? [])
                    .map(id => row.get(id))
                    .filter(r => r !== undefined);
                return rows.length ? rows.reduce((a, b) => a + b, 0) / rows.length : Number.POSITIVE_INFINITY;
            };
            const fixed = new Map(cols[c]!.map((n, i) => [n.id, i]));
            cols[c]!.sort(
                (a, b) =>
                    laneOf(a, grouped).localeCompare(laneOf(b, grouped)) ||
                    centre(a) - centre(b) ||
                    fixed.get(a.id)! - fixed.get(b.id)!,
            );
        }
    };
    sweep(1, count, 1);
    sweep(count - 2, -1, -1);
    if (grouped) {
        for (const col of cols) {
            col.sort((a, b) => laneOf(a, true).localeCompare(laneOf(b, true)));
        }
    }
    return cols;
}

/**
 * Where the line for `edge` runs: from the right of where it starts to the
 * left of where it ends. One whose end is not to the right of its start --
 * a cycle, or a box moved there by hand -- is drawn as a loop.
 */
function route(edge: GraphEdge, at: Map<string, Placed>): Routed | null {
    const from = at.get(edge.from);
    const to = at.get(edge.to);
    if (!from || !to) {
        return null;
    }
    return {
        edge,
        x1: from.x + from.w,
        y1: from.y + from.h / 2,
        x2: to.x,
        y2: to.y + to.h / 2,
        backwards: to.x < from.x + from.w,
    };
}

/** The lines of `drawing`, run again between where its boxes stand now: after one was moved. */
export function reroute(drawing: Pick<Layout, "placed" | "routed">): Routed[] {
    const at = new Map(drawing.placed.map(p => [p.node.id, p]));
    return drawing.routed.map(r => route(r.edge, at)).filter((r): r is Routed => r !== null);
}

/** How large the drawing must be to hold every box, wherever it stands, with the margin round it. */
export function extent(
    placed: readonly Placed[],
    width: number,
    height: number,
): { width: number; height: number } {
    return {
        width: Math.max(width, ...placed.map(p => p.x + p.w + MARGIN)),
        height: Math.max(height, ...placed.map(p => p.y + p.h + MARGIN)),
    };
}

/**
 * Lay `graph` out, in lanes by machine when `grouped`, with each box in
 * `pins` where a person put it instead. A pinned box keeps its place
 * whatever else arrives, so it may come to stand over a new one; the rest
 * are laid out as though it were where the layout would put it.
 */
export function layout(graph: Graph, grouped = false, pins: Pins = new Map()): Layout {
    const column = columns(graph);
    const cols = ordered(graph, column, grouped);
    const lanesNamed = grouped ? [...new Set(graph.nodes.map(n => laneOf(n, true)))].sort() : [""];
    const widest = Math.max(...Object.values(WIDTH));
    const placed: Placed[] = [];
    const lanes: Lane[] = [];
    let top = MARGIN;
    for (const machine of lanesNamed) {
        const header = grouped ? LANE_HEADER : 0;
        let tallest = 0;
        cols.forEach((col, c) => {
            let y = top + header;
            for (const node of col.filter(n => laneOf(n, grouped) === machine)) {
                const w = WIDTH[node.kind];
                const h = heightOf(node);
                const pin = pins.get(node.id);
                placed.push({
                    node,
                    x: pin ? Math.max(0, pin.x) : MARGIN + c * (widest + COLUMN_GAP) + (widest - w) / 2,
                    y: pin ? Math.max(0, pin.y) : y,
                    w,
                    h,
                    column: c,
                    pinned: pin !== undefined,
                });
                y += h + ROW_GAP;
            }
            tallest = Math.max(tallest, y - (top + header));
        });
        lanes.push({ machine, y: top, h: header + tallest });
        top += header + tallest + (grouped ? ROW_GAP : 0);
    }
    const at = new Map(placed.map(p => [p.node.id, p]));
    const routed = graph.edges.map(e => route(e, at)).filter((r): r is Routed => r !== null);
    const width = MARGIN * 2 + Math.max(1, cols.length) * (widest + COLUMN_GAP) - COLUMN_GAP;
    return { placed, routed, lanes, ...extent(placed, width, top + MARGIN) };
}
