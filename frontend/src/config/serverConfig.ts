/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Bootstrap runtime config from the server's `/api/config` endpoint.
 *
 * The response is small (a handful of key/value pairs) — we fetch it once and
 * seed every registered field into localStorage so the rest of the UI can
 * read them synchronously without further network round-trips.
 *
 * Each seeded key is paired with a `__server` baseline so we can tell a
 * user's local edit apart from an actual server-side `.env` change and only
 * overwrite when the server value itself has changed.
 *
 * Core registers only its own keys here (the HA URL). Extensions contribute
 * theirs via ``registerConfigEntry()`` at module load — before
 * ``seedServerConfig()`` runs — so this file never names an extension.
 * Keys not registered are **never** stored, even if the server sends them.
 */

import { safeStorage } from "../safeStorage";
import { UPLOADS_KEY } from "../ui/dashboard/uploads";

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

/** Extract a storable string from the parsed `/api/config` payload. */
export type ConfigExtract = (cfg: Record<string, unknown>) => string | undefined;

const _entries = new Map<string, ConfigExtract>();

/** Register a localStorage key to seed from `/api/config`. Extensions call
 *  this at module load, before `seedServerConfig()` runs. */
export function registerConfigEntry(key: string, extract: ConfigExtract): void {
    _entries.set(key, extract);
}

// Core entry — the HA URL for the external Devices link (never a token).
registerConfigEntry(
    "wactorz-ha-url",
    c => (c.ha as Record<string, unknown> | undefined)?.url as string | undefined,
);

// Core entry — whether the server registered its upload routes, which is what
// decides if the attachment UI can work at all. Seeded as "1"/"0" rather than
// "1"/absent: `seedKeyFromServer` ignores an empty value, so an absent one would
// leave a stale "1" behind and keep offering uploads after they were turned off.
registerConfigEntry(UPLOADS_KEY, c =>
    (c.uploads as Record<string, unknown> | undefined)?.enabled ? "1" : "0",
);

/** Fetch `/api/config` and seed every registered client-side key from it.
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
        for (const [key, extract] of _entries) {
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
