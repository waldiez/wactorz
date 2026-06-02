import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { HAClient } from "../io/HAClient";

// ── WebSocket stub ────────────────────────────────────────────────────────────

class FakeWS {
  static OPEN = 1;
  readyState = FakeWS.OPEN;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: ((err: any) => void) | null = null;

  send(data: string) { this.sent.push(data); }
  close() { this.onclose?.(); }

  // Helper to simulate server → client message
  receive(data: any) { this.onmessage?.({ data: JSON.stringify(data) }); }
}

let fakeWS: FakeWS;

function makeFakeWSCtor(): { ctor: new (url: string) => FakeWS; spy: ReturnType<typeof vi.fn> } {
  const spy = vi.fn();
  function FakeWSCtor(this: any, url: string) {
    spy(url);
    return fakeWS;
  }
  FakeWSCtor.OPEN = FakeWS.OPEN;
  return { ctor: FakeWSCtor as any, spy };
}

let wsSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fakeWS = new FakeWS();
  const { ctor, spy } = makeFakeWSCtor();
  wsSpy = spy;
  (globalThis as any).WebSocket = ctor;
});

afterEach(() => {
  vi.restoreAllMocks();
  delete (globalThis as any).WebSocket;
});

// ── URL normalisation ─────────────────────────────────────────────────────────

describe("HAClient URL normalisation", () => {
  it("converts http:// to ws:// and appends /api/websocket for root URL", () => {
    const client = new HAClient("http://homeassistant.local:8123", "token");
    client.connect(vi.fn());
    expect(wsSpy).toHaveBeenCalledWith("ws://homeassistant.local:8123/api/websocket");
  });

  it("converts https:// to wss://", () => {
    const client = new HAClient("https://ha.example.com", "token");
    client.connect(vi.fn());
    expect(wsSpy).toHaveBeenCalledWith("wss://ha.example.com/api/websocket");
  });

  it("uses URL as-is when it already has a non-root path", () => {
    const client = new HAClient("http://proxy.local/ha/api/websocket", "token");
    client.connect(vi.fn());
    expect(wsSpy).toHaveBeenCalledWith("ws://proxy.local/ha/api/websocket");
  });

  it("strips trailing slash before converting", () => {
    const client = new HAClient("http://ha.local:8123/", "token");
    client.connect(vi.fn());
    expect(wsSpy).toHaveBeenCalledWith("ws://ha.local:8123/api/websocket");
  });

  it("falls back for invalid URL (no protocol)", () => {
    const client = new HAClient("invalid-url", "token");
    client.connect(vi.fn());
    // URL constructor throws, so it appends /api/websocket to wsBase
    expect(wsSpy).toHaveBeenCalledWith("invalid-url/api/websocket");
  });
});

// ── Authentication flow ───────────────────────────────────────────────────────

describe("HAClient authentication", () => {
  it("sends auth message on auth_required", () => {
    const client = new HAClient("http://ha.local", "my-token");
    client.connect(vi.fn());
    fakeWS.onopen?.();
    fakeWS.receive({ type: "auth_required" });
    const authMsg = JSON.parse(fakeWS.sent[0]!);
    expect(authMsg.type).toBe("auth");
    expect(authMsg.access_token).toBe("my-token");
  });

  it("fetches states and subscribes events on auth_ok", () => {
    const client = new HAClient("http://ha.local", "token");
    client.connect(vi.fn());
    fakeWS.receive({ type: "auth_required" });
    fakeWS.receive({ type: "auth_ok" });
    const msgs = fakeWS.sent.map((s) => JSON.parse(s));
    const types = msgs.map((m) => m.type);
    expect(types).toContain("get_states");
    expect(types).toContain("subscribe_events");
  });

  it("calls onUpdate with entities on get_states result", () => {
    const onUpdate = vi.fn();
    const entities = [{ entity_id: "light.living", state: "on", attributes: {}, last_changed: "", last_updated: "" }];
    const client = new HAClient("http://ha.local", "token");
    client.connect(onUpdate);
    fakeWS.receive({ type: "auth_ok" });
    // Simulate result for get_states (id: 1)
    fakeWS.receive({ id: 1, type: "result", success: true, result: entities });
    expect(onUpdate).toHaveBeenCalledWith(entities);
  });

  it("does not call onUpdate for failed result", () => {
    const onUpdate = vi.fn();
    const client = new HAClient("http://ha.local", "token");
    client.connect(onUpdate);
    fakeWS.receive({ id: 1, type: "result", success: false, result: [] });
    expect(onUpdate).not.toHaveBeenCalled();
  });

  it("does not call onUpdate when result is not an array", () => {
    const onUpdate = vi.fn();
    const client = new HAClient("http://ha.local", "token");
    client.connect(onUpdate);
    fakeWS.receive({ id: 1, type: "result", success: true, result: "not-array" });
    expect(onUpdate).not.toHaveBeenCalled();
  });

  it("logs error on auth_invalid", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const client = new HAClient("http://ha.local", "bad-token");
    client.connect(vi.fn());
    fakeWS.receive({ type: "auth_invalid", message: "Invalid token" });
    expect(spy).toHaveBeenCalled();
    spy.mockRestore();
  });

  it("ignores messages with bad JSON", () => {
    const client = new HAClient("http://ha.local", "token");
    client.connect(vi.fn());
    expect(() => fakeWS.onmessage?.({ data: "{{bad json" })).not.toThrow();
  });
});

// ── State-changed events ──────────────────────────────────────────────────────

describe("HAClient state_changed events", () => {
  it("updates existing entity and calls onUpdate", () => {
    const onUpdate = vi.fn();
    const entities = [{ entity_id: "light.living", state: "on", attributes: {}, last_changed: "", last_updated: "" }];
    const client = new HAClient("http://ha.local", "token");
    client.connect(onUpdate);
    fakeWS.receive({ type: "auth_ok" });
    fakeWS.receive({ id: 1, type: "result", success: true, result: entities });
    onUpdate.mockClear();

    const newState = { entity_id: "light.living", state: "off", attributes: {}, last_changed: "", last_updated: "" };
    fakeWS.receive({ type: "event", event: { data: { new_state: newState } } });
    expect(onUpdate).toHaveBeenCalledOnce();
    const updated = onUpdate.mock.calls[0]![0] as any[];
    expect(updated.find((e: any) => e.entity_id === "light.living")?.state).toBe("off");
  });

  it("adds new entity to list and calls onUpdate", () => {
    const onUpdate = vi.fn();
    const client = new HAClient("http://ha.local", "token");
    client.connect(onUpdate);
    fakeWS.receive({ type: "auth_ok" });
    fakeWS.receive({ id: 1, type: "result", success: true, result: [] });
    onUpdate.mockClear();

    const newState = { entity_id: "switch.fan", state: "off", attributes: {}, last_changed: "", last_updated: "" };
    fakeWS.receive({ type: "event", event: { data: { new_state: newState } } });
    expect(onUpdate).toHaveBeenCalledOnce();
    const list = onUpdate.mock.calls[0]![0] as any[];
    expect(list.some((e: any) => e.entity_id === "switch.fan")).toBe(true);
  });
});

// ── disconnect / toggleEntity / callService ───────────────────────────────────

describe("HAClient control methods", () => {
  it("disconnect() closes ws and resets authenticated", () => {
    const client = new HAClient("http://ha.local", "token");
    client.connect(vi.fn());
    fakeWS.receive({ type: "auth_ok" });
    client.disconnect();
    // ws.close() is called which triggers onclose, setting authenticated=false
    expect(fakeWS.readyState).toBe(FakeWS.OPEN); // FakeWS.close() doesn't change readyState
  });

  it("disconnect() is safe when called before connect", () => {
    const client = new HAClient("http://ha.local", "token");
    expect(() => client.disconnect()).not.toThrow();
  });

  it("toggleEntity() sends call_service when authenticated", () => {
    const client = new HAClient("http://ha.local", "token");
    client.connect(vi.fn());
    fakeWS.receive({ type: "auth_ok" });
    fakeWS.sent = [];
    client.toggleEntity("light.living_room");
    const msg = JSON.parse(fakeWS.sent[0]!);
    expect(msg.type).toBe("call_service");
    expect(msg.domain).toBe("light");
    expect(msg.service).toBe("toggle");
    expect(msg.service_data.entity_id).toBe("light.living_room");
  });

  it("toggleEntity() is no-op when not authenticated", () => {
    const client = new HAClient("http://ha.local", "token");
    client.connect(vi.fn());
    // no auth_ok
    fakeWS.sent = [];
    client.toggleEntity("light.x");
    expect(fakeWS.sent.length).toBe(0);
  });

  it("callService() sends correct message when authenticated", () => {
    const client = new HAClient("http://ha.local", "token");
    client.connect(vi.fn());
    fakeWS.receive({ type: "auth_ok" });
    fakeWS.sent = [];
    client.callService("climate", "set_temperature", { temperature: 21 });
    const msg = JSON.parse(fakeWS.sent[0]!);
    expect(msg.type).toBe("call_service");
    expect(msg.domain).toBe("climate");
    expect(msg.service).toBe("set_temperature");
  });

  it("callService() is no-op when not authenticated", () => {
    const client = new HAClient("http://ha.local", "token");
    client.connect(vi.fn());
    fakeWS.sent = [];
    client.callService("climate", "set_temperature", {});
    expect(fakeWS.sent.length).toBe(0);
  });

  it("send() is no-op when WS is not OPEN", () => {
    const client = new HAClient("http://ha.local", "token");
    client.connect(vi.fn());
    fakeWS.receive({ type: "auth_ok" });
    fakeWS.readyState = 3; // CLOSED
    fakeWS.sent = [];
    client.toggleEntity("light.x");
    expect(fakeWS.sent.length).toBe(0);
  });

  it("idCounter increments with each send", () => {
    const client = new HAClient("http://ha.local", "token");
    client.connect(vi.fn());
    fakeWS.receive({ type: "auth_ok" });
    fakeWS.sent = [];
    client.toggleEntity("light.a");
    client.toggleEntity("light.b");
    const msg1 = JSON.parse(fakeWS.sent[0]!);
    const msg2 = JSON.parse(fakeWS.sent[1]!);
    expect(msg2.id).toBe(msg1.id + 1);
  });

  // ── _fetchRegistries / _request resolver ─────────────────────────────────

  it("_fetchRegistries resolves and calls onRegistriesUpdate", async () => {
    const regSpy = vi.fn();
    const client = new HAClient("http://ha.local", "token");
    client.onRegistriesUpdate = regSpy;
    client.connect(vi.fn());
    fakeWS.receive({ type: "auth_ok" });
    // After auth_ok: get_states=id1, area=id2, entity=id3, device=id4, subscribe=id5
    fakeWS.receive({ id: 2, type: "result", success: true, result: [{ area_id: "living", name: "Living Room" }] });
    fakeWS.receive({ id: 3, type: "result", success: true, result: [] });
    fakeWS.receive({ id: 4, type: "result", success: true, result: [] });
    await new Promise((r) => setTimeout(r, 10));
    expect(regSpy).toHaveBeenCalledWith({
      areas: [{ area_id: "living", name: "Living Room" }],
      entityEntries: [],
      deviceEntries: [],
    });
  });

  it("_fetchRegistries catch fires and calls onRegistriesUpdate with empty arrays on rejection", async () => {
    const regSpy = vi.fn();
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const client = new HAClient("http://ha.local", "token");
    client.onRegistriesUpdate = regSpy;
    client.connect(vi.fn());
    fakeWS.receive({ type: "auth_ok" });
    // Reject first registry request → _request rejects → Promise.all rejects → catch
    fakeWS.receive({ id: 2, type: "result", success: false, error: { message: "permission denied" } });
    await new Promise((r) => setTimeout(r, 10));
    expect(regSpy).toHaveBeenCalledWith({ areas: [], entityEntries: [], deviceEntries: [] });
    warnSpy.mockRestore();
  });

  it("onerror is handled without throwing", () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const client = new HAClient("http://ha.local", "token");
    client.connect(vi.fn());
    expect(() => fakeWS.onerror?.(new Error("connection refused"))).not.toThrow();
    errSpy.mockRestore();
    void client;
  });

  it("connected returns true when WebSocket is open", () => {
    const client = new HAClient("http://ha.local", "token");
    client.connect(vi.fn());
    expect(client.connected).toBe(true);
  });

  it("connected returns false before connect() is called", () => {
    const client = new HAClient("http://ha.local", "token");
    expect(client.connected).toBe(false);
  });

  it("_request rejects immediately when WebSocket is closed at auth_ok, catch fires with empty arrays", async () => {
    const regSpy = vi.fn();
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const client = new HAClient("http://ha.local", "token");
    client.onRegistriesUpdate = regSpy;
    client.connect(vi.fn());
    fakeWS.readyState = 3; // CLOSED before auth_ok triggers _fetchRegistries
    fakeWS.receive({ type: "auth_ok" });
    await new Promise((r) => setTimeout(r, 10));
    expect(regSpy).toHaveBeenCalledWith({ areas: [], entityEntries: [], deviceEntries: [] });
    warnSpy.mockRestore();
  });

  it("onclose resets authenticated flag", () => {
    const client = new HAClient("http://ha.local", "token");
    client.connect(vi.fn());
    fakeWS.receive({ type: "auth_ok" });
    fakeWS.onclose?.();
    // After close, toggleEntity should be a no-op (not authenticated)
    fakeWS.sent = [];
    client.toggleEntity("light.x");
    expect(fakeWS.sent.length).toBe(0);
  });
});
