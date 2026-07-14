/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Bootstrap runtime config from the server's `/api/config` endpoint.
 *
 * The response is small (a handful of key/value pairs) — we fetch it once and
 * seed every whitelisted field into localStorage so the rest of the UI can
 * read them synchronously without further network round-trips.
 *
 * Each seeded key is paired with a `__server` baseline so we can tell a
 * user's local edit apart from an actual server-side `.env` change and only
 * overwrite when the server value itself has changed.
 *
 * CONFIG_ENTRIES is the single source of truth: add one line to bring a new
 * server field into localStorage. Fields not listed here are **never** stored,
 * even if the server starts sending them.
 */

import { safeStorage } from "../../safeStorage";

/** Seed a single key from the server value; returns whether it wrote. */
export function seedKeyFromServer(key: string, value: string | undefined | null): boolean {
    if (!value) {
        return false;
    }
    const baselineKey = `${key}__server`;
    if (value === safeStorage.get(baselineKey)) {
        return false;
    }
    safeStorage.set(key, value);
    safeStorage.set(baselineKey, value);
    return true;
}

const CONFIG_ENTRIES = [
    [
        "wactorz-ha-url",
        (c: Record<string, unknown>) =>
            (c.ha as Record<string, unknown> | undefined)?.url as string | undefined,
    ],
    [
        "wactorz-fuseki-url",
        (c: Record<string, unknown>) =>
            (c.fuseki as Record<string, unknown> | undefined)?.url as string | undefined,
    ],
    [
        "wactorz-fuseki-dataset",
        (c: Record<string, unknown>) =>
            (c.fuseki as Record<string, unknown> | undefined)?.dataset as string | undefined,
    ],
] as const;

/** Fetch `/api/config` and seed every whitelisted client-side key from it.
 *  Returns whether the HA URL changed (the caller uses this to refresh the
 *  Devices nav link). */
export async function seedServerConfig(): Promise<boolean> {
    try {
        const ingress: string = window.__WACTORZ_INGRESS_PATH ?? "";
        const resp = await fetch(`${ingress}/api/config`);
        if (!resp.ok) {
            return false;
        }
        const cfg = (await resp.json()) as Record<string, unknown>;
        let haChanged = false;
        for (const [key, extract] of CONFIG_ENTRIES) {
            const changed = seedKeyFromServer(key, extract(cfg));
            if (key === "wactorz-ha-url" && changed) {
                haChanged = true;
            }
        }
        return haChanged;
    } catch {
        return false; // server may not be ready yet
    }
}
