/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { areaIconText, renderHADevices } from "../ui/dashboard/haDevices";
import type { HAClient } from "../io/HAClient";
import type { HAEntity, HARegistries } from "../types/ha";

function ent(entity_id: string, state: string, attributes: Record<string, unknown> = {}): HAEntity {
    return { entity_id, state, attributes, last_changed: "", last_updated: "" } as HAEntity;
}

function fakeClient() {
    return { toggleEntity: vi.fn(), callService: vi.fn() } as unknown as HAClient;
}

/** The single clickable (expandable) device row in a container. */
function clickableRow(container: HTMLElement): HTMLElement {
    const row = [...container.querySelectorAll<HTMLElement>("div")].find(d =>
        d.classList.contains("is-clickable"),
    );
    if (!row) {
        throw new Error("no clickable row found");
    }
    return row;
}

describe("areaIconText", () => {
    it("falls back to 🏠 for missing icon", () => {
        expect(areaIconText()).toBe("🏠");
        expect(areaIconText(null)).toBe("🏠");
    });

    it("converts an mdi hex codepoint to its character", () => {
        expect(areaIconText("mdi:0041")).toBe("A"); // 0x41
    });

    it("falls back on non-hex input", () => {
        expect(areaIconText("mdi:zzzz")).toBe("🏠");
    });

    it("falls back on out-of-range codepoints", () => {
        expect(areaIconText("mdi:200000")).toBe("🏠"); // > 0x10FFFF
    });
});

describe("renderHADevices", () => {
    let container: HTMLElement;
    let haClient: HAClient;

    beforeEach(() => {
        container = document.createElement("div");
        document.body.appendChild(container);
        haClient = fakeClient();
    });

    it("shows an empty message when there are no device-domain entities", () => {
        renderHADevices(container, [ent("sensor.temp", "21")], null, haClient);
        expect(container.textContent).toContain("No devices found.");
    });

    it("groups devices under their area header (from registries)", () => {
        const registries: HARegistries = {
            areas: [{ area_id: "a1", name: "Kitchen", icon: "mdi:0041" }],
            entityEntries: [{ entity_id: "light.k", device_id: "d1" }],
            deviceEntries: [{ id: "d1", area_id: "a1" }],
        };
        renderHADevices(container, [ent("light.k", "on", { friendly_name: "Lamp" })], registries, haClient);
        expect(container.textContent).toContain("Kitchen");
        expect(container.textContent).toContain("Lamp");
    });

    it("renders a row per device domain and skips capability domains", () => {
        renderHADevices(
            container,
            [
                ent("light.k", "on", { friendly_name: "Lamp" }),
                ent("switch.s", "off", { friendly_name: "Sw" }),
                ent("sensor.t", "21", { friendly_name: "Temp" }), // capability, not a device row
            ],
            null,
            haClient,
        );
        const rows = [...container.querySelectorAll<HTMLElement>("div")].filter(d =>
            d.classList.contains("is-clickable"),
        );
        // light has controls (clickable); switch has controls too → both clickable rows
        expect(rows.length).toBe(2);
    });

    it("toggles an entity via the row toggle without opening the detail", () => {
        renderHADevices(container, [ent("light.k", "on", { friendly_name: "Lamp" })], null, haClient);
        const row = clickableRow(container);
        row.querySelector("button")!.click(); // the toggle is the row's button
        expect(haClient.toggleEntity).toHaveBeenCalledWith("light.k");
    });

    it("opens the detail on row click and renders light controls", () => {
        const registries: HARegistries = {
            areas: [],
            entityEntries: [
                { entity_id: "light.k", device_id: "d1" },
                { entity_id: "sensor.k", device_id: "d1" },
            ],
            deviceEntries: [{ id: "d1", area_id: null }],
        };
        renderHADevices(
            container,
            [
                ent("light.k", "on", {
                    friendly_name: "Lamp",
                    supported_color_modes: ["rgb", "brightness"],
                    brightness: 128,
                    rgb_color: [255, 0, 0],
                }),
                ent("sensor.k", "21", { friendly_name: "Temp", unit_of_measurement: "°C" }),
            ],
            registries,
            haClient,
        );
        clickableRow(container).click();

        // Turn-off button (entity is active), brightness slider, colour picker.
        expect(container.querySelector('input[type="range"]')).not.toBeNull();
        expect(container.querySelector('input[type="color"]')).not.toBeNull();
        expect([...container.querySelectorAll("button")].some(b => b.textContent === "Turn Off")).toBe(true);
        // Capability badge from the linked sensor.
        expect(container.textContent).toContain("Temp: 21 °C");

        // Driving the brightness slider calls the light.turn_on service.
        const slider = container.querySelector<HTMLInputElement>('input[type="range"]')!;
        slider.value = "200";
        slider.dispatchEvent(new Event("change"));
        expect(haClient.callService).toHaveBeenCalledWith(
            "light",
            "turn_on",
            expect.objectContaining({ entity_id: "light.k", brightness: 200 }),
        );
    });

    it("renders climate target-temp control and calls set_temperature", () => {
        renderHADevices(
            container,
            [ent("climate.c", "heat", { friendly_name: "Heat", temperature: 21 })],
            null,
            haClient,
        );
        clickableRow(container).click();
        const slider = container.querySelector<HTMLInputElement>('input[type="range"]')!;
        slider.value = "24";
        slider.dispatchEvent(new Event("change"));
        expect(haClient.callService).toHaveBeenCalledWith(
            "climate",
            "set_temperature",
            expect.objectContaining({ entity_id: "climate.c", temperature: 24 }),
        );
    });

    it("renders cover buttons and calls the cover service", () => {
        renderHADevices(container, [ent("cover.g", "open", { friendly_name: "Garage" })], null, haClient);
        clickableRow(container).click();
        const openBtn = [...container.querySelectorAll("button")].find(b => b.textContent === "OPEN")!;
        openBtn.click();
        expect(haClient.callService).toHaveBeenCalledWith(
            "cover",
            "open_cover",
            expect.objectContaining({ entity_id: "cover.g" }),
        );
    });

    it("renders media controls (pause + volume) and calls the media service", () => {
        renderHADevices(
            container,
            [ent("media_player.tv", "playing", { friendly_name: "TV", volume_level: 0.5 })],
            null,
            haClient,
        );
        clickableRow(container).click();
        const pause = [...container.querySelectorAll("button")].find(b => b.textContent === "⏸")!;
        pause.click();
        expect(haClient.callService).toHaveBeenCalledWith(
            "media_player",
            "media_pause",
            expect.objectContaining({ entity_id: "media_player.tv" }),
        );
        const vol = container.querySelector<HTMLInputElement>('input[type="range"]')!;
        vol.value = "80";
        vol.dispatchEvent(new Event("change"));
        expect(haClient.callService).toHaveBeenCalledWith(
            "media_player",
            "volume_set",
            expect.objectContaining({ entity_id: "media_player.tv", volume_level: 0.8 }),
        );
    });

    it("the detail toggle button and colour picker call HA services", () => {
        renderHADevices(
            container,
            [
                ent("light.k", "on", {
                    friendly_name: "Lamp",
                    supported_color_modes: ["rgb"],
                    rgb_color: [255, 0, 0],
                }),
            ],
            null,
            haClient,
        );
        clickableRow(container).click();

        [...container.querySelectorAll<HTMLButtonElement>("button")]
            .find(b => b.textContent === "Turn Off")!
            .click();
        expect(haClient.toggleEntity).toHaveBeenCalledWith("light.k");

        const picker = container.querySelector<HTMLInputElement>('input[type="color"]')!;
        picker.value = "#00ff00";
        picker.dispatchEvent(new Event("change"));
        expect(haClient.callService).toHaveBeenCalledWith(
            "light",
            "turn_on",
            expect.objectContaining({ entity_id: "light.k", rgb_color: [0, 255, 0] }),
        );
    });

    it("sorts named areas alphabetically with the no-area bucket last", () => {
        const registries: HARegistries = {
            areas: [
                { area_id: "z", name: "Zen Den" },
                { area_id: "a", name: "Attic" },
            ],
            // entity-level area_id (not via a device) exercises the direct lookup
            entityEntries: [
                { entity_id: "light.z", area_id: "z" },
                { entity_id: "light.a", area_id: "a" },
            ],
            deviceEntries: [],
        };
        renderHADevices(
            container,
            [
                ent("light.z", "on", { friendly_name: "ZLight" }),
                ent("light.a", "on", { friendly_name: "ALight" }),
                ent("switch.n", "off", { friendly_name: "NoArea" }), // no registry entry → no-area bucket
            ],
            registries,
            haClient,
        );
        const text = container.textContent ?? "";
        expect(text.indexOf("Attic")).toBeLessThan(text.indexOf("Zen Den")); // alphabetical
        expect(text).toContain("NoArea"); // no-area bucket still rendered
    });

    it("device rows carry the styled row class (hover highlight is CSS-driven)", () => {
        renderHADevices(container, [ent("light.k", "on", { friendly_name: "Lamp" })], null, haClient);
        // Hover is now a `.af-ha-row:hover` rule rather than JS; guard the class
        // that carries it so removing it (and the highlight) is caught.
        expect(clickableRow(container).classList.contains("af-ha-row")).toBe(true);
    });
});
