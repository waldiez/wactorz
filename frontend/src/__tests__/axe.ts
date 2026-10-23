/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Accessibility checks with axe, under the rule the end-to-end suite applies:
 * a serious or critical finding fails, unless it is one accepted, with its
 * reason, in `e2e/harness/axe-accepted.json`.
 *
 * happy-dom lays nothing out, so what axe can judge here is structure — roles,
 * names, ARIA that is valid and allowed where it is. Colour contrast and
 * landmarks are left to the browser, where the whole page is on screen.
 */
import axe from "axe-core";

import accepted from "../../../e2e/harness/axe-accepted.json";

/** A finding someone decided to live with, and why. */
export interface AcceptedFinding {
    /** The axe rule, such as `aria-allowed-role`. */
    rule: string;
    /** Part of the CSS selector axe reports for the element. */
    selector: string;
    /** Why it stays, for whoever reads the list next. */
    reason: string;
}

const ACCEPTED: AcceptedFinding[] = accepted;

/** Impacts that fail a check. */
const BLOCKING = new Set(["serious", "critical"]);

/** Rules that need a laid-out page, so cannot be judged without one. */
const NEEDS_LAYOUT = { "color-contrast": { enabled: false }, region: { enabled: false } };

/** Whether `target` is covered by an accepted finding for `rule`. */
export function isAccepted(rule: string, target: string, list: AcceptedFinding[] = ACCEPTED): boolean {
    return list.some(a => a.rule === rule && target.includes(a.selector));
}

/** The serious and critical findings under `root` that nobody accepted, one line each. */
export async function blockingFindings(root: Element): Promise<string[]> {
    const result = await axe.run(root, { rules: NEEDS_LAYOUT, resultTypes: ["violations"] });
    return result.violations
        .filter(v => BLOCKING.has(v.impact ?? ""))
        .flatMap(v =>
            v.nodes
                .map(n => n.target.join(" "))
                .filter(target => !isAccepted(v.id, target))
                .map(target => `${v.id} (${v.impact}): ${target} — ${v.help}`),
        );
}
