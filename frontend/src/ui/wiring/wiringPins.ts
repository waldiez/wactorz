/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Where a person moved the wiring's boxes, kept in this browser.
 *
 * Kept by node id, which names the same agent, topic or source on every
 * read, so a box stays where it was put across refreshes and reloads. Each
 * grouping keeps its own, since boxes stand in different places with lanes
 * and without. What is kept for a box no longer drawn is left alone, so it
 * is back in its place if the box comes back.
 */
import { safeStorage } from "../../safeStorage";
import type { Pin } from "../../wiring/layout";

/** Where the pins are kept. */
export const PINS_KEY = "wactorz-wiring-pins";

/** The pins of one grouping, by node id. */
export type KeptPins = Record<string, Pin>;

/** Each grouping's pins: `machine` with lanes, `none` without. */
export type StoredPins = Record<"machine" | "none", KeptPins>;

/** What storage holds, as pins: anything not shaped like one is left out. */
export function parsePins(text: string | null): StoredPins {
    const empty: StoredPins = { machine: {}, none: {} };
    try {
        const parsed: unknown = JSON.parse(text ?? "{}");
        if (!parsed || typeof parsed !== "object") {
            return empty;
        }
        for (const grouping of ["machine", "none"] as const) {
            const kept = (parsed as Record<string, unknown>)[grouping];
            if (!kept || typeof kept !== "object") {
                continue;
            }
            for (const [id, pin] of Object.entries(kept as Record<string, unknown>)) {
                const { x, y } = (pin ?? {}) as Partial<Pin>;
                if (
                    typeof x === "number" &&
                    typeof y === "number" &&
                    Number.isFinite(x) &&
                    Number.isFinite(y)
                ) {
                    empty[grouping][id] = { x, y };
                }
            }
        }
        return empty;
    } catch {
        return empty;
    }
}

/** The pins, read once and written on every change. */
export class WiringPins {
    private _stored: StoredPins = parsePins(safeStorage.get(PINS_KEY));

    /** The pins for a grouping, by node id. */
    of(grouped: boolean): Map<string, Pin> {
        return new Map(Object.entries(this._stored[grouped ? "machine" : "none"]));
    }

    /** Whether any box is pinned in a grouping. */
    any(grouped: boolean): boolean {
        return Object.keys(this._stored[grouped ? "machine" : "none"]).length > 0;
    }

    /** Pin the box `id` at `pin`, rounded to whole pixels. */
    set(grouped: boolean, id: string, pin: Pin): void {
        this._stored[grouped ? "machine" : "none"][id] = { x: Math.round(pin.x), y: Math.round(pin.y) };
        this._save();
    }

    /** Let every box of a grouping go back to where the layout puts it. */
    clear(grouped: boolean): void {
        this._stored[grouped ? "machine" : "none"] = {};
        this._save();
    }

    private _save(): void {
        safeStorage.set(PINS_KEY, JSON.stringify(this._stored));
    }
}
