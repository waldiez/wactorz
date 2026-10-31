/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * One model on screen: a card with its name, its moves as buttons and its
 * versions along a line, the served, the tried and the previous one marked;
 * its trial, while there is one; below them, the table of versions beside the
 * history of its pointers, and how earlier trials ended.
 *
 * Every word set here is text: a model's name, its metadata and a move's
 * reason were written by whoever stored or moved it.
 */
import type { ModelAction, ModelDetail, ModelVersion, PastTrial, PointerMove } from "../../types/models";
import { POINTER_NAMES } from "../../types/models";
import { modelOffers, roleOf, versionOffer, type Offer, type VersionRole } from "../../models/pointers";
import { button, el, labelled, list, listItem } from "../dom";
import { humanSize } from "../dashboard/uploads";
import { timeLabel } from "../../time";
import { pastTrials, trialCard, type TrialHost } from "./modelTrial";

/** What the detail needs from the view: to ask for a move. */
export interface DetailHost {
    /** The person pressed `offer`'s button. */
    choose(offer: Offer): void;
    /** Whether a move is on its way, so no other is asked for meanwhile. */
    moving: boolean;
    /** The trial under way, for its card. */
    trial: TrialHost;
    /** How earlier trials ended, newest first. */
    pastTrials: PastTrial[];
}

/** When something was, in Unix seconds, as the page says times. */
function when(seconds: number): string {
    return timeLabel(seconds * 1000);
}

/** How a move's button stands out: serving a new version leads, going back warns. */
const TONE: Partial<Record<ModelAction["kind"], string>> = { promote: " primary", rollback: " danger" };

/** The button for `offer`, and why it cannot be pressed when it cannot. */
function offerButton(offer: Offer, host: DetailHost, small = false): HTMLElement {
    const btn = button(
        `af-mini-btn${small ? "" : ` af-model-action${TONE[offer.action.kind] ?? ""}`}`,
        offer.label,
    );
    btn.disabled = host.moving || offer.refused !== null;
    btn.addEventListener("click", () => host.choose(offer));
    if (!offer.refused) {
        return btn;
    }
    const why = el("span", "af-model-refused", offer.refused);
    const wrap = el("span", "af-model-offer");
    wrap.append(btn, why);
    return wrap;
}

/** A pointer whose version no longer holds its bytes, said for each. */
function warnings(detail: ModelDetail): HTMLElement[] {
    return POINTER_NAMES.flatMap(which => {
        const pointer = detail.pointers[which];
        if (!pointer || pointer.holds) {
            return [];
        }
        const said = `${which} v${pointer.version} no longer holds the bytes it was set to: agents will not follow it.`;
        return [el("p", "af-model-warning", said)];
    });
}

/** The card at the top: the name, what was said of it, the moves, and the versions over time. */
function header(detail: ModelDetail, host: DetailHost): HTMLElement {
    const head = el("section", "af-model-card af-model-head");
    head.appendChild(el("h2", "af-model-name", detail.name));
    const description = detail.versions.at(-1)?.metadata["description"];
    if (typeof description === "string" && description) {
        head.appendChild(el("p", "af-model-description", description));
    }
    head.append(...warnings(detail));
    const actions = labelled(el("div", "af-model-actions"), "Moves");
    actions.setAttribute("role", "group");
    modelOffers(detail).forEach(offer => actions.appendChild(offerButton(offer, host)));
    if (actions.childElementCount) {
        head.appendChild(actions);
    }
    head.appendChild(timeline(detail));
    return head;
}

/** The words a version's mark says. */
const MARK: Record<VersionRole, string> = { active: "active", shadow: "shadow", previous: "previous" };

/**
 * Every version along a line, oldest first, the served, tried and previous ones
 * marked. Every step has the same three rows (mark, circle, date), the mark's
 * left empty where there is none, so the circles sit on one line.
 */
function timeline(detail: ModelDetail): HTMLElement {
    const line = list(el("ol", "af-model-timeline"), "Versions over time");
    for (const version of detail.versions) {
        const role = roleOf(detail.pointers, version.version);
        const said = role ? `v${version.version}, ${MARK[role]}` : `v${version.version}`;
        const item = listItem(el("li", `af-model-step${role ? ` ${role}` : ""}`), said);
        item.append(
            el("span", "af-model-mark", role ? MARK[role] : ""),
            el("span", "af-model-dot", `v${version.version}`),
            el("span", "af-model-when", when(version.added_at)),
        );
        line.appendChild(item);
    }
    return line;
}

/** Who made `version`, as whoever stored it said (`source`): `cli`, an agent, the train step. */
function madeBy(version: ModelVersion): string {
    const source = version.metadata["source"];
    return typeof source === "string" ? source : "";
}

/** What else was said of `version`, as `key=value` pairs; its description and source have their own place. */
function said(version: ModelVersion): string {
    return Object.entries(version.metadata)
        .filter(([key]) => key !== "description" && key !== "source")
        .map(([key, value]) => `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`)
        .join(" · ");
}

/** One version's row: its number and mark, what it is, and the move it offers. */
function versionRow(detail: ModelDetail, version: ModelVersion, host: DetailHost): HTMLElement {
    const role = roleOf(detail.pointers, version.version);
    const row = el("tr", role ?? "");
    const name = el("td", "af-model-version", `v${version.version}`);
    if (role) {
        name.appendChild(el("span", "af-model-role", ` ${MARK[role]}`));
    }
    const offer = versionOffer(detail, version.version);
    const action = el("td", "af-model-row-action");
    if (offer) {
        action.appendChild(offerButton(offer, host, true));
    }
    row.append(
        name,
        el("td", "", when(version.added_at)),
        el("td", "", madeBy(version)),
        el("td", "", version.runs_code ? `${version.format} (runs code)` : version.format),
        el("td", "", humanSize(version.size)),
        el("td", "af-model-said", said(version)),
        action,
    );
    return row;
}

/** The table of versions, newest first, each with the move it offers. */
function versionsTable(detail: ModelDetail, host: DetailHost): HTMLElement {
    const card = el("section", "af-model-card af-model-versions-card");
    const table = el("table", "af-model-versions");
    table.appendChild(el("caption", "", "Versions"));
    const head = el("tr");
    for (const title of ["Version", "Added", "Made by", "Format", "Size", "Said of it", ""]) {
        head.appendChild(el("th", "", title));
    }
    table.appendChild(el("thead")).appendChild(head);
    const body = table.appendChild(el("tbody"));
    [...detail.versions].reverse().forEach(version => body.appendChild(versionRow(detail, version, host)));
    card.appendChild(table);
    return card;
}

/** One move as a line: what moved where, and who moved it. */
export function moveLine(move: PointerMove): string {
    const was = move.from != null ? ` (was v${move.from})` : "";
    if (move.change === "promoted") {
        return `shadow v${move.from ?? "?"} promoted, by ${move.by}`;
    }
    if (move.change === "cleared") {
        return `${move.pointer} cleared${was}, by ${move.by}`;
    }
    return `${move.pointer} → v${move.version ?? "?"}${was}, by ${move.by}`;
}

/** The moves of the model's pointers, newest first: what moved, then when and why. */
function history(moves: PointerMove[]): HTMLElement {
    const section = el("section", "af-model-card af-model-moves");
    section.appendChild(el("h3", "", "Pointer moves"));
    if (!moves.length) {
        section.appendChild(el("p", "af-model-none", "No pointer has moved yet."));
        return section;
    }
    const lines = list(el("ul"), "Pointer moves");
    for (const move of moves) {
        const line = moveLine(move);
        const item = listItem(el("li", `af-model-move ${move.pointer}`), line);
        const detail = el("span", "af-model-move-when", when(move.at));
        if (move.reason) {
            detail.append(" · ", el("span", "af-model-reason", `“${move.reason}”`));
        }
        item.append(el("span", "af-model-move-what", line), detail);
        lines.appendChild(item);
    }
    section.appendChild(lines);
    return section;
}

/** The whole of one model. */
export function buildModelDetail(detail: ModelDetail, moves: PointerMove[], host: DetailHost): HTMLElement {
    const root = el("article", "af-model-detail");
    root.setAttribute("aria-label", detail.name);
    const below = el("div", "af-model-below");
    below.append(versionsTable(detail, host), history(moves));
    root.appendChild(header(detail, host));
    const { active, shadow } = detail.pointers;
    if (shadow) {
        root.appendChild(trialCard(shadow.version, active?.version ?? null, host.trial));
    }
    root.appendChild(below);
    if (host.pastTrials.length) {
        root.appendChild(pastTrials(host.pastTrials));
    }
    return root;
}
