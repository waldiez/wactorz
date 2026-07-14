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

import { buildIdentityView } from "./identityView";

export interface SwidConfig {
    /** Callback to re-render the view when needed. */
    onRender: () => void;
    /** Dashboard view registry (registerView from CardDashboard). */
    registerView: (key: string, icon: string, label: string, builder: () => HTMLElement) => void;
}

/**
 * Bootstrap the SWID extension. Called once from main.ts during startup.
 * The "key" icon is pre-registered as a built-in — no registerIcon call needed.
 */
export function register(config: SwidConfig): void {
    config.registerView("identity", "key", "Identity", () => buildIdentityView());
}
