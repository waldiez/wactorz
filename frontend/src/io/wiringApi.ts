/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The wiring, from the server (`/api/wiring`): read from main's topic registry
 * when asked. A `503` says no actor system runs beside the monitor, so there is
 * no registry to read.
 */
import type { Wiring } from "../types/wiring";
import { apiUrl, requestJson, type Answer } from "./api";

/** Read the wiring as it stands. */
export function fetchWiring(): Promise<Answer<Wiring>> {
    return requestJson<Wiring>(apiUrl("/api/wiring"));
}
