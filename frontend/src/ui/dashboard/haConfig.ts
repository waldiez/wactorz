/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Bootstrap of Home Assistant credentials from the server's `/api/config`
 * endpoint — the single source of truth for HA config seeding.
 *
 * Policy: the server's `.env` value is authoritative, but a deliberate local
 * edit (Settings) must survive reloads. We record the last value we seeded under
 * `${key}__server` and only re-adopt the server value when it *changes* from that
 * baseline — so a changed `.env` propagates while a manual edit is preserved.
 */

/** Apply the seed policy for one key against localStorage. Returns whether it wrote. */
export function seedKeyFromServer(key: string, value: string | undefined | null): boolean {
    if (!value) {
        return false;
    }
    const baselineKey = `${key}__server`;
    // Server value unchanged since we last seeded → leave the active key alone so
    // a deliberate local edit survives.
    if (value === localStorage.getItem(baselineKey)) {
        return false;
    }
    localStorage.setItem(key, value);
    localStorage.setItem(baselineKey, value);
    return true;
}

/** Fetch `/api/config` and seed the HA url/token. Returns whether anything changed. */
export async function seedHaConfigFromServer(): Promise<boolean> {
    try {
        const ingress: string = window.__WACTORZ_INGRESS_PATH ?? "";
        const resp = await fetch(`${ingress}/api/config`);
        if (!resp.ok) {
            return false;
        }
        const cfg = (await resp.json()) as { ha?: { url?: string; token?: string } };
        const urlChanged = seedKeyFromServer("wactorz-ha-url", cfg.ha?.url);
        const tokenChanged = seedKeyFromServer("wactorz-ha-token", cfg.ha?.token);
        return urlChanged || tokenChanged;
    } catch {
        return false; // server may not be ready yet
    }
}
