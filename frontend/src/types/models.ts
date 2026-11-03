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
    /** What it is judged on; absent or null when nothing says. */
    judged_on?: Judgement | null;
}

/** What a version is judged on: the labels its answers are held against (`wactorz/core/judgement.py`). */
export interface Judgement {
    /** How its answers are read: a `detector` flags an input by publishing something for it. */
    kind: string;
    /** The label values that count as what it should flag. */
    positive: string[];
    /** The one label source every input is judged against; null for each input's own. */
    source: string | null;
    /** Who said so: `cli`, `chat`, `request`. */
    by: string;
    /** Seconds since the epoch. */
    at: number;
    /** The version it was copied from when this one was stored without one. */
    from_version: number | null;
}

/** A rate's 95% interval, `[low, high]`, or null with nothing to count. */
export type RateInterval = [number, number] | null;

/** One side of a detector's trial, judged against the labels. */
export interface DetectorSide {
    tp: number;
    fp: number;
    fn: number;
    tn: number;
    precision: number | null;
    recall: number | null;
    f1: number | null;
    false_positive_rate: number | null;
    specificity: number | null;
    /** The Matthews correlation, honest when faults are rare. */
    mcc: number | null;
    intervals: { precision: RateInterval; recall: RateInterval; false_positive_rate: RateInterval };
    /** Alarms raised where no known fault was, and per day of the trial's event time. */
    alarms_without_fault: number;
    alarms_per_day: number | null;
    /** Faults (stretches a positive label holds) the inputs fell in, and how many were caught, how soon. */
    faults: {
        seen: number;
        caught: number;
        recall: number | null;
        recall_interval: RateInterval;
        delay_s: { mean: number | null; median: number | null; max: number | null };
    };
}

/** A trial judged against the labels, as its version declares or as the request asked. */
export interface TrialJudged extends TrialSide<DetectorSide> {
    kind: string;
    /** The version whose declaration it was judged on (`hvac:8`), or `request`. */
    from: string;
    judged_on: Judgement;
    positive: string[];
    /** The inputs a label covered, those whose label counts as positive, and those none covered. */
    labelled: number;
    positives: number;
    unlabelled: number;
    /** The label sources the judged inputs were held against. */
    sources: string[];
    /** How many days of event time the inputs span. */
    days: number;
    /** Whether the two sides really differ, from the inputs only one judged rightly. */
    mcnemar: { active_only: number; shadow_only: number; p: number; better: "active" | "shadow" | null };
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
    }; /** Each side judged against the labels, when a version says what it is judged on. */
    labels?: TrialJudged;
    /** Why the labels could not be read, when a version said how to judge it. */
    labels_unavailable?: string;
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

/** One stretch of a trial, as `GET /api/models/{name}/trial/series` gives it. */
export interface TrialBucket {
    /** The middle of the stretch, in seconds since the epoch. */
    ts: number;
    /** How many inputs came in it; values are null when none did. */
    pairs: number;
    active_score: number | null;
    shadow_score: number | null;
    active_ms: number | null;
    shadow_ms: number | null;
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
