/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Stored models, as `GET /api/models` and its siblings give them
 * (`wactorz/web/api_models.py` over `wactorz/core/artifacts.py`).
 */

/** The two pointers a model has: the version served, and the one on trial beside it. */
export type PointerName = "active" | "shadow";

/** The pointers there are, in the order they are shown. */
export const POINTER_NAMES: readonly PointerName[] = ["active", "shadow"];

/** One stored version of a model. */
export interface ModelVersion {
    name: string;
    version: number;
    sha256: string;
    format: string;
    /** Bytes. */
    size: number;
    /** Whether loading it runs code (pickle, joblib, torch). */
    runs_code: boolean;
    /** Seconds since the epoch. */
    added_at: number;
    /** Whatever the one who added it said of it: where it came from, how good it is. */
    metadata: Record<string, unknown>;
    /** `name:version`. */
    reference: string;
}

/** Where one pointer stands, and the move that put it there. */
export interface PointerState {
    name: string;
    pointer: PointerName;
    version: number;
    /** The version it named before this move, or null. */
    previous: number | null;
    /** Seconds since the epoch. */
    moved_at: number;
    /** Who moved it: `cli`, `chat`, `dashboard`, `agent:<name>`. */
    by: string;
    reason: string | null;
    /** Whether that version still holds the bytes the pointer was set to. */
    holds: boolean;
}

/** The pointers a model has set; an unset one is absent. */
export type Pointers = Partial<Record<PointerName, PointerState>>;

/** A model as the list shows it. */
export interface ModelSummary {
    name: string;
    newest: ModelVersion;
    /** How many versions are stored. */
    versions: number;
    pointers: Pointers;
}

/** One model in full: every version, oldest first, and its pointers. */
export interface ModelDetail {
    name: string;
    versions: ModelVersion[];
    pointers: Pointers;
}

/** One move of a pointer, from the provenance graph. */
export interface PointerMove {
    /** Seconds since the epoch. */
    at: number;
    pointer: PointerName;
    /** `moved` to `version`, `promoted` (shadow made active, so shadow unset) or `cleared`. */
    change: "moved" | "promoted" | "cleared";
    /** The version it moved to; null when it was unset. */
    version: number | null;
    /** The version it named before, or null. */
    from: number | null;
    by: string;
    reason: string | null;
}

/** A move the dashboard can ask for. */
export type ModelAction =
    | { kind: "shadow"; version: number }
    | { kind: "promote" }
    | { kind: "rollback" }
    | { kind: "clear-shadow" };
