/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/** Shared dashboard types. */

import type { IconName } from "./icons";

/** View identifier — core views are predefined, extensions register custom ones. */
export type View = string;

/** Built-in views that are always present. Settings always renders last. */
export const BUILTIN_VIEWS: { key: View; label: string; icon: IconName }[] = [
    { key: "overview", label: "Overview", icon: "grid" },
    { key: "feed", label: "Feed", icon: "list" },
    { key: "chat", label: "Chat", icon: "chat" },
];
export const SETTINGS_VIEW = { key: "settings" as View, label: "Settings", icon: "settings" as IconName };

export type ConnState = "live" | "connecting" | "demo";
