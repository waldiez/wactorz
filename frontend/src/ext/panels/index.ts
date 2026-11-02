/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Dashboard tabs that installed packages add.
 *
 * Mirrors the server's `wactorz/web/panels.py`: `/api/config` lists the panels of
 * extensions that loaded, and each becomes a view whose script loads when it opens.
 */
import { registerConfigEntry } from "../../config/serverConfig";
import { emit } from "../../events";
import { safeStorage } from "../../safeStorage";
import { iconMarkup } from "../../ui/shell/icons";
import { PanelView, type PanelServices } from "./PanelView";
import type { ToastOptions } from "../../ui/ToastManager";
import type { PanelEntry, PanelStop, PanelToast } from "./types";

export { PANEL_API_VERSION } from "./types";
export type { PanelEntry, PanelHost, PanelModule, PanelStop, PanelToast } from "./types";

/** Where the list of panels is kept. */
export const PANELS_KEY = "wactorz-panels";

/** The icon of a tab that names one the dashboard does not have. */
const DEFAULT_ICON = "grid";

registerConfigEntry(
    PANELS_KEY,
    c => {
        const panels = (c.extensions as Record<string, unknown> | undefined)?.panels;
        return Array.isArray(panels) && panels.length > 0 ? JSON.stringify(panels) : undefined;
    },
    { clearWhenAbsent: true },
);

/** Whether `entry` is shaped like a panel. */
function isEntry(entry: unknown): entry is PanelEntry {
    const fields = (entry ?? {}) as Partial<Record<keyof PanelEntry, unknown>>;
    return (
        typeof fields.name === "string" && typeof fields.title === "string" && typeof fields.icon === "string"
    );
}

/** The panels in what the server sent, leaving out anything not shaped like one. */
export function parsePanels(stored: string | null): PanelEntry[] {
    try {
        const parsed: unknown = JSON.parse(stored ?? "[]");
        return Array.isArray(parsed) ? (parsed as unknown[]).filter(isEntry) : [];
    } catch {
        return [];
    }
}

/** A panel's notice, as the dashboard's toasts take it. */
export function toastOptions(notice: PanelToast): ToastOptions {
    const types = { info: "system", warning: "alert-warning", error: "alert-error" } as const;
    return { title: notice.title, message: notice.message, type: types[notice.type ?? "info"] };
}

/** A script is loaded by the browser, at an address only known once running. */
const loadScript: PanelServices["load"] = url => import(/* @vite-ignore */ url);

/**
 * Add a tab for each panel the server lists. Called once from app.ts, after
 * `/api/config` was read.
 *
 * @returns the way to stop them.
 */
export function register(services: Omit<PanelServices, "load"> & Partial<PanelServices>): PanelStop {
    const stops = parsePanels(safeStorage.get(PANELS_KEY)).map(entry => {
        const view = new PanelView(entry, { load: loadScript, ...services });
        const stop = view.start();
        emit("af-register-view", {
            key: entry.name,
            icon: iconMarkup(entry.icon) ? entry.icon : DEFAULT_ICON,
            label: entry.title,
            build: () => view.build(),
        });
        return stop;
    });
    return () => stops.forEach(stop => stop());
}
