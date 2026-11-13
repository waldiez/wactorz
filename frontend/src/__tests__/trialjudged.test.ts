/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * A trial judged against the labels, as the Models tab shows it: each side's
 * figures with their intervals, whether the sides really differ, and what to do
 * when nothing says how the model is judged.
 */
import { describe, expect, it } from "vitest";

import type {
    ClassifierJudged,
    ClassifierSide,
    DetectorJudged,
    DetectorSide,
    RegressorJudged,
    RegressorSide,
    ShadowTrial,
} from "../types/models";
import { amount } from "../ui/models/trialFigures";
import { duration, judgedOn, judgedSection, verdict } from "../ui/models/trialJudged";
import { regressorVerdict } from "../ui/models/trialRegressor";
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

const JUDGED = (overrides: Partial<DetectorJudged> = {}): DetectorJudged => ({
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
        const sure = JUDGED({ mcnemar: { active_only: 0, shadow_only: 28, p: 7.4e-9, better: "shadow" } });
        expect(verdict(sure)).toBe(
            "The shadow is better: shadow right alone on 28 inputs, active on 0; p < 0.001.",
        );
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

const CLASS_SIDE = (overrides: Partial<ClassifierSide> = {}): ClassifierSide => ({
    right: 3,
    accuracy: 0.6,
    accuracy_interval: [0.23, 0.88],
    no_answer: 1,
    macro_f1: 0.65,
    weighted_f1: 0.62,
    per_class: {
        kettle: { precision: 1, recall: 1 / 3, f1: 0.5, support: 3 },
        washer: { precision: 2 / 3, recall: 1, f1: 0.8, support: 2 },
    },
    confusion: [
        [1, 1, 1],
        [0, 2, 0],
    ],
    ...overrides,
});

const CLASSIFIED = (overrides: Partial<ClassifierJudged> = {}): ClassifierJudged => ({
    kind: "classifier",
    from: "meter:2",
    judged_on: {
        kind: "classifier",
        positive: [],
        field: "appliance",
        source: "meter",
        by: "cli",
        at: 0,
        from_version: null,
    },
    positive: [],
    labelled: 5,
    unlabelled: 1,
    sources: ["meter"],
    classes: ["kettle", "washer"],
    active: CLASS_SIDE(),
    shadow: CLASS_SIDE({
        right: 4,
        accuracy: 0.8,
        no_answer: 0,
        confusion: [
            [3, 0, 0],
            [1, 1, 0],
        ],
    }),
    mcnemar: { active_only: 1, shadow_only: 2, p: 1, better: null },
    ...overrides,
});

const REGRESSION_SIDE = (overrides: Partial<RegressorSide> = {}): RegressorSide => ({
    judged: 4,
    no_answer: 0,
    mae: 15,
    rmse: 18.708,
    bias: 0,
    worst: 30,
    ...overrides,
});

const REGRESSED = (overrides: Partial<RegressorJudged> = {}): RegressorJudged => ({
    kind: "regressor",
    from: "co2:3",
    judged_on: {
        kind: "regressor",
        positive: [],
        field: "co2",
        source: null,
        by: "cli",
        at: 0,
        from_version: null,
    },
    positive: [],
    labelled: 4,
    unlabelled: 1,
    sources: ["room-1"],
    not_numbers: 1,
    active: REGRESSION_SIDE(),
    shadow: REGRESSION_SIDE({ judged: 3, no_answer: 1, mae: 13.333, rmse: 14.142, bias: -2.5, worst: 20 }),
    closer: { active: 1, shadow: 12, ties: 1, p: 0.003, better: "shadow" },
    ...overrides,
});

describe("a classifier's trial judged against the labels", () => {
    it("shows accuracy, the F1 means and how often each side gave no answer", () => {
        const section = judgedSection(TRIAL({ labels: CLASSIFIED() }));

        expect(section.textContent).toContain(
            "Against appliance as a classifier, labels of meter, as meter:2 declares · 5 of 6 inputs labelled · 2 classes",
        );
        const overall = byRole(section, "table", "Overall");
        expect(byRole(overall, "rowheader", "Accuracy").closest("tr")?.textContent).toBe(
            "Accuracy0.600.23–0.880.800.23–0.88",
        );
        expect(byRole(overall, "rowheader", "Macro F1").closest("tr")?.textContent).toBe("Macro F10.650.65");
        expect(byRole(overall, "rowheader", "No answer").closest("tr")?.textContent).toBe("No answer10");
    });

    it("gives each class its inputs and its precision, recall and F1 on each side", () => {
        const perClass = byRole(judgedSection(TRIAL({ labels: CLASSIFIED() })), "table", "Per class");

        expect(byRole(perClass, "rowheader", "kettle").closest("tr")?.textContent).toBe(
            "kettle31.00 · 0.33 · 0.501.00 · 0.33 · 0.50",
        );
    });

    it("shows each side's confusion with a column for no answer, the right answers marked", () => {
        const section = judgedSection(TRIAL({ labels: CLASSIFIED() }));

        const active = byRole(section, "table", "Active: labelled ▸ answered");
        expect([...active.querySelectorAll("thead th")].map(th => th.textContent)).toEqual([
            "Labelled",
            "kettle",
            "washer",
            "No answer",
        ]);
        expect(byRole(active, "rowheader", "kettle").closest("tr")?.textContent).toBe("kettle111");
        const right = [...active.querySelectorAll("td.af-trial-confusion-right")].map(td => td.textContent);
        expect(right).toEqual(["1", "2"]);
        const shadow = byRole(section, "table", "Shadow: labelled ▸ answered");
        expect(byRole(shadow, "rowheader", "washer").closest("tr")?.textContent).toBe("washer110");
    });

    it("compares the sides on the inputs only one got right", () => {
        expect(judgedSection(TRIAL({ labels: CLASSIFIED() })).textContent).toContain(
            "No clear difference: active right alone on 1 inputs, shadow on 2; p = 1.000, so the difference could be chance.",
        );
    });
});

describe("a regressor's trial judged against the labels", () => {
    it("shows each side's errors and how many inputs it answered", () => {
        const section = judgedSection(TRIAL({ labels: REGRESSED() }));

        expect(section.textContent).toContain(
            "Against co2 as a regressor, labels of each input's source, as co2:3 declares · 4 of 6 inputs labelled · 1 label not a number, left out",
        );
        expect(section.textContent).not.toContain("95% interval");
        const errors = byRole(section, "table", "Errors");
        expect(byRole(errors, "rowheader", "Mean absolute error").closest("tr")?.textContent).toBe(
            "Mean absolute error15.0013.33",
        );
        expect(byRole(errors, "rowheader", "Bias").closest("tr")?.textContent).toBe("Bias0−2.50");
        expect(byRole(errors, "rowheader", "Answered").closest("tr")?.textContent).toBe(
            "Answered4 of 43 of 4",
        );
    });

    it("says which side was nearer the truth, and whether that could be chance", () => {
        expect(judgedSection(TRIAL({ labels: REGRESSED() })).textContent).toContain(
            "The shadow is closer: shadow nearer on 12 inputs, active on 1, 1 input tied; p = 0.003.",
        );
        const even = REGRESSED({ closer: { active: 4, shadow: 5, ties: 0, p: 1, better: null } });
        expect(regressorVerdict(even)).toBe(
            "No clear difference: active nearer on 4 inputs, shadow on 5; p = 1.000, so the difference could be chance.",
        );
        const active = REGRESSED({ closer: { active: 9, shadow: 0, ties: 0, p: 0.004, better: "active" } });
        expect(regressorVerdict(active)).toBe(
            "The active version is closer: active nearer on 9 inputs, shadow on 0; p = 0.004.",
        );
        const none = REGRESSED({ closer: { active: 0, shadow: 0, ties: 3, p: 1, better: null } });
        expect(regressorVerdict(none)).toContain("nothing tells them apart yet");
    });

    it("says when no label of its inputs is a number", () => {
        const section = judgedSection(TRIAL({ labels: REGRESSED({ labelled: 0, not_numbers: 4 }) }));

        expect(section.textContent).toContain("No label of the trial's inputs is a number");
        expect(allByRole(section, "table")).toHaveLength(0);
    });

    it("gives a quantity as many decimals as its size needs, and a bias its sign", () => {
        expect([amount(null), amount(1234.5), amount(42.137), amount(0.01234)]).toEqual([
            "–",
            "1,235",
            "42.14",
            "0.012",
        ]);
        expect([amount(2.5, true), amount(-2.5, true), amount(0, true)]).toEqual(["+2.50", "−2.50", "0"]);
    });
});

describe("a kind this dashboard has no figures for", () => {
    it("says so rather than showing the wrong figures", () => {
        const odd = { ...JUDGED(), kind: "ranker" } as unknown as DetectorJudged;

        const section = judgedSection(TRIAL({ labels: odd }));

        expect(section.textContent).toContain("Judged as a ranker, which this dashboard cannot show yet.");
        expect(allByRole(section, "table")).toHaveLength(0);
    });
});

describe("how to judge a model that answers with a class or a number", () => {
    it("names the kind and the field", () => {
        expect(judgedSection(TRIAL()).textContent).toContain(
            "wactorz-artifacts judge hvac:8 --kind classifier --field <field>",
        );
    });

    it("says what a classifier is judged on", () => {
        expect(
            judgedOn({
                kind: "regressor",
                positive: [],
                field: "watts",
                source: null,
                by: "",
                at: 0,
                from_version: null,
            }),
        ).toBe("watts as a regressor, labels of each input's source");
    });
});
