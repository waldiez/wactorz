/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * HAClient — Minimal Home Assistant WebSocket API client.
 *
 * Handles authentication, entity state fetching, real-time state_changed
 * subscriptions, and optional registry fetching (areas, entity registry,
 * device registry) for room-based grouping in the UI.
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
        [key: string]: any;
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

export type HAUpdateHandler = (entities: HAEntity[]) => void;

export class HAClient {
    private ws: WebSocket | null = null;
    private idCounter = 1;
    private authenticated = false;
    private entities: HAEntity[] = [];
    private onUpdate: HAUpdateHandler | null = null;

    /** Called once after auth when area/entity/device registries are loaded. */
    onRegistriesUpdate: ((r: HARegistries) => void) | null = null;

    /** Tracks the id used for get_states so we can handle it synchronously. */
    private _entitiesReqId: number | null = null;

    /** Pending promise resolvers for registry (and other) tracked requests. */
    private _resolvers = new Map<number, (data: any) => void>();

    constructor(
        private readonly url: string,
        private readonly token: string,
    ) {}

    get connected(): boolean {
        return this.ws !== null && this.ws.readyState !== WebSocket.CLOSED;
    }

    connect(onUpdate: HAUpdateHandler): void {
        this.onUpdate = onUpdate;
        const baseUrl = this.url.endsWith("/") ? this.url.slice(0, -1) : this.url;
        const wsBase = baseUrl.replace(/^http(s?):/, "ws$1:");
        let wsUrl: string;
        try {
            const hasPath = new URL(baseUrl).pathname !== "/";
            wsUrl = hasPath ? wsBase : wsBase + "/api/websocket";
        } catch {
            wsUrl = wsBase + "/api/websocket";
        }

        console.log("[HA] Connecting to", wsUrl);
        this.ws = new WebSocket(wsUrl);

        this.ws.onopen = () => {
            console.log("[HA] WebSocket opened");
        };

        this.ws.onmessage = ev => {
            let data: any;
            try {
                data = JSON.parse(ev.data);
            } catch {
                console.error("[HA] Failed to parse message:", ev.data);
                return;
            }
            this._handleMessage(data);
        };

        this.ws.onclose = () => {
            this.authenticated = false;
            console.warn("[HA] WebSocket closed");
        };

        this.ws.onerror = err => {
            console.error("[HA] WebSocket error:", err);
        };
    }

    /** Route a parsed WebSocket frame to the matching handler. */
    private _handleMessage(data: any): void {
        if (data.type === "auth_required") {
            this.ws?.send(JSON.stringify({ type: "auth", access_token: this.token }));
        } else if (data.type === "auth_ok") {
            this.authenticated = true;
            console.info("[HA] Authenticated");
            this._fetchEntities();
            void this._fetchRegistries();
            this._subscribeEvents();
        } else if (data.type === "auth_invalid") {
            console.error("[HA] Authentication failed:", data.message);
        } else if (data.type === "result" && data.id != null) {
            this._handleResult(data);
        } else if (data.type === "event" && data.event?.data?.new_state) {
            this._handleStateEvent(data.event.data.new_state as HAEntity);
        }
    }

    /** Resolve a tracked request — entity snapshot callback and/or promise resolver. */
    private _handleResult(data: any): void {
        const id = data.id as number;

        // Entities (get_states) — handled synchronously to preserve callback contract
        if (id === this._entitiesReqId && data.success && Array.isArray(data.result)) {
            this._entitiesReqId = null;
            this.entities = data.result;
            this.onUpdate?.(this.entities);
        }

        // Registry / other tracked requests — Promise-based
        const resolver = this._resolvers.get(id);
        if (resolver) {
            this._resolvers.delete(id);
            resolver(data);
        }
    }

    /** Apply a state-changed event: update the entity cache and notify listeners. */
    private _handleStateEvent(newState: HAEntity): void {
        const idx = this.entities.findIndex(e => e.entity_id === newState.entity_id);
        if (idx !== -1) {
            this.entities[idx] = newState;
        } else {
            this.entities.push(newState);
        }
        this.onUpdate?.(this.entities);
        document.dispatchEvent(
            new CustomEvent("af-ha-state-change", {
                detail: {
                    entityId: newState.entity_id,
                    state: newState.state,
                    friendlyName: newState.attributes?.friendly_name ?? newState.entity_id,
                },
            }),
        );
    }

    disconnect(): void {
        this.ws?.close();
        this.ws = null;
        this.authenticated = false;
        this._resolvers.clear();
    }

    toggleEntity(entityId: string): void {
        if (!this.authenticated) {
            return;
        }
        const domain = entityId.split(".")[0];
        this._sendVoid({
            type: "call_service",
            domain,
            service: "toggle",
            service_data: { entity_id: entityId },
        });
    }

    callService(domain: string, service: string, serviceData: any): void {
        if (!this.authenticated) {
            return;
        }
        this._sendVoid({ type: "call_service", domain, service, service_data: serviceData });
    }

    /** Fire-and-forget: assigns an id and sends. Result response is ignored. */
    private _sendVoid(msg: any): void {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            return;
        }
        msg.id = this.idCounter++;
        this.ws.send(JSON.stringify(msg));
    }

    /** Tracked request: returns a Promise that resolves/rejects when the result arrives. */
    private _request<T = any>(msg: any): Promise<T> {
        return new Promise((resolve, reject) => {
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
                reject(new Error("WebSocket not open"));
                return;
            }
            const id = this.idCounter++;
            msg.id = id;
            this._resolvers.set(id, data => {
                if (data.success) {
                    resolve(data.result as T);
                } else {
                    reject(new Error(data.error?.message ?? "Request failed"));
                }
            });
            this.ws.send(JSON.stringify(msg));
        });
    }

    private _fetchEntities(): void {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            return;
        }
        const id = this.idCounter++;
        this._entitiesReqId = id;
        this.ws.send(JSON.stringify({ type: "get_states", id }));
    }

    private async _fetchRegistries(): Promise<void> {
        try {
            const [areas, entityEntries, deviceEntries] = await Promise.all([
                this._request<HAArea[]>({ type: "config/area_registry/list" }),
                this._request<HAEntityEntry[]>({ type: "config/entity_registry/list" }),
                this._request<HADeviceEntry[]>({ type: "config/device_registry/list" }),
            ]);
            this.onRegistriesUpdate?.({ areas, entityEntries, deviceEntries });
        } catch (e) {
            console.warn("[HA] Registry fetch failed (older HA or no permission):", e);
            // Fire with empty registries so the UI still renders without room grouping
            this.onRegistriesUpdate?.({ areas: [], entityEntries: [], deviceEntries: [] });
        }
    }

    private _subscribeEvents(): void {
        this._sendVoid({ type: "subscribe_events", event_type: "state_changed" });
    }
}
