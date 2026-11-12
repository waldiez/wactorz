/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The Wiring view: who publishes what, who reads it, and where the data
 * stops, as a graph on a wide screen and a list on a narrow one, with the
 * selection's details beside it.
 *
 * It draws what `WiringData` hands it and asks the server nothing itself.
 * Above the drawing: the warnings, counted over the whole wiring, each a
 * button that shows only the topics with that warning, in their context;
 * then the choices, set apart by what they are about: how it is grouped,
 * what is shown, and what can be done (the boxes put back, the list in place
 * of the graph, a read). How large the drawing is shown (`wiringZoom`) is
 * set over a corner of the drawing itself.
 *
 * A wiring read again while a pointer is held down in the view is not drawn
 * until it is let go: drawing replaces what is on the page, and the slider
 * or the box being dragged would be taken from under the pointer. A redraw
 * keeps the focus on the control that had it.
 */
import {
    buildGraph as buildModel,
    DEFAULT_CHOICES,
    warningCounts,
    type GraphChoices,
    type GraphNode,
    type WarningKind,
} from "../../wiring/graph";
import { layout, type Pin } from "../../wiring/layout";
import type { Wiring } from "../../types/wiring";
import { relTime } from "../overview/agentState";
import { button, el, labelled, live, pressed } from "../dom";
import type { WiringData, WiringState } from "./wiringData";
import { buildDetail } from "./wiringDetail";
import { buildGraph } from "./wiringGraph";
import { buildList } from "./wiringList";
import { WiringPins } from "./wiringPins";
import { WiringZoom, type Shown } from "./wiringZoom";

/** Below this width the list is shown in place of the graph. */
export const NARROW = "(max-width: 720px)";

/** What ends a pointer being held down in the view: let go, taken away by the browser, or the page left. */
const RELEASES = ["pointerup", "pointercancel", "blur"] as const;

/** How each warning's button reads, for a count. */
export function warningLabel(kind: WarningKind, count: number): string {
    const one = count === 1;
    if (kind === "field_mismatch") {
        return `${count} field mismatch${one ? "" : "es"}`;
    }
    if (kind === "unread") {
        return `${count} topic${one ? "" : "s"} nobody reads`;
    }
    return `${count} subscription${one ? "" : "s"} with no producer`;
}

/** What the status line says of `state`. */
export function statusText(state: WiringState): string {
    if (state.status === "unavailable") {
        return `No wiring to show: ${state.error}.`;
    }
    if (state.status === "error") {
        return `Could not read the wiring: ${state.error}`;
    }
    if (!state.wiring) {
        return "Reading the wiring…";
    }
    const when = state.readAt === null ? "" : relTime(state.readAt);
    if (state.error) {
        return `Could not read it again: ${state.error}. Showing what was read ${when}.`;
    }
    return state.reading ? "Reading again…" : `Read ${when}.`;
}

/** The narrow screen's query, where the browser has one. */
function narrowQuery(): MediaQueryList | null {
    return typeof window.matchMedia === "function" ? window.matchMedia(NARROW) : null;
}

function isNarrow(): boolean {
    return narrowQuery()?.matches ?? false;
}

export class WiringView {
    private readonly _root = el("div", "af-wiring");
    private readonly _status = live(el("p", "af-wire-status"), "status");
    private readonly _chips = el("div", "af-wire-chips");
    private readonly _toolbar = labelled(el("div", "af-wire-toolbar"), "Wiring options");
    private readonly _body = el("div", "af-wire-body");
    private _state: WiringState;
    private _choices: GraphChoices = { ...DEFAULT_CHOICES };
    private _grouped = false;
    private _asList = false;
    private _selected = "";
    /** Where boxes were moved by hand, kept in this browser. */
    private readonly _pins = new WiringPins();
    /** How large the drawing is shown: fitted, or as set by hand and kept in this browser. */
    private readonly _zoom = new WiringZoom(() => this._renderToolbar(this._state.wiring !== null));
    private readonly _unsubscribe: () => void;
    /** The narrow screen's query, heard so a turn or a resize across it swaps graph and list at once. */
    private readonly _narrow = narrowQuery();
    private readonly _onWidth = (): void => this._render();
    /** Whether a pointer is held down in the view, and whether a wiring read meanwhile waits to be drawn. */
    private _held = false;
    private _waiting = false;
    private readonly _onRelease = (): void => {
        this._held = false;
        if (this._waiting) {
            this._waiting = false;
            this._render();
        }
    };

    constructor(private readonly data: WiringData) {
        this._state = data.state;
        const heading = el("header", "af-wire-heading");
        const title = el("div", "af-wire-title");
        title.append(
            el("h2", "af-wire-page-title", "Wiring"),
            el("p", "af-wire-subtitle", "Who publishes what, who reads it, and where the data stops."),
        );
        heading.append(title, this._chips);
        this._toolbar.setAttribute("role", "group");
        this._root.append(heading, this._toolbar, this._status, this._body);
        this._unsubscribe = data.subscribe(state => {
            this._state = state;
            if (this._held) {
                this._waiting = true;
            } else {
                this._render();
            }
        });
        this._narrow?.addEventListener?.("change", this._onWidth);
        this._root.addEventListener("pointerdown", () => (this._held = true));
        // Let go anywhere, the page left included: a release outside the view is still a release.
        for (const release of RELEASES) {
            window.addEventListener(release, this._onRelease);
        }
    }

    /** The view, drawn from what is held, and the wiring read again. */
    build(): HTMLElement {
        this._render();
        this.data.opened();
        return this._root;
    }

    /** Stop hearing the data and the screen's width. */
    destroy(): void {
        this._unsubscribe();
        this._narrow?.removeEventListener?.("change", this._onWidth);
        for (const release of RELEASES) {
            window.removeEventListener(release, this._onRelease);
        }
        this._zoom.destroy();
    }

    /** Select the node `id`, or nothing with "". */
    select(id: string): void {
        this._selected = this._selected === id ? "" : id;
        this._render();
        // On a phone the details are under the list, out of sight of what was chosen.
        if (this._selected && isNarrow()) {
            this._root.querySelector(".af-wire-detail")?.scrollIntoView?.({ block: "nearest" });
        }
    }

    private _render(): void {
        const focused = this._focusKey();
        this._zoom.group(this._grouped);
        this._status.textContent = statusText(this._state);
        const wiring = this._state.wiring;
        this._renderChips(wiring);
        this._renderToolbar(wiring !== null);
        this._body.replaceChildren();
        if (wiring) {
            this._renderWiring(wiring);
        }
        // In the page now, so its frame has a size to fit the drawing to.
        this._zoom.refit();
        this._refocus(focused);
    }

    private _renderChips(wiring: Wiring | null): void {
        this._chips.replaceChildren();
        if (!wiring) {
            return;
        }
        const counts = warningCounts(wiring);
        for (const kind of ["field_mismatch", "unread", "no_producer"] as const) {
            if (!counts[kind]) {
                continue;
            }
            const chip = button(`af-wire-chip ${kind}`, warningLabel(kind, counts[kind]));
            chip.dataset["key"] = `chip:${kind}`;
            pressed(chip, this._choices.focus === kind);
            chip.addEventListener("click", () => {
                this._choices = { ...this._choices, focus: this._choices.focus === kind ? null : kind };
                this._render();
            });
            this._chips.append(chip);
        }
    }

    private _renderToolbar(any: boolean): void {
        this._toolbar.replaceChildren();
        if (any) {
            this._toolbar.append(this._grouping(), this._showing());
        }
        const actions = el("div", "af-wire-actions");
        actions.append(...(any ? this._drawingButtons() : []), this._refreshButton());
        this._toolbar.append(actions);
    }

    /** What is shown: the outside sources, and the system's own agents. */
    private _showing(): HTMLElement {
        const group = labelled(el("div", "af-wire-segment"), "Show");
        group.setAttribute("role", "group");
        group.append(
            el("span", "af-wire-segment-label", "Show"),
            this._toggle(
                "Outside sources",
                this._choices.outside,
                on => (this._choices = { ...this._choices, outside: on }),
            ),
            this._toggle(
                "System agents",
                this._choices.system,
                on => (this._choices = { ...this._choices, system: on }),
            ),
        );
        return group;
    }

    /** Group by machine, or not. */
    private _grouping(): HTMLElement {
        const group = labelled(el("div", "af-wire-segment"), "Group by");
        group.setAttribute("role", "group");
        group.append(el("span", "af-wire-segment-label", "Group by"));
        for (const [label, grouped] of [
            ["Machine", true],
            ["None", false],
        ] as const) {
            const choose = button("af-wire-segment-btn", label);
            choose.dataset["key"] = `group:${label}`;
            pressed(choose, this._grouped === grouped);
            choose.addEventListener("click", () => {
                this._grouped = grouped;
                this._render();
            });
            group.append(choose);
        }
        return group;
    }

    /**
     * On a wide screen: the boxes put back where the layout puts them and the drawing fitted again, when
     * a box was moved or the drawing was zoomed or moved about; and the list instead.
     */
    private _drawingButtons(): HTMLElement[] {
        if (isNarrow()) {
            return [];
        }
        const buttons: HTMLElement[] = [];
        if (!this._asList && (this._pins.any(this._grouped) || !this._zoom.fitted())) {
            const reset = button("af-wire-btn", "Reset layout");
            reset.dataset["key"] = "reset";
            reset.addEventListener("click", () => {
                this._pins.clear(this._grouped);
                this._zoom.fit();
                this._render();
            });
            buttons.push(reset);
        }
        const swap = button("af-wire-btn", this._asList ? "View as graph" : "View as list");
        swap.dataset["key"] = "swap";
        swap.addEventListener("click", () => {
            this._asList = !this._asList;
            this._render();
        });
        buttons.push(swap);
        return buttons;
    }

    private _toggle(label: string, on: boolean, set: (on: boolean) => void): HTMLElement {
        const wrap = el("label", "af-wire-toggle");
        const box = el("input");
        box.type = "checkbox";
        box.checked = on;
        box.dataset["key"] = `toggle:${label}`;
        box.addEventListener("change", () => {
            set(box.checked);
            this._render();
        });
        wrap.append(box, el("span", "", label));
        return wrap;
    }

    private _refreshButton(): HTMLButtonElement {
        const refresh = button("af-wire-btn", "Refresh");
        refresh.dataset["key"] = "refresh";
        refresh.disabled = this._state.reading;
        refresh.addEventListener("click", () => void this.data.refresh());
        return refresh;
    }

    private _renderWiring(wiring: Wiring): void {
        const graph = buildModel(wiring, this._choices);
        if (!graph.nodes.find(n => n.id === this._selected)) {
            this._selected = "";
        }
        if (!graph.nodes.length) {
            this._body.append(el("p", "af-wire-empty", this._emptyText(wiring)));
            return;
        }
        const host = {
            selected: () => this._selected,
            select: (id: string) => this.select(id),
            moved: (id: string, pin: Pin) => this._pinned(id, pin),
            scale: () => this._zoom.scale(),
            shown: (shown: Shown) => this._zoom.show(shown),
        };
        const main = el("div", "af-wire-main");
        if (this._asList || isNarrow()) {
            main.append(buildList(graph, wiring, host));
        } else {
            // The zoom stands over a corner of the frame, beside the drawing it holds: it never moves with it.
            const drawing = el("div", "af-wire-drawing");
            drawing.append(
                buildGraph(layout(graph, this._grouped, this._pins.of(this._grouped)), host),
                this._zoom.controls(),
            );
            main.append(drawing, this._legend());
        }
        const selected: GraphNode | null = graph.nodes.find(n => n.id === this._selected) ?? null;
        this._body.append(main, buildDetail(selected, wiring, graph));
    }

    /** Keep a box where it was put, and draw again so everything else follows it. */
    private _pinned(id: string, pin: Pin): void {
        this._pins.set(this._grouped, id, pin);
        this._zoom.moved();
        this._render();
    }

    private _emptyText(wiring: Wiring): string {
        if (this._choices.focus !== null) {
            return "No topic has that warning now.";
        }
        if (wiring.agents.length && !this._choices.system) {
            return "Only the system's own agents are running. Tick System agents to see them.";
        }
        return "No agent publishes or reads anything yet.";
    }

    private _legend(): HTMLElement {
        const legend = labelled(el("ul", "af-wire-legend"), "Legend");
        for (const [kind, text] of [
            ["flowing", "publishes, reads"],
            ["shadow", "on trial"],
            ["problem", "problem"],
            ["asks", "asks a person"],
            ["feeds", "from outside"],
            ["leaves", "leaves"],
            ["arrives", "arrives"],
        ]) {
            const item = el("li", `af-wire-legend-item ${kind}`);
            item.append(el("span", "af-wire-legend-key"), el("span", "", text));
            legend.append(item);
        }
        legend.append(
            el(
                "li",
                "af-wire-legend-hint",
                "Drag a box or the background · the wheel zooms · Alt + arrows move a box",
            ),
        );
        return legend;
    }

    /** What has the focus, by the key that finds it again after a redraw. */
    private _focusKey(): string | null {
        const active = document.activeElement;
        if (!(active instanceof HTMLElement) || !this._root.contains(active)) {
            return null;
        }
        return active.dataset["key"] ?? (active.dataset["node"] ? `node:${active.dataset["node"]}` : null);
    }

    private _refocus(key: string | null): void {
        if (!key) {
            return;
        }
        const found = key.startsWith("node:")
            ? this._root.querySelector<HTMLElement>(`[data-node="${CSS.escape(key.slice(5))}"]`)
            : this._root.querySelector<HTMLElement>(`[data-key="${CSS.escape(key)}"]`);
        // The focus comes back to what had it, where it stands: the page is not scrolled to it.
        found?.focus({ preventScroll: true });
    }
}
