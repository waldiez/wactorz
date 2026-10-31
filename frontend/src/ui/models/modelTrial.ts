/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * A model's shadow trial on screen: what the one under way shows so far, and how
 * the earlier ones ended. Figures come as the server worked them out, and one it
 * has no value for reads as a dash, not a zero.
 *
 * Read when the model is, and when Refresh is pressed: the pairs are files on
 * the server, and reading them on a timer would be work nobody asked for.
 */
import type { PastTrial, ShadowTrial, TrialSide } from "../../types/models";
import { timeLabel } from "../../time";
import { button, el, labelled, list, listItem } from "../dom";

/** What the trial card shows, and what it can ask for. */
export interface TrialHost {
    /** The trial's figures, or null until they are read (or when they could not be). */
    trial: ShadowTrial | null;
    /** Why they could not be read, or "". */
    error: string;
    /** Whether a read is on its way. */
    reading: boolean;
    /** Read the figures again. */
    refresh(): void;
}

/** What no value reads as. */
const NONE = "–";

function when(seconds: number): string {
    return timeLabel(seconds * 1000);
}

function percent(share: number | null): string {
    return share === null ? NONE : `${(share * 100).toFixed(1)}%`;
}

function count(value: number): string {
    return value.toLocaleString();
}

function fixed(value: number | null, digits = 2): string {
    return value === null ? NONE : value.toFixed(digits);
}

/** Active's figure, then shadow's, the order a person weighs them in. */
function sides(pair: TrialSide<string>): string {
    return `${pair.active} → ${pair.shadow}`;
}

/** One figure: its name, its value and, when there is one, what the value means. */
function stat(name: string, value: string, detail: string, warn = false): HTMLElement {
    const box = el("div", `af-trial-stat${warn ? " warn" : ""}`);
    box.append(el("dt", "af-trial-stat-name", name), el("dd", "af-trial-stat-value", value));
    if (detail) {
        box.appendChild(el("dd", "af-trial-stat-detail", detail));
    }
    return box;
}

/** The figures of a trial with pairs to read. */
function figures(trial: ShadowTrial): HTMLElement {
    const grid = el("dl", "af-trial-stats");
    const { latency, score } = trial;
    grid.append(
        stat("Agreement", percent(trial.agreement), `on ${count(trial.pairs)} inputs`),
        stat(
            "Flagged",
            sides({ active: count(trial.flagged.active), shadow: count(trial.flagged.shadow) }),
            "active → shadow",
        ),
        stat("Shadow errors", count(trial.shadow_errors), "", trial.shadow_errors > 0),
        stat(
            "P95 latency",
            sides({ active: fixed(latency.active.p95_ms, 1), shadow: fixed(latency.shadow.p95_ms, 1) }),
            "ms, active → shadow",
        ),
    );
    if (score.scored > 0) {
        grid.appendChild(
            stat(
                "Mean score",
                sides({ active: fixed(score.active_mean), shadow: fixed(score.shadow_mean) }),
                `over ${count(score.scored)} inputs both scored`,
            ),
        );
    }
    return grid;
}

/** What goes under the heading: the figures, or why there are none. */
function body(host: TrialHost): HTMLElement {
    if (host.error) {
        return el("p", "af-model-none", `Could not read the trial: ${host.error}`);
    }
    if (!host.trial) {
        return el("p", "af-model-none", "Reading the trial…");
    }
    if (!host.trial.pairs) {
        return el(
            "p",
            "af-model-none",
            "No pairs yet: a serving agent keeps one for each input it answers during the trial.",
        );
    }
    return figures(host.trial);
}

/** The trial under way, of `shadow` beside `active` (null when no version is served). */
export function trialCard(shadow: number, active: number | null, host: TrialHost): HTMLElement {
    const card = labelled(el("section", "af-model-card af-model-trial"), "Shadow trial");
    const head = el("div", "af-model-trial-head");
    const beside = active === null ? "" : ` beside v${active}`;
    const title = el("h3", "", `Shadow trial: v${shadow}${beside}`);
    const refresh = button("af-mini-btn", host.reading ? "Reading…" : "Refresh");
    refresh.disabled = host.reading;
    refresh.addEventListener("click", () => host.refresh());
    head.append(title, refresh);
    card.appendChild(head);
    if (host.trial) {
        const span = `${when(host.trial.since)} to ${when(host.trial.until)}`;
        card.appendChild(el("p", "af-model-trial-span", span));
    }
    card.appendChild(body(host));
    return card;
}

/** What an ended trial showed, or why it shows nothing. */
function shownBy(trial: PastTrial): string {
    if (trial.pairs_unavailable) {
        return `its pairs could not be read: ${trial.pairs_unavailable}`;
    }
    return trial.pairs ? `${percent(trial.agreement)} agreed on ${count(trial.pairs)} inputs` : "no pairs";
}

/** One ended trial as a line: which versions, and how it ended. */
export function pastTrialLine(trial: PastTrial): string {
    const beside = trial.active_version === null ? "" : ` beside v${trial.active_version}`;
    return `v${trial.shadow_version}${beside}, ${trial.ended}`;
}

/** The trials that ended, newest first: how each ended, then when and what it showed. */
export function pastTrials(trials: PastTrial[]): HTMLElement {
    const card = labelled(el("section", "af-model-card af-model-past-trials"), "Past trials");
    card.appendChild(el("h3", "", "Past trials"));
    const lines = list(el("ul"), "Past trials");
    for (const trial of trials) {
        const line = pastTrialLine(trial);
        const item = listItem(el("li", `af-model-move ${trial.ended}`), line);
        item.append(
            el("span", "af-model-move-what", line),
            el(
                "span",
                "af-model-move-when",
                `${when(trial.since)} to ${when(trial.until)} · ${shownBy(trial)}`,
            ),
        );
        lines.appendChild(item);
    }
    card.appendChild(lines);
    return card;
}
