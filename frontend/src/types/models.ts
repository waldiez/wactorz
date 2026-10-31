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

/** A figure for each side of a trial. */
export interface TrialSide<T> {
    /** The served version's. */
    active: T;
    /** The tried version's. */
    shadow: T;
}

/** How long one side took, in milliseconds; null with nothing to measure. */
export interface TrialLatency {
    mean_ms: number | null;
    p95_ms: number | null;
}

/** What a trial's pairs show, under way or ended. */
export interface TrialFigures {
    /** The inputs both sides answered. */
    pairs: number;
    /** The share of pairs where both answered alike; null with no pairs. */
    agreement: number | null;
    /** How many inputs each side published something for. */
    flagged: TrialSide<number>;
    /** How many inputs the tried version failed on. */
    shadow_errors: number;
    latency: TrialSide<TrialLatency>;
    /** Read from a `score` field in the outputs, over the pairs where both gave one. */
    score: {
        active_mean: number | null;
        shadow_mean: number | null;
        difference_mean: number | null;
        scored: number;
    };
}

/** The trial under way, as `GET /api/models/{name}/trial` gives it. */
export interface ShadowTrial extends TrialFigures {
    name: string;
    /** The pointer on trial; null for a window read after it was cleared. */
    shadow: PointerState | null;
    /** Seconds since the epoch. */
    since: number;
    until: number;
}

/** A trial that ended, as `GET /api/models/{name}/trials` gives it. */
export interface PastTrial extends TrialFigures {
    id: string;
    /** Seconds since the epoch. */
    since: number;
    until: number;
    /** Who ended it. */
    by: string;
    shadow_version: number;
    active_version: number | null;
    /** Whether the tried version was made active, or the trial let go. */
    ended: "promoted" | "cleared";
    /** Why its pairs could not be read when it ended, so its figures are empty. */
    pairs_unavailable?: string;
}

/** A move the dashboard can ask for. */
export type ModelAction =
    | { kind: "shadow"; version: number }
    | { kind: "promote" }
    | { kind: "rollback" }
    | { kind: "clear-shadow" };
