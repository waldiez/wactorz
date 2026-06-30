/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Home Assistant domain models — the entity/area/device/registry shapes returned
 * by HA's WebSocket API. `io/HAClient` fetches and emits them; the dashboard
 * (`ui/CardDashboard`, `ui/dashboard/haDevices`) renders them.
 */

export interface HAEntity {
    entity_id: string;
    state: string;
    attributes: {
        friendly_name?: string;
        icon?: string;
        unit_of_measurement?: string;
        device_class?: string;
        supported_features?: number;
        entity_picture?: string;
        // Domain-specific attributes read by the device cards (haDevices.ts).
        brightness?: number;
        rgb_color?: number[];
        supported_color_modes?: string[];
        temperature?: number;
        target_temp_low?: number;
        volume_level?: number;
        [key: string]: unknown;
    };
    last_changed: string;
    last_updated: string;
}

export interface HAArea {
    area_id: string;
    name: string;
    icon?: string | null;
    floor_id?: string | null;
}

export interface HAEntityEntry {
    entity_id: string;
    device_id?: string | null;
    area_id?: string | null;
    disabled_by?: string | null;
    hidden_by?: string | null;
}

export interface HADeviceEntry {
    id: string;
    area_id?: string | null;
    name?: string | null;
    name_by_user?: string | null;
    manufacturer?: string | null;
    model?: string | null;
}

export interface HARegistries {
    areas: HAArea[];
    entityEntries: HAEntityEntry[];
    deviceEntries: HADeviceEntry[];
}
