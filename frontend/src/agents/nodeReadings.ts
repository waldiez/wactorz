/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * A node's readings out of whatever carried them: its heartbeat over MQTT, or
 * the server's listing of it. Both use the heartbeat's field names.
 */
import type { NodeReadings } from "../types/agent";

/** The numeric readings, by the names a heartbeat uses. */
const NUMERIC = [
    "cpu_pct",
    "mem_used_mb",
    "mem_free_mb",
    "swap_used_mb",
    "load_1m",
    "load_5m",
    "disk_free_mb",
    "temp_c",
] as const;

/**
 * The readings ``payload`` carries. A value that is not a finite number, or a
 * throttle list that is not a list, is left out: absent means "not known",
 * which is not the same as zero.
 */
export function readingsFrom(payload: Record<string, unknown>): NodeReadings {
    const readings: NodeReadings = {};
    for (const key of NUMERIC) {
        const v = payload[key];
        if (typeof v === "number" && Number.isFinite(v)) {
            readings[key] = v;
        }
    }
    const throttled = payload["throttled"];
    if (Array.isArray(throttled)) {
        readings.throttled = throttled.filter((f): f is string => typeof f === "string");
    }
    return readings;
}
