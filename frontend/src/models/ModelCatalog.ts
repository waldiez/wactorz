/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The stored models as the page knows them: the list, and the one model open
 * with its versions and the moves of its pointers.
 *
 * The server is the one source of what is shown. A pointer that moves anywhere
 * (here, the CLI, the chat, an agent) is published on `models/<name>/<pointer>`
 * and relayed to the page; that says only "this changed", and the catalogue
 * asks the server again. A pointer drawn from the message would be a second
 * copy of the store's own account of it.
 *
 * Reads overlap: a move here and its echo from main each ask again, and a
 * slow answer can arrive after a newer one. Each read is numbered, and only
 * the answer to the newest is taken, so an older one never puts back what a
 * newer one replaced.
 *
 * No DOM: the view reads `state` and is told when it changes.
 */
import type { Answer } from "../io/api";
import type { ModelsApi } from "../io/modelsApi";
import type { ModelAction, ModelDetail, ModelSummary, PointerMove } from "../types/models";

/** What there is to show. */
export interface CatalogState {
    /** Every stored model, or null before the first answer. */
    models: ModelSummary[] | null;
    /** Why the list could not be read, or "". */
    listError: string;
    /** The model open, or "" for none. */
    open: string;
    /** The open model in full, or null until it is read (or when it could not be). */
    detail: ModelDetail | null;
    /** The moves of its pointers, newest first. */
    moves: PointerMove[];
    /** Why the open model could not be read, or "". */
    detailError: string;
    /** Whether a move asked for here is on its way. */
    moving: boolean;
}

/** Told each time the state changes. */
export type CatalogListener = (state: CatalogState) => void;

const EMPTY: CatalogState = {
    models: null,
    listError: "",
    open: "",
    detail: null,
    moves: [],
    detailError: "",
    moving: false,
};

export class ModelCatalog {
    private _state: CatalogState = EMPTY;
    private readonly _listeners = new Set<CatalogListener>();
    /** The number of the newest read of the list, and of the open model. */
    private _listReads = 0;
    private _openReads = 0;

    constructor(private readonly api: Pick<ModelsApi, "list" | "detail" | "moves" | "move">) {}

    /** The state as it stands. Replaced, never changed in place. */
    get state(): CatalogState {
        return this._state;
    }

    /** Be told each change; returns the way to stop. */
    subscribe(listener: CatalogListener): () => void {
        this._listeners.add(listener);
        return () => this._listeners.delete(listener);
    }

    /** Read the list again, and the open model with it. */
    async refresh(): Promise<void> {
        await Promise.all([this._readList(), this._readOpen()]);
    }

    /** Open `name`, or none for "", and read it. */
    async open(name: string): Promise<void> {
        if (name === this._state.open) {
            return;
        }
        this._set({ open: name, detail: null, moves: [], detailError: "" });
        await this._readOpen();
    }

    /** A pointer of `name` moved somewhere: read what changed. */
    async moved(name: string): Promise<void> {
        await Promise.all([this._readList(), name === this._state.open ? this._readOpen() : undefined]);
    }

    /**
     * Ask for `action` on the model `name`; the server's refusal, or "" once it
     * took. Named by the caller rather than read from what is open: the person
     * confirmed a move of the model whose button they pressed, and another may
     * have been opened while they were asked.
     */
    async act(name: string, action: ModelAction, reason = ""): Promise<string> {
        if (!name || this._state.moving) {
            return "";
        }
        this._set({ moving: true });
        const done = await this.api.move(name, action, reason);
        this._set({ moving: false });
        if (!done.ok) {
            return done.error;
        }
        await this.moved(name);
        return "";
    }

    private async _readList(): Promise<void> {
        const read = ++this._listReads;
        const got = await this.api.list();
        // A newer read was asked for meanwhile: its answer is the one to show.
        if (read !== this._listReads) {
            return;
        }
        this._set(got.ok ? { models: got.value, listError: "" } : { listError: got.error });
    }

    private async _readOpen(): Promise<void> {
        const name = this._state.open;
        if (!name) {
            return;
        }
        const read = ++this._openReads;
        const [detail, moves] = await Promise.all([this.api.detail(name), this.api.moves(name)]);
        // A newer read was asked for meanwhile, of this model or of another opened
        // since: these are older than what it gives.
        if (read !== this._openReads) {
            return;
        }
        this._set(opened(detail, moves));
    }

    private _set(change: Partial<CatalogState>): void {
        this._state = { ...this._state, ...change };
        this._listeners.forEach(listener => listener(this._state));
    }
}

/** What reading a model gave: it and its moves, or why it could not be read. */
function opened(detail: Answer<ModelDetail>, moves: Answer<PointerMove[]>): Partial<CatalogState> {
    if (!detail.ok) {
        return { detail: null, moves: [], detailError: detail.error };
    }
    // The history is a part the model can be shown without.
    return { detail: detail.value, moves: moves.ok ? moves.value : [], detailError: "" };
}
