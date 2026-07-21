/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * SWID extension — frontend module.
 *
 * Adds an "Identity" tab showing the DID minting index from the server-side
 * SWID extension. The panel lists agent/device/space identities with their
 * DID, handle, and creation timestamp.
 */

import { registerIcon } from "../../ui/dashboard/icons";
import { buildIdentityView } from "./identityView";

export interface SwidConfig {
    /** Callback to re-render the view when needed. */
    onRender: () => void;
    /** Dashboard view registry (registerView from CardDashboard). */
    registerView: (key: string, icon: string, label: string, builder: () => HTMLElement) => void;
}

/**
 * Bootstrap the SWID extension. Called once from main.ts during startup.
 */
export function register(config: SwidConfig): void {
    registerIcon(
        "key",
        '<path d="M2 18v3c0 .6.4 1 1 1h4v-3h3v-3h2l1.4-1.4a6.5 6.5 0 1 0-4-4Z"/><circle cx="16.5" cy="7.5" r=".5" fill="currentColor"/>',
    );

    config.registerView("identity", "key", "Identity", () => buildIdentityView());
}
