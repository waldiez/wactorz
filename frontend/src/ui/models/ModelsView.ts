/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The Models view: every stored model in a list, and the one the address names
 * (`#/models/<name>`) beside it, or on a phone in its place.
 *
 * A model in the list is a link to its address, so opening one is following a
 * link: the router hears it and the dashboard opens it here. A move asked for
 * goes through a confirm dialog that takes an optional reason; the server's
 * refusal, if it refuses, is said in a notice. What a move changed arrives the
 * way any move's does, from the catalogue, which asks the server again.
 */
import type { ModelCatalog, CatalogState } from "../../models/ModelCatalog";
import type { ModelSummary } from "../../types/models";
import { statusOf, summaryLine, type Offer } from "../../models/pointers";
import { formatRoute } from "../../router";
import { confirmWithReason } from "../dashboard/confirmDialog";
import { sayRefusal } from "../dashboard/mutate";
import { el, labelled, list, named } from "../dom";
import { buildModelDetail } from "./modelDetail";

/** The address of `name`'s page; the list's, for "". */
export function modelHref(name: string): string {
    return formatRoute({ view: "models", path: name ? [name] : [] });
}

/** One model in the list: a link to it, its status, its pointers. */
function listRow(summary: ModelSummary, open: string): HTMLElement {
    const status = statusOf(summary.pointers);
    const link = el("a", `af-model-row${summary.name === open ? " open" : ""}`);
    link.href = modelHref(summary.name);
    if (summary.name === open) {
        link.setAttribute("aria-current", "page");
    }
    link.append(
        el("span", "af-model-row-name", summary.name),
        el("span", `af-model-status ${status.replace(" ", "-")}`, status),
        el("span", "af-model-row-line", summaryLine(summary)),
    );
    const item = el("li");
    item.appendChild(link);
    return item;
}

export class ModelsView {
    private readonly _root = el("div", "af-models");
    private readonly _filter = named(el("input", "af-models-filter"), "filter", "Filter models");
    private readonly _list = list(el("ul", "af-models-list"), "Stored models");
    private readonly _pane = labelled(el("section", "af-models-pane"), "Model");
    /** How many models are stored, beside the list's title. */
    private readonly _count = el("span", "af-models-count");
    private _unsubscribe: (() => void) | null = null;

    constructor(private readonly catalog: ModelCatalog) {
        this._filter.type = "search";
        this._filter.placeholder = "Filter by name";
        this._filter.addEventListener("input", () => this._drawList(this.catalog.state));
        const heading = el("header", "af-models-heading");
        heading.append(
            el("h1", "af-models-page-title", "Models"),
            el(
                "p",
                "af-models-lead",
                "Every stored version, which one is served, and which one is on trial beside it.",
            ),
        );
        const title = el("div", "af-models-side-head");
        title.append(el("h2", "af-models-title", "Stored models"), this._count);
        const side = el("aside", "af-models-side");
        side.append(title, this._filter, this._list);
        const columns = el("div", "af-models-columns");
        columns.append(side, this._pane);
        this._root.append(heading, columns);
    }

    /** The model open, for the address: "" for the list alone. */
    get open(): string {
        return this.catalog.state.open;
    }

    /** Open `name` ("" for none), as the address asks. */
    show(name: string): void {
        void this.catalog.open(name);
    }

    /** The view's element, read afresh from the server. Built once and kept, so
     *  the filter keeps what was typed in it across redraws of the dashboard. */
    build(): HTMLElement {
        this._unsubscribe ??= this.catalog.subscribe(state => this._draw(state));
        this._draw(this.catalog.state);
        void this.catalog.refresh();
        return this._root;
    }

    /** A pointer of `name` moved somewhere: show what changed, if the view is on screen. */
    moved(name: string): void {
        if (this._root.isConnected) {
            void this.catalog.moved(name);
        }
    }

    /** Stop following the catalogue (the dashboard goes). */
    destroy(): void {
        this._unsubscribe?.();
        this._unsubscribe = null;
    }

    private _draw(state: CatalogState): void {
        this._root.classList.toggle("has-open", Boolean(state.open));
        this._drawList(state);
        this._drawPane(state);
    }

    private _drawList(state: CatalogState): void {
        this._list.replaceChildren();
        const stored = state.models?.length ?? 0;
        this._count.textContent = state.models ? `${stored} ${stored === 1 ? "name" : "names"}` : "";
        if (state.listError) {
            this._list.appendChild(
                el("li", "af-model-none", `Could not read the models: ${state.listError}`),
            );
            return;
        }
        if (state.models === null) {
            this._list.appendChild(el("li", "af-model-none", "Reading the models…"));
            return;
        }
        const wanted = this._filter.value.trim().toLowerCase();
        const shown = state.models.filter(m => m.name.toLowerCase().includes(wanted));
        if (!shown.length) {
            const none = state.models.length ? "No model's name has that in it." : "No model is stored yet.";
            this._list.appendChild(el("li", "af-model-none", none));
            return;
        }
        shown.forEach(summary => this._list.appendChild(listRow(summary, state.open)));
    }

    private _drawPane(state: CatalogState): void {
        this._pane.replaceChildren();
        if (!state.open) {
            this._pane.appendChild(
                el("p", "af-model-none", "Pick a model to see its versions and pointers."),
            );
            return;
        }
        const back = el("a", "af-models-back", "← All models");
        back.href = modelHref("");
        this._pane.appendChild(back);
        if (state.detailError) {
            this._pane.appendChild(
                el("p", "af-model-none", `Could not read ${state.open}: ${state.detailError}`),
            );
        } else if (!state.detail) {
            this._pane.appendChild(el("p", "af-model-none", `Reading ${state.open}…`));
        } else {
            this._pane.appendChild(
                buildModelDetail(state.detail, state.moves, {
                    moving: state.moving,
                    choose: offer => void this._choose(offer),
                    pastTrials: state.pastTrials,
                    trial: {
                        trial: state.trial,
                        series: state.trialSeries,
                        error: state.trialError,
                        reading: state.trialReading,
                        refresh: () => void this.catalog.refreshTrial(),
                    },
                }),
            );
        }
    }

    /** Ask whether to make `offer`'s move, and why; then ask the server to make it on the
     *  model whose button was pressed, whatever is open by then. */
    private async _choose(offer: Offer): Promise<void> {
        const model = this.catalog.state.open;
        const question = { title: `${offer.label}?`, message: offer.consequence, confirmLabel: offer.label };
        const reason = await confirmWithReason(question);
        if (reason === null) {
            return;
        }
        const refused = await this.catalog.act(model, offer.action, reason);
        if (refused) {
            sayRefusal(refused, `${offer.label}: not done`, `${offer.label}: unconfirmed`);
        }
    }
}
