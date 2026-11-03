/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * A trial judged against the labels, as the Models tab shows it: each side's
 * figures with their intervals, whether the sides really differ, and what to do
 * when nothing says how the model is judged.
 */
import { describe, expect, it } from "vitest";

import type { DetectorSide, ShadowTrial, TrialJudged } from "../types/models";
import { duration, judgedOn, judgedSection, verdict } from "../ui/models/trialJudged";
import { allByRole, byRole } from "./queries";

const SIDE = (overrides: Partial<DetectorSide> = {}): DetectorSide => ({
    tp: 7,
    fp: 3,
    fn: 2,
    tn: 88,
    precision: 0.7,
    recall: 0.78,
    f1: 0.74,
    false_positive_rate: 0.033,
    specificity: 0.967,
    mcc: 0.7,
    intervals: { precision: [0.4, 0.89], recall: [0.45, 0.94], false_positive_rate: [0.01, 0.09] },
    alarms_without_fault: 3,
    alarms_per_day: 1.5,
    faults: {
        seen: 3,
        caught: 2,
        recall: 0.67,
        recall_interval: [0.21, 0.94],
        delay_s: { mean: 300, median: 300, max: 420 },
    },
    ...overrides,
});

const JUDGED = (overrides: Partial<TrialJudged> = {}): TrialJudged => ({
    kind: "detector",
    from: "hvac:8",
    judged_on: { kind: "detector", positive: ["fault"], source: null, by: "cli", at: 0, from_version: null },
    positive: ["fault"],
    labelled: 100,
    positives: 9,
    unlabelled: 20,
    sources: ["pump-1"],
    days: 2,
    active: SIDE(),
    shadow: SIDE({
        precision: 0.89,
        intervals: { precision: [0.6, 0.98], recall: null, false_positive_rate: null },
    }),
    mcnemar: { active_only: 1, shadow_only: 9, p: 0.021, better: "shadow" },
    ...overrides,
});

const TRIAL = (overrides: Partial<ShadowTrial> = {}): ShadowTrial => ({
    name: "hvac",
    shadow: {
        name: "hvac",
        pointer: "shadow",
        version: 8,
        previous: null,
        moved_at: 0,
        by: "cli",
        reason: null,
        holds: true,
    },
    since: 0,
    until: 1,
    pairs: 120,
    agreement: 0.9,
    flagged: { active: 10, shadow: 9 },
    shadow_errors: 0,
    latency: { active: { mean_ms: 1, p95_ms: 2 }, shadow: { mean_ms: 1, p95_ms: 2 } },
    score: { active_mean: null, shadow_mean: null, difference_mean: null, scored: 0 },
    ...overrides,
});

describe("a trial judged against the labels", () => {
    it("shows each side's figures with their intervals, and what they rest on", () => {
        const section = judgedSection(TRIAL({ labels: JUDGED() }));

        expect(section.textContent).toContain(
            "Against fault labels of each input's source, as hvac:8 declares · 100 of 120 inputs labelled · 3 faults seen",
        );
        const table = byRole(section, "table", "Per input");
        const precision = byRole(table, "rowheader", "Precision").closest("tr");
        expect(precision?.textContent).toBe("Precision0.700.40–0.890.890.60–0.98");
        expect(byRole(table, "rowheader", "Recall").closest("tr")?.textContent).toBe(
            "Recall0.780.45–0.940.78",
        );
        const operator = byRole(section, "table", "Per fault and per day");
        expect(byRole(operator, "rowheader", "Faults caught").closest("tr")?.textContent).toContain("2 of 3");
        expect(byRole(operator, "rowheader", "Detection delay").closest("tr")?.textContent).toContain(
            "5 min",
        );
        expect(byRole(operator, "rowheader", "False alarms per day").closest("tr")?.textContent).toContain(
            "1.5",
        );
        expect(section.textContent).toContain(
            "The shadow is better: shadow right alone on 9 inputs, active on 1; p = 0.021.",
        );
    });

    it("counts alarms rather than a rate when the trial is too short for one", () => {
        const short = SIDE({ alarms_per_day: null, alarms_without_fault: 4 });
        const section = judgedSection(TRIAL({ labels: JUDGED({ active: short, shadow: short }) }));

        const row = byRole(section, "rowheader", "False alarms per day").closest("tr");
        expect(row?.textContent).toBe("False alarms per day4 in the trial4 in the trial");
    });

    it("says when no input has a label yet", () => {
        const section = judgedSection(TRIAL({ labels: JUDGED({ labelled: 0 }) }));

        expect(section.textContent).toContain("No input of the trial has a label yet");
        expect(allByRole(section, "table")).toHaveLength(0);
    });

    it("says how to judge a model nothing says it of", () => {
        const section = judgedSection(TRIAL());

        expect(section.textContent).toContain("wactorz-artifacts judge hvac:8 --positive fault");
        expect(section.textContent).toContain("/artifacts judge hvac:8 fault");
    });

    it("names the model alone when no version is on trial", () => {
        expect(judgedSection(TRIAL({ shadow: null })).textContent).toContain("judge hvac --positive");
    });

    it("says why the labels could not be read", () => {
        const section = judgedSection(TRIAL({ labels_unavailable: "persistence is not running" }));

        expect(section.textContent).toContain("The labels could not be read: persistence is not running");
    });

    it("says it was judged as asked rather than as declared", () => {
        expect(judgedSection(TRIAL({ labels: JUDGED({ from: "request" }) })).textContent).toContain(
            "as asked",
        );
    });
});

describe("whether the sides differ", () => {
    it("names the better side only when the difference is not chance", () => {
        const active = JUDGED({ mcnemar: { active_only: 9, shadow_only: 1, p: 0.021, better: "active" } });
        const chance = JUDGED({ mcnemar: { active_only: 3, shadow_only: 5, p: 0.727, better: null } });
        const close = JUDGED({ mcnemar: { active_only: 30, shadow_only: 31, p: 0.04, better: null } });
        const same = JUDGED({ mcnemar: { active_only: 0, shadow_only: 0, p: 1, better: null } });

        expect(verdict(active)).toBe(
            "The active version is better: active right alone on 9 inputs, shadow on 1; p = 0.021.",
        );
        expect(verdict(chance)).toContain("No clear difference");
        expect(verdict(chance)).toContain("could be chance");
        expect(verdict(close)).not.toContain("could be chance");
        expect(verdict(same)).toBe("Both sides were right on the same inputs: nothing tells them apart yet.");
    });
});

describe("the words for a judgement and a delay", () => {
    it("says what a version is judged on", () => {
        const judgement = {
            kind: "detector",
            positive: ["fault", "bearing"],
            by: "cli",
            at: 0,
            from_version: null,
        };

        expect(judgedOn({ ...judgement, source: null })).toBe("fault, bearing labels of each input's source");
        expect(judgedOn({ ...judgement, source: "line-3" })).toBe("fault, bearing labels of line-3");
    });

    it("reads a delay in seconds, minutes or hours", () => {
        expect([null, 45, 600, 9000].map(duration)).toEqual(["–", "45 s", "10 min", "2.5 h"]);
    });
});
