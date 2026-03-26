/**
 * HAClient — Minimal Home Assistant WebSocket API client.
 *
 * Handles authentication and fetching the initial state of entities.
 * Can be extended for real-time state updates (subscriptions) and service calls.
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

export type HAUpdateHandler = (entities: HAEntity[]) => void;

export class HAClient {
  private ws: WebSocket | null = null;
  private idCounter = 1;
  private authenticated = false;
  private entities: HAEntity[] = [];
  private onUpdate: HAUpdateHandler | null = null;

  constructor(
    private readonly url: string,
    private readonly token: string,
  ) {}

  connect(onUpdate: HAUpdateHandler): void {
    this.onUpdate = onUpdate;
    // HA WS URL: ws://HOST:8123/api/websocket
    // Ensure we don't have double slashes if url ends with /
    const baseUrl = this.url.endsWith("/") ? this.url.slice(0, -1) : this.url;
    const wsUrl = baseUrl.replace(/^http/, "ws") + "/api/websocket";

    console.log("[HA] Connecting to", wsUrl);
    this.ws = new WebSocket(wsUrl);

    this.ws.onopen = () => {
      console.log("[HA] WebSocket opened");
    };

    this.ws.onmessage = (ev) => {
      let data: any;
      try {
        data = JSON.parse(ev.data);
      } catch (e) {
        console.error("[HA] Failed to parse message:", ev.data);
        return;
      }

      if (data.type === "auth_required") {
        console.log("[HA] Received auth_required, sending auth message...");
        this.ws?.send(
          JSON.stringify({
            type: "auth",
            access_token: this.token,
          }),
        );
      } else if (data.type === "auth_ok") {
        this.authenticated = true;
        console.info("[HA] Authenticated successfully");
        this.fetchEntities();
        this.subscribeEvents();
      } else if (data.type === "auth_invalid") {
        console.error("[HA] Authentication failed:", data.message);
      } else if (data.id && data.type === "result") {
        if (data.success && Array.isArray(data.result)) {
          this.entities = data.result;
          this.onUpdate?.(this.entities);
        }
      } else if (data.type === "event" && data.event?.data?.new_state) {
        const newState = data.event.data.new_state as HAEntity;
        const idx = this.entities.findIndex(
          (e) => e.entity_id === newState.entity_id,
        );
        if (idx !== -1) {
          this.entities[idx] = newState;
        } else {
          this.entities.push(newState);
        }
        this.onUpdate?.(this.entities);
      }
    };

    this.ws.onclose = () => {
      this.authenticated = false;
      console.warn("[HA] WebSocket closed");
    };

    this.ws.onerror = (err) => {
      console.error("[HA] WebSocket error:", err);
    };
  }

  disconnect(): void {
    this.ws?.close();
    this.ws = null;
    this.authenticated = false;
  }

  toggleEntity(entityId: string): void {
    if (!this.authenticated) return;
    const domain = entityId.split(".")[0];
    this.send({
      type: "call_service",
      domain,
      service: "toggle",
      service_data: { entity_id: entityId },
    });
  }

  callService(domain: string, service: string, serviceData: any): void {
    if (!this.authenticated) return;
    this.send({
      type: "call_service",
      domain,
      service,
      service_data: serviceData,
    });
  }

  private fetchEntities(): void {
    this.send({
      type: "get_states",
    });
  }

  private subscribeEvents(): void {
    this.send({
      type: "subscribe_events",
      event_type: "state_changed",
    });
  }

  private send(msg: any): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    if (msg.type !== "auth") {
      msg.id = this.idCounter++;
    }
    this.ws.send(JSON.stringify(msg));
  }
}
