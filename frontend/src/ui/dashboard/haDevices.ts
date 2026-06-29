/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Home Assistant device list for the dashboard's HA view.
 *
 * Groups entities by area, renders a row per device with a quick toggle and a
 * lazily-built detail panel (controls + capability badges). All HA mutations
 * go through the passed-in HAClient, so this module owns no state.
 */
import type { HAClient } from "../../io/HAClient";
import type { HAEntity, HAArea, HARegistries } from "../../types/ha";

/** Domains rendered as controllable device rows. */
const DEVICE_DOMAINS = new Set([
    "light",
    "switch",
    "fan",
    "climate",
    "cover",
    "lock",
    "media_player",
    "vacuum",
    "camera",
    "alarm_control_panel",
    "remote",
    "humidifier",
    "siren",
    "water_heater",
    "valve",
    "lawn_mower",
    "button",
    "number",
    "select",
]);
/** Domains shown as read-only capability badges grouped under their device. */
const CAP_DOMAINS = new Set(["sensor", "binary_sensor"]);
/** Domains that get an inline on/off toggle in the row. */
const TOGGLEABLE = new Set(["light", "switch", "fan", "humidifier", "vacuum", "input_boolean"]);
const ACTIVE_STATES = ["on", "playing", "cool", "heat", "open", "active", "detected", "home", "locked"];
const ALERT_STATES = ["problem", "error", "critical", "warning", "emergency"];
const CONTROL_DOMAINS = [
    "light",
    "switch",
    "fan",
    "humidifier",
    "vacuum",
    "input_boolean",
    "climate",
    "cover",
    "media_player",
];

const DOMAIN_ICONS: Record<string, string> = {
    light: "💡",
    switch: "🔌",
    sensor: "🌡",
    binary_sensor: "🔔",
    media_player: "📺",
    climate: "❄",
    camera: "📷",
    fan: "🌀",
    vacuum: "🧹",
    cover: "🚪",
    lock: "🔒",
    drone: "🚁",
    person: "👤",
    device_tracker: "📍",
    sun: "☀️",
};

function domainOf(e: HAEntity): string {
    return e.entity_id.split(".")[0] || "";
}

/** Best-effort emoji for an HA area icon (MDI codepoint), falling back to 🏠. */
export function areaIconText(icon?: string | null): string {
    const fallback = "🏠";
    if (!icon) {
        return fallback;
    }
    const cp = parseInt(icon.replace(/^mdi:/, ""), 16);
    if (!Number.isFinite(cp) || cp < 0 || cp > 0x10ffff) {
        return fallback;
    }
    try {
        return String.fromCodePoint(cp);
    } catch {
        return fallback;
    }
}

function domainIcon(domain: string): string {
    return DOMAIN_ICONS[domain] || "📦";
}

function entityHasControls(domain: string): boolean {
    return CONTROL_DOMAINS.includes(domain);
}

interface RegistryIndex {
    areaById: Map<string, HAArea>;
    areaIdOf: (entityId: string) => string;
    capsFor: (entityId: string) => HAEntity[];
}

/** Build the area/device lookups and the per-device capability groups. */
function buildRegistryIndex(registries: HARegistries | null, entities: HAEntity[]): RegistryIndex {
    const areaById = new Map<string, HAArea>();
    const entityDeviceId = new Map<string, string>();
    const entityAreaId = new Map<string, string>();
    const deviceAreaId = new Map<string, string>();

    if (registries) {
        registries.areas.forEach(a => areaById.set(a.area_id, a));
        registries.deviceEntries.forEach(d => {
            if (d.area_id) {
                deviceAreaId.set(d.id, d.area_id);
            }
        });
        registries.entityEntries.forEach(entry => {
            if (entry.device_id) {
                entityDeviceId.set(entry.entity_id, entry.device_id);
            }
            if (entry.area_id) {
                entityAreaId.set(entry.entity_id, entry.area_id);
            }
        });
    }

    const areaIdOf = (entityId: string): string => {
        if (entityAreaId.has(entityId)) {
            return entityAreaId.get(entityId)!;
        }
        const devId = entityDeviceId.get(entityId);
        if (devId && deviceAreaId.has(devId)) {
            return deviceAreaId.get(devId)!;
        }
        return "";
    };

    const capsByDevice = new Map<string, HAEntity[]>();
    entities
        .filter(e => CAP_DOMAINS.has(domainOf(e)))
        .forEach(e => {
            const devId = entityDeviceId.get(e.entity_id);
            if (!devId) {
                return;
            }
            if (!capsByDevice.has(devId)) {
                capsByDevice.set(devId, []);
            }
            capsByDevice.get(devId)!.push(e);
        });

    const capsFor = (entityId: string): HAEntity[] => {
        const devId = entityDeviceId.get(entityId);
        return devId ? (capsByDevice.get(devId) ?? []) : [];
    };

    return { areaById, areaIdOf, capsFor };
}

function groupByArea(deviceEntities: HAEntity[], idx: RegistryIndex): Map<string, HAEntity[]> {
    const byArea = new Map<string, HAEntity[]>();
    deviceEntities.forEach(e => {
        const aId = idx.areaIdOf(e.entity_id);
        if (!byArea.has(aId)) {
            byArea.set(aId, []);
        }
        byArea.get(aId)!.push(e);
    });
    return byArea;
}

/** Named areas first (sorted by name), the no-area bucket ("") last. */
function sortAreaIds(byArea: Map<string, HAEntity[]>, areaById: Map<string, HAArea>): string[] {
    return [...byArea.keys()].sort((a, b) => {
        if (!a && !b) {
            return 0;
        }
        if (!a) {
            return 1;
        }
        if (!b) {
            return -1;
        }
        return (areaById.get(a)?.name ?? a).localeCompare(areaById.get(b)?.name ?? b);
    });
}

function addSlider(
    container: HTMLElement,
    labelText: string,
    min: number,
    max: number,
    current: number,
    onChange: (val: number) => void,
    format?: (v: number) => string,
): void {
    const wrap = document.createElement("div");
    wrap.className = "af-ha-slider";

    const lbl = document.createElement("div");
    lbl.className = "af-ha-ctrl-label";
    lbl.textContent = `${labelText}: ${format ? format(current) : current}`;

    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = String(min);
    slider.max = String(max);
    slider.value = String(current);
    slider.className = "af-ha-slider-input";
    slider.addEventListener("change", () => {
        const val = parseInt(slider.value, 10);
        if (format) {
            lbl.textContent = `${labelText}: ${format(val)}`;
        }
        onChange(val);
    });

    wrap.append(lbl, slider);
    container.appendChild(wrap);
}

const hex2 = (n: number): string => n.toString(16).padStart(2, "0");

function addColorPicker(container: HTMLElement, e: HAEntity, haClient: HAClient | null): void {
    const row = document.createElement("div");
    row.className = "af-ha-color-row";
    const lbl = document.createElement("div");
    lbl.className = "af-ha-ctrl-label";
    lbl.textContent = "Color:";

    const picker = document.createElement("input");
    picker.type = "color";
    picker.className = "af-ha-color-picker";

    if (e.attributes.rgb_color) {
        const [r = 0, g = 0, b = 0] = e.attributes.rgb_color;
        picker.value = `#${hex2(r)}${hex2(g)}${hex2(b)}`;
    }

    picker.addEventListener("change", () => {
        const hex = picker.value;
        const rgb = [hex.slice(1, 3), hex.slice(3, 5), hex.slice(5, 7)].map(h => parseInt(h, 16));
        haClient?.callService("light", "turn_on", { entity_id: e.entity_id, rgb_color: rgb });
    });
    row.append(lbl, picker);
    container.appendChild(row);
}

function addButtonRow(
    container: HTMLElement,
    specs: { label: string; onClick: () => void; flex?: string }[],
): void {
    const row = document.createElement("div");
    row.className = "af-ha-btn-row";
    specs.forEach(({ label, onClick, flex }) => {
        const btn = document.createElement("button");
        btn.className = "af-mini-btn";
        btn.textContent = label;
        if (flex) {
            btn.style.flex = flex;
        }
        btn.addEventListener("click", onClick);
        row.appendChild(btn);
    });
    container.appendChild(row);
}

/** Append the per-domain detail controls (toggle/sliders/buttons) for an entity. */
function appendEntityControls(
    container: HTMLElement,
    e: HAEntity,
    isActive: boolean,
    haClient: HAClient | null,
): void {
    const domain = domainOf(e);

    if (["light", "switch", "fan", "input_boolean", "humidifier", "vacuum"].includes(domain)) {
        const btn = document.createElement("button");
        btn.className = "af-mini-btn af-ha-ctrl-full";
        btn.textContent = isActive ? "Turn Off" : "Turn On";
        btn.addEventListener("click", () => haClient?.toggleEntity(e.entity_id));
        container.appendChild(btn);
    }

    if (domain === "light" && e.attributes.supported_color_modes?.some((m: string) => m !== "onoff")) {
        addSlider(
            container,
            "Brightness",
            0,
            255,
            e.attributes.brightness || 0,
            val => haClient?.callService("light", "turn_on", { entity_id: e.entity_id, brightness: val }),
            v => Math.round((v / 255) * 100) + "%",
        );
    }

    if (domain === "light" && e.attributes.supported_color_modes?.includes("rgb")) {
        addColorPicker(container, e, haClient);
    }

    if (domain === "climate") {
        const target = e.attributes.temperature || e.attributes.target_temp_low || 20;
        addSlider(
            container,
            "Target Temp",
            15,
            30,
            target,
            val =>
                haClient?.callService("climate", "set_temperature", {
                    entity_id: e.entity_id,
                    temperature: val,
                }),
            v => v + "°",
        );
    }

    if (domain === "cover") {
        addButtonRow(
            container,
            ["open_cover", "stop_cover", "close_cover"].map(svc => ({
                label: (svc.split("_")[0] || "ACTION").toUpperCase(),
                flex: "1",
                onClick: () => haClient?.callService("cover", svc, { entity_id: e.entity_id }),
            })),
        );
    }

    if (domain === "media_player") {
        appendMediaControls(container, e, haClient);
    }
}

function appendMediaControls(container: HTMLElement, e: HAEntity, haClient: HAClient | null): void {
    addButtonRow(container, [
        {
            label: e.state === "playing" ? "⏸" : "▶",
            flex: "1",
            onClick: () =>
                haClient?.callService("media_player", e.state === "playing" ? "media_pause" : "media_play", {
                    entity_id: e.entity_id,
                }),
        },
    ]);

    if (e.attributes.volume_level != null) {
        addSlider(
            container,
            "Volume",
            0,
            100,
            Math.round(e.attributes.volume_level * 100),
            val =>
                haClient?.callService("media_player", "volume_set", {
                    entity_id: e.entity_id,
                    volume_level: val / 100,
                }),
            v => v + "%",
        );
    }
}

function buildToggle(e: HAEntity, isActive: boolean, haClient: HAClient | null): HTMLButtonElement {
    const toggle = document.createElement("button");
    toggle.title = isActive ? "Turn off" : "Turn on";
    toggle.className = isActive ? "af-ha-toggle is-on" : "af-ha-toggle";
    const thumb = document.createElement("div");
    thumb.className = isActive ? "af-ha-toggle-thumb is-on" : "af-ha-toggle-thumb";
    toggle.appendChild(thumb);
    toggle.addEventListener("click", ev => {
        ev.stopPropagation();
        haClient?.toggleEntity(e.entity_id);
    });
    return toggle;
}

function buildCapabilityBadges(capabilities: HAEntity[]): HTMLElement {
    const capWrap = document.createElement("div");
    capWrap.className = "af-ha-caps";
    capabilities.forEach(cap => {
        const badge = document.createElement("span");
        const capName = cap.attributes.friendly_name || cap.entity_id.split(".").pop() || cap.entity_id;
        const capState =
            cap.state + (cap.attributes.unit_of_measurement ? " " + cap.attributes.unit_of_measurement : "");
        badge.className = "af-ha-cap-badge";
        badge.textContent = `${capName}: ${capState}`;
        capWrap.appendChild(badge);
    });
    return capWrap;
}

/** Render (once) the expanded detail panel: controls + capability badges. */
function renderDetail(
    detail: HTMLElement,
    e: HAEntity,
    isActive: boolean,
    hasControls: boolean,
    capabilities: HAEntity[],
    haClient: HAClient | null,
): void {
    if (hasControls) {
        const ctrlDiv = document.createElement("div");
        ctrlDiv.className = "af-ha-controls";
        appendEntityControls(ctrlDiv, e, isActive, haClient);
        if (ctrlDiv.children.length > 0) {
            detail.appendChild(ctrlDiv);
        }
    }
    if (capabilities.length > 0) {
        detail.appendChild(buildCapabilityBadges(capabilities));
    }
}

function buildRowEl(
    e: HAEntity,
    isActive: boolean,
    isAlert: boolean,
    haClient: HAClient | null,
): HTMLElement {
    const domain = domainOf(e);
    const stateColor = isAlert ? "#f87171" : isActive ? "#34d399" : "rgba(255,255,255,0.35)";

    const row = document.createElement("div");
    row.className = "af-ha-row";

    const iconEl = document.createElement("span");
    iconEl.textContent = domainIcon(domain);
    iconEl.className = "af-ha-row-icon";

    const nameEl = document.createElement("div");
    nameEl.className = "af-ha-row-name";
    nameEl.textContent = e.attributes.friendly_name || e.entity_id;

    const stateEl = document.createElement("span");
    stateEl.className = "af-ha-row-state";
    stateEl.style.color = stateColor;
    stateEl.textContent =
        e.state + (e.attributes.unit_of_measurement ? " " + e.attributes.unit_of_measurement : "");

    row.append(iconEl, nameEl, stateEl);

    if (TOGGLEABLE.has(domain)) {
        row.appendChild(buildToggle(e, isActive, haClient));
    }
    return row;
}

function buildHADeviceRow(e: HAEntity, capabilities: HAEntity[], haClient: HAClient | null): HTMLElement {
    const domain = domainOf(e);
    const isActive = ACTIVE_STATES.includes(e.state);
    const isAlert = ALERT_STATES.includes(e.state);

    const wrapper = document.createElement("div");
    const row = buildRowEl(e, isActive, isAlert, haClient);

    const hasControls = entityHasControls(domain);
    const hasDetail = hasControls || capabilities.length > 0;

    if (hasDetail) {
        const chevron = document.createElement("span");
        chevron.textContent = "›";
        chevron.className = "af-ha-chevron";
        row.appendChild(chevron);
        row.classList.add("is-clickable");

        const detail = document.createElement("div");
        detail.className = "af-ha-detail";
        let detailRendered = false;

        row.addEventListener("click", () => {
            const open = detail.classList.contains("is-open");
            detail.classList.toggle("is-open", !open);
            chevron.classList.toggle("is-open", !open);
            if (!open && !detailRendered) {
                detailRendered = true;
                renderDetail(detail, e, isActive, hasControls, capabilities, haClient);
            }
        });

        wrapper.appendChild(detail);
    }

    wrapper.insertBefore(row, wrapper.firstChild);
    return wrapper;
}

function buildAreaSection(
    areaId: string,
    entities: HAEntity[],
    idx: RegistryIndex,
    haClient: HAClient | null,
): HTMLElement {
    const area = idx.areaById.get(areaId);
    const sectionEntities = [...entities].sort((a, b) =>
        (a.attributes.friendly_name ?? a.entity_id).localeCompare(b.attributes.friendly_name ?? b.entity_id),
    );

    const section = document.createElement("div");
    section.className = "af-ha-section";

    const header = document.createElement("div");
    header.className = "af-ha-section-head";
    // Area name comes from the HA area registry (untrusted) — build with
    // textContent, never innerHTML, so a hostile name can't inject markup.
    const iconSpan = document.createElement("span");
    iconSpan.textContent = area ? areaIconText(area.icon) : "📦";
    const nameSpan = document.createElement("span");
    nameSpan.textContent = area?.name ?? "Other";
    const countSpan = document.createElement("span");
    countSpan.className = "af-ha-section-count";
    countSpan.textContent = String(sectionEntities.length);
    header.append(iconSpan, nameSpan, countSpan);
    section.appendChild(header);

    sectionEntities.forEach(e => {
        section.appendChild(buildHADeviceRow(e, idx.capsFor(e.entity_id), haClient));
    });
    return section;
}

/** Render the grouped HA device list into `container`. */
export function renderHADevices(
    container: HTMLElement,
    entities: HAEntity[],
    registries: HARegistries | null,
    haClient: HAClient | null,
): void {
    container.innerHTML = "";
    container.classList.add("af-ha-device-list");

    const idx = buildRegistryIndex(registries, entities);
    const deviceEntities = entities.filter(e => DEVICE_DOMAINS.has(domainOf(e)));

    if (deviceEntities.length === 0) {
        container.innerHTML = `<div class="af-ha-empty-msg">No devices found.</div>`;
        return;
    }

    const byArea = groupByArea(deviceEntities, idx);
    sortAreaIds(byArea, idx.areaById).forEach(areaId => {
        container.appendChild(buildAreaSection(areaId, byArea.get(areaId) ?? [], idx, haClient));
    });
}
