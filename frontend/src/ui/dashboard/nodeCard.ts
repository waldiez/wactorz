/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * A remote node's card on the overview: what its machine is (from its
 * manifest), how it is doing now (from its heartbeat), its trend over the last
 * hour, and its agents. Opening it shows the node's full history.
 *
 * Everything here arrives from the node over MQTT or from the server's listing
 * of it, so all of it is set as text. A field the node did not send is left
 * out rather than shown as zero.
 */
import type { NodeReadings, RemoteNode } from "../../types/agent";
import { button, el } from "../dom";
import { buildSparkline, lastValue, SERIES_COLORS, type Point } from "./trend";

/** A node's trends over the last hour, as its card draws them. */
export interface NodeTrend {
    /** CPU in use, in percent. */
    cpu: Point[];
    /** Memory available, in MiB. */
    free: Point[];
}

/** Everything a node card is built from. */
export interface NodeCardData extends RemoteNode {
    /** The node's name. */
    name: string;
    /** Whether it has been heard from lately. */
    online: boolean;
    /** Its trend, once fetched. */
    trend?: NodeTrend;
}

function str(manifest: Record<string, unknown>, key: string): string | null {
    const v = manifest[key];
    return typeof v === "string" && v ? v : null;
}

function num(value: unknown): number | null {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** A size given in MiB, in the unit a person reads it in. */
export function megabytes(mb: number): string {
    return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(0)} MB`;
}

/** What the machine is, in one line: model, architecture, system and Python. */
export function machineLine(manifest: Record<string, unknown>): string {
    const parts = [str(manifest, "model"), str(manifest, "arch"), str(manifest, "os_release")];
    const python = str(manifest, "python");
    if (python) {
        parts.push(`Python ${python}`);
    }
    return parts.filter((p): p is string => p !== null).join(" · ");
}

/** How much machine there is, in one line: CPUs, memory, accelerators, and whether it is a container. */
export function capacityLine(manifest: Record<string, unknown>): string {
    const parts: string[] = [];
    const cpus = num(manifest["cpu_count"]);
    if (cpus !== null) {
        parts.push(`${cpus} CPU${cpus === 1 ? "" : "s"}`);
    }
    const ram = num(manifest["ram_total_mb"]);
    if (ram !== null) {
        parts.push(`${megabytes(ram)} memory`);
    }
    const gpus = manifest["gpu"];
    if (Array.isArray(gpus)) {
        for (const g of gpus) {
            if (g && typeof g === "object" && typeof (g as Record<string, unknown>)["name"] === "string") {
                parts.push(String((g as Record<string, unknown>)["name"]));
            }
        }
    }
    if (manifest["container"] === true) {
        parts.push("in a container");
    }
    return parts.join(" · ");
}

/** The node's readings now, each as a short phrase, leaving out what it did not send. */
export function readingParts(readings: NodeReadings | undefined): string[] {
    if (!readings) {
        return [];
    }
    const parts: string[] = [];
    const cpu = num(readings.cpu_pct);
    if (cpu !== null) {
        parts.push(`CPU ${cpu.toFixed(0)}%`);
    }
    const load = num(readings.load_1m);
    if (load !== null) {
        parts.push(`load ${load.toFixed(2)}`);
    }
    const free = num(readings.mem_free_mb);
    if (free !== null) {
        parts.push(`${megabytes(free)} free`);
    }
    const disk = num(readings.disk_free_mb);
    if (disk !== null) {
        parts.push(`${megabytes(disk)} disk`);
    }
    const temp = num(readings.temp_c);
    if (temp !== null) {
        parts.push(`${temp.toFixed(0)}°C`);
    }
    return parts;
}

/** The flags holding the node back, in words, or "" when none are. */
export function throttleText(readings: NodeReadings | undefined): string {
    const flags = readings?.throttled;
    if (!Array.isArray(flags) || flags.length === 0) {
        return "";
    }
    return flags.map(f => String(f).replace(/_/g, " ")).join(", ");
}

/** One labelled sparkline row: what it is, the line, and its latest value. */
function trendRow(label: string, points: Point[], format: (v: number) => string, color: string): HTMLElement {
    const row = el("div", "af-node-trend");
    const last = lastValue(points);
    row.append(
        el("span", "af-node-trend-label", label),
        buildSparkline(points, `${label} over the last hour`, color),
        el("span", "af-node-trend-value", last === null ? "—" : format(last)),
    );
    return row;
}

/** What the machine is: its line, its capacity and its devices, from the manifest. */
function machineParts(manifest: Record<string, unknown>): HTMLElement[] {
    const parts: HTMLElement[] = [];
    const machine = machineLine(manifest);
    if (machine) {
        parts.push(el("div", "af-node-machine", machine));
    }
    const capacity = capacityLine(manifest);
    if (capacity) {
        parts.push(el("div", "af-node-meta", capacity));
    }
    const devices = manifest["devices"];
    if (Array.isArray(devices) && devices.length > 0) {
        const chips = el("div", "af-node-chips");
        for (const d of devices) {
            chips.appendChild(el("span", "af-node-chip", String(d)));
        }
        parts.push(chips);
    }
    return parts;
}

/** How the node is doing now: its readings, and a warning when it is held back. */
function nowParts(readings: NodeReadings | undefined): HTMLElement[] {
    const parts: HTMLElement[] = [];
    const now = readingParts(readings);
    if (now.length > 0) {
        parts.push(el("div", "af-node-readings", now.join(" · ")));
    }
    const throttled = throttleText(readings);
    if (throttled) {
        const warning = el("div", "af-node-warning");
        warning.append(el("span", "af-node-warning-icon", "⚠"), el("span", "", `Throttled: ${throttled}`));
        parts.push(warning);
    }
    return parts;
}

/**
 * Build one remote node's card.
 *
 * ``onOpen`` is called with the node's name when its History button is pressed.
 */
export function buildNodeCard(data: NodeCardData, onOpen: (name: string) => void): HTMLElement {
    const card = el("div", "af-node-card");
    card.dataset["node"] = data.name;

    const head = el("div", "af-node-card-head");
    head.append(
        el("div", "af-node-name", data.name),
        el("span", `af-node-pill ${data.online ? "online" : "offline"}`, data.online ? "online" : "offline"),
    );
    card.appendChild(head);
    if (data.manifest) {
        card.append(...machineParts(data.manifest));
    }
    // Readings describe the machine as it is now, which an offline node cannot say.
    if (data.online) {
        card.append(...nowParts(data.readings));
    }
    if (data.trend) {
        card.append(
            trendRow("CPU", data.trend.cpu, v => `${v.toFixed(0)}%`, SERIES_COLORS[0]),
            trendRow("Free", data.trend.free, megabytes, SERIES_COLORS[2]),
        );
    }
    card.appendChild(
        el(
            "div",
            "af-node-meta af-node-agents",
            data.agents.length > 0 ? data.agents.join(", ") : "no agents",
        ),
    );

    const history = button("af-mini-btn af-node-history", "History");
    history.addEventListener("click", () => onOpen(data.name));
    card.appendChild(history);
    return card;
}
