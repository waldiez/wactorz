/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * A trial held against its model's policy, as the Models tab shows it: each
 * term met or short, what main does when all are, and how to set a policy for
 * a model that has none.
 */
import { describe, expect, it } from "vitest";

import type { Policy, PolicyVerdict, ShadowTrial } from "../types/models";
import { policySection, policyVerdict } from "../ui/models/trialPolicy";
import { allByRole, byRole } from "./queries";

const POLICY = (overrides: Partial<Policy> = {}): Policy => ({
    metric: "f1",
    margin: 0.02,
    min_days: 1,
    min_labelled: 100,
    significant: true,
    automatic: false,
    by: "cli",
    at: 0,
    ...overrides,
});

const VERDICT = (overrides: Partial<PolicyVerdict> = {}): PolicyVerdict => ({
    met: true,
    metric: "f1",
    kind: "detector",
    reported: true,
    metrics: ["f1", "precision", "recall", "mcc", "false_positive_rate"],
    test: "mcnemar",
    shadow: 0.91,
    active: 0.85,
    gain: 0.06,
    terms: [
        { name: "judged", wanted: true, actual: true, met: true },
        { name: "margin", wanted: 0.02, actual: 0.06, met: true },
        { name: "min_days", wanted: 1, actual: 2.1, met: true },
        { name: "min_labelled", wanted: 100, actual: 340, met: true },
        { name: "significant", wanted: true, actual: 0.003, met: true },
    ],
    policy: POLICY(),
    words: "met: f1 0.91 vs 0.85 (+0.06, margin 0.02), 2.1 day(s), 340 labelled, McNemar p=0.003",
    ...overrides,
});

const TRIAL = (overrides: Partial<ShadowTrial> = {}): ShadowTrial => ({
    name: "hvac",
    shadow: null,
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

describe("a trial against its policy", () => {
    it("lists every term met, and says main will ask", () => {
        const section = policySection(TRIAL({ policy: VERDICT() }));

        expect(section.textContent).toContain(
            "The policy is met: f1 0.910 against 0.850; main asks whether to promote it.",
        );
        const terms = byRole(section, "list", "Policy terms");
        const items = allByRole(terms, "listitem");
        expect(items.map(item => item.className)).toEqual(["met", "met", "met", "met", "met"]);
        expect(byRole(terms, "listitem", "Gain on f1").textContent).toBe("Gain on f1: 0.060 (asks 0.020)");
        expect(byRole(terms, "listitem", "Days on trial").textContent).toBe("Days on trial: 2.100 (asks 1)");
        expect(byRole(terms, "listitem", "Labelled inputs").textContent).toBe(
            "Labelled inputs: 340 (asks 100)",
        );
        expect(byRole(terms, "listitem", "Difference not chance").textContent).toBe(
            "Difference not chance: McNemar p = 0.003",
        );
        expect(section.textContent).toContain(
            "asks for f1 by 0.02 over 1 day and 100 labelled inputs; when it is met, main asks on the dashboard. Set by cli.",
        );
    });

    it("marks the terms short, and counts them", () => {
        const verdict = VERDICT({
            met: false,
            terms: [
                { name: "judged", wanted: true, actual: true, met: true },
                { name: "margin", wanted: 0.02, actual: 0.01, met: false },
                { name: "min_days", wanted: 3, actual: 2.1, met: false },
                { name: "min_labelled", wanted: 100, actual: 340, met: true },
                { name: "significant", wanted: false, actual: 0.4, met: true },
            ],
            policy: POLICY({ min_days: 3, significant: false, automatic: true }),
        });
        const section = policySection(TRIAL({ policy: verdict }));

        expect(section.textContent).toContain(
            "The policy is not met yet: f1 0.910 against 0.850; 2 of 5 terms short.",
        );
        const items = allByRole(byRole(section, "list", "Policy terms"), "listitem");
        expect(items.map(item => item.className)).toEqual(["met", "short", "short", "met", "met"]);
        expect(byRole(section, "listitem", "Difference not chance").textContent).toContain("not required");
        expect(section.textContent).toContain(
            "over 3 days and 100 labelled inputs; when it is met, main promotes the shadow on its own",
        );
    });

    it("says what to set for a model with no policy", () => {
        const section = policySection(TRIAL());

        expect(section.textContent).toContain("No policy: the shadow becomes active only by hand.");
        expect(section.textContent).toContain("wactorz-artifacts policy hvac --margin 0.02");
        expect(section.querySelector("ul")).toBeNull();
    });

    it("reads a trial not judged as not measured, and the automatic policy as promoting", () => {
        const verdict = VERDICT({
            met: false,
            shadow: null,
            active: null,
            gain: null,
            terms: [
                { name: "judged", wanted: true, actual: false, met: false },
                { name: "margin", wanted: 0.02, actual: null, met: false },
                { name: "min_days", wanted: 1, actual: 2.1, met: true },
                { name: "min_labelled", wanted: 100, actual: 0, met: false },
                { name: "significant", wanted: true, actual: 1, met: false },
            ],
        });

        expect(policyVerdict(verdict)).toBe(
            "The policy is not met yet: f1 not measured yet; 4 of 5 terms short.",
        );
        expect(policyVerdict(VERDICT({ policy: POLICY({ automatic: true }) }))).toBe(
            "The policy is met: f1 0.910 against 0.850; main promotes it.",
        );
        const section = policySection(TRIAL({ policy: verdict }));
        expect(byRole(section, "listitem", "Judged against the labels").textContent).toBe(
            "Judged against the labels: no",
        );
        expect(byRole(section, "listitem", "Gain on f1").textContent).toBe("Gain on f1: – (asks 0.020)");
    });
});

describe("a policy on a classifier or a regressor", () => {
    it("names the sign test a regressor's significance reads", () => {
        const verdict = VERDICT({ kind: "regressor", metric: "mae", metrics: ["mae", "rmse"], test: "sign" });

        const section = policySection(TRIAL({ policy: verdict }));

        const term = byRole(section, "listitem", "Difference not chance");
        expect(term.textContent).toBe("Difference not chance: Sign test p = 0.003");
        const sure = VERDICT({
            terms: [{ name: "significant", wanted: true, actual: 2e-16, met: true }],
        });
        expect(
            byRole(policySection(TRIAL({ policy: sure })), "listitem", "Difference not chance").textContent,
        ).toBe("Difference not chance: McNemar p < 0.001");
    });

    it("says which figure a policy that names none compares on", () => {
        const verdict = VERDICT({ kind: "classifier", metric: "macro_f1", policy: POLICY({ metric: null }) });

        expect(policySection(TRIAL({ policy: verdict })).textContent).toContain(
            "The policy asks for macro_f1, the main figure of its kind, by 0.02 over 1 day",
        );
    });

    it("says a figure the kind does not report can never be met, and which it does", () => {
        const verdict = VERDICT({
            met: false,
            kind: "classifier",
            metric: "mae",
            reported: false,
            metrics: ["macro_f1", "weighted_f1", "accuracy"],
            shadow: null,
            active: null,
            gain: null,
        });

        expect(policyVerdict(verdict)).toBe(
            "The policy cannot be met: a classifier reports no mae. Set its metric to one of macro_f1, weighted_f1, accuracy.",
        );
    });
});
