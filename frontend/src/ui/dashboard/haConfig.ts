/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Bootstrap the Home Assistant URL from the server's `/api/config` endpoint.
 *
 * The URL is the only HA value the browser needs — the Devices nav button links
 * straight to the HA UI, so no token is ever fetched or stored. The server's
 * value is authoritative; we record it under `wactorz-ha-url__server` and only
 * re-write when it changes, so a stale value can't override a fresh `.env`.
 */

import { safeStorage } from "../../safeStorage";

/** Seed the HA URL key from the server value; returns whether it wrote. */
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

/** Fetch `/api/config` and seed the HA URL. Returns whether it changed. */
export async function seedHaConfigFromServer(): Promise<boolean> {
    try {
        const ingress: string = window.__WACTORZ_INGRESS_PATH ?? "";
        const resp = await fetch(`${ingress}/api/config`);
        if (!resp.ok) {
            return false;
        }
        const cfg = (await resp.json()) as {
            ha?: { url?: string };
            fuseki?: { url?: string; dataset?: string };
        };
        // Seed all keys (each is a no-op when the server omits the value), then
        // report whether anything changed. Kept as separate calls so short-
        // circuiting can't skip a seed.
        const haChanged = seedKeyFromServer("wactorz-ha-url", cfg.ha?.url);
        const fkUrl = seedKeyFromServer("wactorz-fuseki-url", cfg.fuseki?.url);
        const fkDs = seedKeyFromServer("wactorz-fuseki-dataset", cfg.fuseki?.dataset);
        return haChanged || fkUrl || fkDs;
    } catch {
        return false; // server may not be ready yet
    }
}
