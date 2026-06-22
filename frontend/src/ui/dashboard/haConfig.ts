/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Best-effort bootstrap of Home Assistant credentials from the server's
 * `/api/config` endpoint. Seeds localStorage only for keys the user has not
 * already set, so manual settings always win. Returns whether anything changed.
 */
export async function seedHaConfigFromServer(): Promise<boolean> {
    try {
        const ingress: string = (window as any).__WACTORZ_INGRESS_PATH ?? "";
        const resp = await fetch(`${ingress}/api/config`);
        if (!resp.ok) {
            return false;
        }
        const cfg = (await resp.json()) as { ha?: { url?: string; token?: string } };
        let changed = false;
        const seed = (key: string, val: string | undefined | null) => {
            if (val && !localStorage.getItem(key)) {
                localStorage.setItem(key, val);
                changed = true;
            }
        };
        seed("wactorz-ha-url", cfg.ha?.url);
        seed("wactorz-ha-token", cfg.ha?.token);
        return changed;
    } catch {
        return false; // server may not be ready yet
    }
}
