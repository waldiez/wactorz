/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi } from "vitest";
import { readingsFrom } from "../agents/nodeReadings";
import {
    buildNodeCard,
    capacityLine,
    machineLine,
    megabytes,
    readingParts,
    throttleText,
    type NodeCardData,
} from "../ui/dashboard/nodeCard";

const PI = {
    manifest_v: 1,
    model: "Raspberry Pi 5 Model B Rev 1.0",
    arch: "aarch64",
    os_release: "Debian GNU/Linux 13 (trixie)",
    python: "3.13.5",
    cpu_count: 4,
    ram_total_mb: 8058,
    container: false,
    gpu: [{ kind: "hailo", name: "Hailo" }],
    devices: ["bluetooth", "camera", "gpio"],
};

function data(over: Partial<NodeCardData> = {}): NodeCardData {
    return { name: "rpi", online: true, agents: ["flic"], lastSeen: Date.now(), ...over };
}

describe("readingsFrom", () => {
    it("keeps finite numbers and a list of flags, and nothing else", () => {
        expect(
            readingsFrom({
                cpu_pct: 3,
                load_1m: Infinity,
                temp_c: "hot",
                throttled: ["throttled", 1],
                other: 2,
            }),
        ).toEqual({ cpu_pct: 3, throttled: ["throttled"] });
        expect(readingsFrom({ throttled: null })).toEqual({});
    });
});

describe("the lines a node card says", () => {
    it("says what the machine is", () => {
        expect(machineLine(PI)).toBe(
            "Raspberry Pi 5 Model B Rev 1.0 · aarch64 · Debian GNU/Linux 13 (trixie) · Python 3.13.5",
        );
        expect(machineLine({ arch: "x86_64", model: "" })).toBe("x86_64");
    });

    it("says how much machine there is", () => {
        expect(capacityLine(PI)).toBe("4 CPUs · 7.9 GB memory · Hailo");
        expect(capacityLine({ cpu_count: 1, ram_total_mb: 512, container: true })).toBe(
            "1 CPU · 512 MB memory · in a container",
        );
        expect(capacityLine({ cpu_count: true, gpu: [null, { kind: "x" }] })).toBe("");
    });

    it("says only the readings the node sent", () => {
        expect(
            readingParts({ cpu_pct: 12.4, load_1m: 0.5, mem_free_mb: 700, disk_free_mb: 3277, temp_c: 61 }),
        ).toEqual(["CPU 12%", "load 0.50", "700 MB free", "3.2 GB disk", "61°C"]);
        expect(readingParts({ cpu_pct: 3 })).toEqual(["CPU 3%"]);
        expect(readingParts(undefined)).toEqual([]);
    });

    it("says what holds the node back, in words", () => {
        expect(throttleText({ throttled: ["under_voltage", "freq_capped"] })).toBe(
            "under voltage, freq capped",
        );
        expect(throttleText({ throttled: [] })).toBe("");
        expect(throttleText(undefined)).toBe("");
    });

    it("sizes in the unit a person reads", () => {
        expect(megabytes(512)).toBe("512 MB");
        expect(megabytes(2048)).toBe("2.0 GB");
    });
});

describe("buildNodeCard", () => {
    it("shows the machine, its devices, the readings, the trend and the agents", () => {
        const card = buildNodeCard(
            data({
                manifest: PI,
                readings: { cpu_pct: 12, mem_free_mb: 7000 },
                trend: {
                    cpu: [
                        { t: 0, v: 10 },
                        { t: 60_000, v: 12 },
                    ],
                    free: [{ t: 0, v: 7000 }],
                },
            }),
            vi.fn(),
        );

        expect(card.dataset["node"]).toBe("rpi");
        expect(card.querySelector(".af-node-machine")?.textContent).toContain("Raspberry Pi 5");
        expect([...card.querySelectorAll(".af-node-chip")].map(c => c.textContent)).toEqual([
            "bluetooth",
            "camera",
            "gpio",
        ]);
        expect(card.querySelector(".af-node-readings")?.textContent).toBe("CPU 12% · 6.8 GB free");
        expect([...card.querySelectorAll(".af-node-trend-value")].map(v => v.textContent)).toEqual([
            "12%",
            "6.8 GB",
        ]);
        expect(card.querySelector(".af-node-agents")?.textContent).toBe("flic");
        expect(card.querySelector(".af-node-pill")?.textContent).toBe("online");
    });

    it("warns with an icon and words when the node is throttled", () => {
        const card = buildNodeCard(data({ readings: { throttled: ["under_voltage"] } }), vi.fn());
        const warning = card.querySelector(".af-node-warning");
        expect(warning?.textContent).toBe("⚠Throttled: under voltage");
    });

    it("shows no readings for an offline node: they would describe a machine as it was", () => {
        const card = buildNodeCard(
            data({ online: false, readings: { cpu_pct: 50, throttled: ["throttled"] } }),
            vi.fn(),
        );
        expect(card.querySelector(".af-node-readings")).toBeNull();
        expect(card.querySelector(".af-node-warning")).toBeNull();
        expect(card.querySelector(".af-node-pill")?.textContent).toBe("offline");
    });

    it("shows a node that has said nothing about itself plainly", () => {
        const card = buildNodeCard(data({ agents: [], manifest: null }), vi.fn());
        expect(card.querySelector(".af-node-machine")).toBeNull();
        expect(card.querySelector(".af-node-trend")).toBeNull();
        expect(card.querySelector(".af-node-agents")?.textContent).toBe("no agents");
    });

    it("shows a dash for a trend with no value yet", () => {
        const card = buildNodeCard(data({ trend: { cpu: [{ t: 0, v: null }], free: [] } }), vi.fn());
        expect([...card.querySelectorAll(".af-node-trend-value")].map(v => v.textContent)).toEqual([
            "—",
            "—",
        ]);
    });

    it("opens the history from the card and from its button, once each", () => {
        const onOpen = vi.fn();
        const card = buildNodeCard(data(), onOpen);
        card.click();
        card.querySelector<HTMLButtonElement>(".af-node-history")!.click();
        expect(onOpen.mock.calls).toEqual([["rpi"], ["rpi"]]);
    });

    it("sets a hostile name and manifest as text", () => {
        const evil = "<img src=x onerror=alert(1)>";
        const card = buildNodeCard(
            data({ name: evil, agents: [evil], manifest: { model: evil, devices: [evil] } }),
            vi.fn(),
        );
        expect(card.querySelector("img")).toBeNull();
        expect(card.querySelector(".af-node-name")?.textContent).toBe(evil);
    });
});
