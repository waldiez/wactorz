/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Fuseki extension — frontend module.
 *
 * Adds a "Graph" tab to the dashboard that queries the server-side Apache
 * Jena Fuseki triple store through the ``/api/fuseki/<dataset>/sparql`` proxy.
 */

import { registerIcon } from "../../ui/dashboard/icons";
import { buildFusekiView } from "./fusekiView";

export interface FusekiConfig {
    /** Fuseki base URL (from /api/config — read-only display). */
    url: string | null;
    /** Dataset name (the only client-side setting). */
    dataset: string;
    /** Callback to re-render the view when the dataset changes. */
    onRender: () => void;
    /** Dashboard view registry (registerView from CardDashboard). */
    registerView: (key: string, icon: string, label: string, builder: () => HTMLElement) => void;
}

/**
 * Bootstrap the Fuseki extension. Called once from main.ts during startup.
 */
export function register(config: FusekiConfig): void {
    if (!config.url) {
        return;
    }

    registerIcon(
        "hexagon",
        '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>',
    );

    config.registerView("fuseki", "hexagon", "Graph", () => buildFusekiView(config.onRender));
}
