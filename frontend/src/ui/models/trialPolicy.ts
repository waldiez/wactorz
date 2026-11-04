/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * A trial held against its model's policy, as the Models tab shows it: what the
 * policy asks, what the trial shows for each term, and whether main will promote
 * or ask. A model with no policy is told how to set one.
 */
import type { PolicyTerm, PolicyVerdict, ShadowTrial } from "../../types/models";
import { el, labelled, list, listItem } from "../dom";
import { pValue } from "./trialFigures";

/** What no value reads as. */
const NONE = "–";

function figure(value: number | boolean | null, digits = 3): string {
    if (value === null) {
        return NONE;
    }
    if (typeof value === "boolean") {
        return value ? "yes" : "no";
    }
    return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(digits);
}

/** The policy's terms in a person's words, by the name the server gives each. */
const TERMS: Record<PolicyTerm["name"], string> = {
    judged: "Judged against the labels",
    margin: "Gain on the metric",
    min_days: "Days on trial",
    min_labelled: "Labelled inputs",
    significant: "Difference not chance",
};

/** One term: its name, what the trial shows, and what the policy asks. */
function termLine(term: PolicyTerm, verdict: PolicyVerdict): HTMLElement {
    const metric = verdict.metric;
    const name = term.name === "margin" ? `Gain on ${metric}` : TERMS[term.name];
    let shown: string;
    if (term.name === "judged") {
        shown = term.actual ? "yes" : "no";
    } else if (term.name === "significant") {
        const test = verdict.test === "sign" ? "Sign test" : "McNemar";
        shown = term.wanted
            ? typeof term.actual === "number"
                ? `${test} ${pValue(term.actual)}`
                : `${test} p = ${NONE}`
            : "not required";
    } else {
        shown = `${figure(term.actual)} (asks ${figure(term.wanted)})`;
    }
    const item = listItem(el("li", term.met ? "met" : "short"), name);
    item.append(el("span", "af-trial-policy-term", `${name}: `), el("span", "", shown));
    return item;
}

/** What main does when the policy is met, and who set it. */
function whatFollows(verdict: PolicyVerdict): string {
    const { policy } = verdict;
    const does = policy.automatic ? "main promotes the shadow on its own" : "main asks on the dashboard";
    const metric = policy.metric ?? `${verdict.metric}, the main figure of its kind,`;
    return `The policy asks for ${metric} by ${policy.margin} over ${policy.min_days} day${policy.min_days === 1 ? "" : "s"} and ${policy.min_labelled} labelled inputs; when it is met, ${does}. Set by ${policy.by || "nobody"}.`;
}

/** How to set a policy, for a model that has none. */
function howToSet(name: string): HTMLElement {
    const hint = el("p", "af-model-none", "No policy: the shadow becomes active only by hand. Set one with ");
    hint.append(
        el("code", "", `wactorz-artifacts policy ${name} --margin 0.02`),
        " or ",
        el("code", "", `/artifacts policy ${name} margin=0.02`),
        ".",
    );
    return hint;
}

/** Whether the trial meets the policy, in a line. */
export function policyVerdict(verdict: PolicyVerdict): string {
    if (!verdict.reported) {
        return `The policy cannot be met: a ${verdict.kind ?? "model"} reports no ${verdict.metric}. Set its metric to one of ${verdict.metrics.join(", ")}.`;
    }
    const sides =
        verdict.shadow === null || verdict.active === null
            ? `${verdict.metric} not measured yet`
            : `${verdict.metric} ${figure(verdict.shadow)} against ${figure(verdict.active)}`;
    if (verdict.met) {
        const next = verdict.policy.automatic ? "main promotes it" : "main asks whether to promote it";
        return `The policy is met: ${sides}; ${next}.`;
    }
    const short = verdict.terms.filter(term => !term.met).length;
    return `The policy is not met yet: ${sides}; ${short} of ${verdict.terms.length} terms short.`;
}

/** The trial against its policy, or how to set one. */
export function policySection(trial: ShadowTrial): HTMLElement {
    const section = labelled(el("section", "af-trial-judged af-trial-policy"), "Against the policy");
    section.appendChild(el("h4", "", "Against the policy"));
    const verdict = trial.policy;
    if (!verdict) {
        section.appendChild(howToSet(trial.name));
        return section;
    }
    section.appendChild(el("p", "af-trial-verdict", policyVerdict(verdict)));
    const terms = list(el("ul", "af-trial-policy-terms"), "Policy terms");
    for (const term of verdict.terms) {
        terms.appendChild(termLine(term, verdict));
    }
    section.append(terms, el("p", "af-trial-judged-basis", whatFollows(verdict)));
    return section;
}
