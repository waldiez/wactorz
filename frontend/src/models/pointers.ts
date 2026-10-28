/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * What a model's pointers mean for the person looking at it: which version is
 * served, which is on trial beside it, which one a rollback returns to, and
 * which moves can be made from here.
 *
 * The rules are the store's (`wactorz/core/artifacts.py`): a promote makes the
 * shadow active and unsets shadow; a rollback returns active to the version it
 * left; a version is tried by setting shadow to it. Making a version active
 * directly is left to the CLI and the chat: from here a version is served by
 * being tried first.
 */
import type { ModelAction, ModelDetail, ModelSummary, Pointers } from "../types/models";

/** What a model is doing: trying a version beside the served one, serving one, or neither. */
export type ModelStatus = "on trial" | "serving" | "no active";

/** What `pointers` say the model is doing. Without an active pointer, agents load the newest. */
export function statusOf(pointers: Pointers): ModelStatus {
    if (pointers.shadow) {
        return "on trial";
    }
    return pointers.active ? "serving" : "no active";
}

/** What a version is to its model, in the words the page shows it with. */
export type VersionRole = "active" | "shadow" | "previous";

/** What `version` is to the model `pointers` describe; null when it is none of them. */
export function roleOf(pointers: Pointers, version: number): VersionRole | null {
    if (pointers.active?.version === version) {
        return "active";
    }
    if (pointers.shadow?.version === version) {
        return "shadow";
    }
    return pointers.active?.previous === version ? "previous" : null;
}

/** A move the page offers, with what its button and its question say. */
export interface Offer {
    action: ModelAction;
    /** The button: it names the version the move puts in place. */
    label: string;
    /** What confirming does, in full. */
    consequence: string;
    /** Whether the move records why; clearing a pointer does not. */
    asksWhy: boolean;
    /** Why it cannot be made now, or null when it can. */
    refused: string | null;
}

/** Whether `detail` still stores `version`. */
function stores(detail: ModelDetail, version: number): boolean {
    return detail.versions.some(v => v.version === version);
}

/** The moves of the whole model: promote, clear shadow, roll back; only those that apply. */
export function modelOffers(detail: ModelDetail): Offer[] {
    const { name, pointers } = detail;
    const offers: Offer[] = [];
    const shadow = pointers.shadow;
    if (shadow) {
        offers.push({
            action: { kind: "promote" },
            label: `Promote v${shadow.version}`,
            consequence: `${name} v${shadow.version} becomes the version agents serve, and the trial ends.`,
            asksWhy: true,
            // A pointer whose version no longer holds its bytes is reported, never followed.
            refused: shadow.holds ? null : `v${shadow.version} no longer holds the bytes it was tried with.`,
        });
        offers.push({
            action: { kind: "clear-shadow" },
            label: "Clear shadow",
            consequence: `The trial of ${name} v${shadow.version} ends; the served version stays.`,
            asksWhy: false,
            refused: null,
        });
    }
    const previous = pointers.active?.previous;
    if (previous != null) {
        offers.push({
            action: { kind: "rollback" },
            label: `Roll back to v${previous}`,
            consequence: `Agents serve ${name} v${previous} again, in place of v${pointers.active?.version}.`,
            asksWhy: true,
            refused: stores(detail, previous) ? null : `v${previous} is no longer stored.`,
        });
    }
    return offers;
}

/** The move offered on one version: trying it as shadow, unless it is already active or on trial. */
export function versionOffer(detail: ModelDetail, version: number): Offer | null {
    const role = roleOf(detail.pointers, version);
    if (role === "active" || role === "shadow") {
        return null;
    }
    return {
        action: { kind: "shadow", version },
        label: `Try v${version} as shadow`,
        consequence: detail.pointers.shadow
            ? `${detail.name} v${version} is tried beside the served version, in place of v${detail.pointers.shadow.version}.`
            : `${detail.name} v${version} is tried beside the served version.`,
        asksWhy: true,
        refused: null,
    };
}

/** The line under a model's name in the list: its pointers and how many versions it has. */
export function summaryLine(summary: ModelSummary): string {
    const parts: string[] = [];
    if (summary.pointers.active) {
        parts.push(`active v${summary.pointers.active.version}`);
    }
    if (summary.pointers.shadow) {
        parts.push(`shadow v${summary.pointers.shadow.version}`);
    }
    if (!summary.pointers.active) {
        // With no active version, agents that name the model get the newest.
        parts.push(`newest v${summary.newest.version}`, "serves newest");
    }
    parts.push(summary.versions === 1 ? "1 version" : `${summary.versions} versions`);
    return parts.join(" · ");
}
