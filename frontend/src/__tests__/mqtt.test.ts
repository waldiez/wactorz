import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  toMs,
  nameFromId,
  resolveAgentName,
  normaliseHeartbeat,
  normaliseChat,
  normaliseStatus,
  MQTTClient,
} from "../mqtt/MQTTClient";

// ── Mock mqtt package ─────────────────────────────────────────────────────────

type Callback = (...args: unknown[]) => void;
const mockHandlers: Record<string, Callback> = {};
let mockConnected = true;

const mockMqttClient = {
  get connected() { return mockConnected; },
  on: vi.fn((event: string, cb: Callback) => { mockHandlers[event] = cb; }),
  subscribe: vi.fn(),
  publish: vi.fn(),
  end: vi.fn(),
};

vi.mock("mqtt", () => ({
  default: { connect: vi.fn(() => mockMqttClient) },
}));

function triggerMessage(topic: string, payload: unknown) {
  const raw = Buffer.from(JSON.stringify(payload));
  mockHandlers["message"]?.(topic, raw);
}

// ── toMs ─────────────────────────────────────────────────────────────────────

describe("toMs", () => {
  it("passes through millisecond timestamps (>= 1e10)", () => {
    expect(toMs(1_700_000_000_000)).toBe(1_700_000_000_000);
  });

  it("converts second timestamps (< 1e10) to ms", () => {
    expect(toMs(1_700_000_000)).toBe(1_700_000_000_000);
  });

  it("returns Date.now() for zero", () => {
    const before = Date.now();
    const result = toMs(0);
    expect(result).toBeGreaterThanOrEqual(before);
  });

  it("returns Date.now() for negative values", () => {
    const before = Date.now();
    const result = toMs(-100);
    expect(result).toBeGreaterThanOrEqual(before);
  });

  it("returns Date.now() for non-finite values", () => {
    const before = Date.now();
    expect(toMs(NaN)).toBeGreaterThanOrEqual(before);
    expect(toMs(Infinity)).toBeGreaterThanOrEqual(before);
  });

  it("returns Date.now() for undefined/null", () => {
    const before = Date.now();
    expect(toMs(undefined)).toBeGreaterThanOrEqual(before);
    expect(toMs(null)).toBeGreaterThanOrEqual(before);
  });

  it("handles numeric strings", () => {
    expect(toMs("1700000000000")).toBe(1_700_000_000_000);
  });
});

// ── nameFromId ────────────────────────────────────────────────────────────────

describe("nameFromId", () => {
  it("extracts name segment from HLC-WID", () => {
    expect(nameFromId("20260325T151725.0000Z-main-actor")).toBe("main-actor");
  });

  it("strips trailing 6-hex suffix if present", () => {
    expect(nameFromId("20260325T151725.0000Z-main-actor-ab12cd")).toBe("main-actor");
  });

  it("falls back to raw string when not a WID", () => {
    expect(nameFromId("plain-name")).toBe("plain-name");
  });

  it("handles empty string", () => {
    expect(nameFromId("")).toBe("");
  });

  it("handles WID with multi-part name", () => {
    expect(nameFromId("20260325T151725.0000Z-io-agent")).toBe("io-agent");
  });
});

// ── resolveAgentName ──────────────────────────────────────────────────────────

describe("resolveAgentName", () => {
  const wid = "20260325T151725.0000Z-bravo";

  it("returns name resolved from WID when name is empty", () => {
    expect(resolveAgentName("", wid)).toBe("bravo");
  });

  it("returns name resolved from WID when name is only digits", () => {
    expect(resolveAgentName("12345", wid)).toBe("bravo");
  });

  it("uses name directly when not numeric-only", () => {
    expect(resolveAgentName("my-agent", wid)).toBe("my-agent");
  });

  it("extracts name segment from a WID passed as name", () => {
    const nameWid = "20260325T151725.0000Z-alpha";
    expect(resolveAgentName(nameWid, wid)).toBe("alpha");
  });
});

// ── normaliseHeartbeat ────────────────────────────────────────────────────────

describe("normaliseHeartbeat", () => {
  it("accepts camelCase Rust payload", () => {
    const hb = normaliseHeartbeat({
      agentId: "20260325T151725.0000Z-alpha",
      agentName: "alpha",
      state: "running",
      sequence: 5,
      timestampMs: 1_700_000_000_000,
    });
    expect(hb.agentId).toBe("20260325T151725.0000Z-alpha");
    expect(hb.agentName).toBe("alpha");
    expect(hb.sequence).toBe(5);
    expect(hb.timestampMs).toBe(1_700_000_000_000);
  });

  it("accepts snake_case Python payload and converts timestamp", () => {
    const hb = normaliseHeartbeat({
      actor_id: "20260325T151725.0000Z-bravo",
      name: "bravo",
      state: "running",
      sequence: 1,
      timestamp: 1_700_000_000, // seconds
    });
    expect(hb.agentId).toBe("20260325T151725.0000Z-bravo");
    expect(hb.agentName).toBe("bravo");
    expect(hb.timestampMs).toBe(1_700_000_000_000);
  });

  it("falls back sequence to 0 when missing", () => {
    const hb = normaliseHeartbeat({ agentId: "x", agentName: "x", state: "running" });
    expect(hb.sequence).toBe(0);
  });

  it("resolves name from WID id when name is absent", () => {
    const hb = normaliseHeartbeat({ agentId: "20260325T151725.0000Z-delta", state: "running" });
    expect(hb.agentName).toBe("delta");
  });

  it("handles empty payload gracefully", () => {
    const hb = normaliseHeartbeat({});
    expect(hb.agentId).toBe("");
    expect(hb.sequence).toBe(0);
  });

  it("handles null payload", () => {
    const hb = normaliseHeartbeat(null);
    expect(hb.agentId).toBe("");
  });
});

// ── normaliseChat ─────────────────────────────────────────────────────────────

describe("normaliseChat", () => {
  it("passes through well-formed chat message", () => {
    const msg = normaliseChat({
      id: "msg-1",
      from: "main-actor",
      to: "user",
      content: "Hello",
      timestampMs: 1_700_000_000_000,
    });
    expect(msg.id).toBe("msg-1");
    expect(msg.from).toBe("main-actor");
    expect(msg.to).toBe("user");
    expect(msg.content).toBe("Hello");
  });

  it("generates id when absent", () => {
    const msg = normaliseChat({ content: "Hi", timestampMs: 1_700_000_000_000 });
    expect(msg.id).toMatch(/^chat-/);
  });

  it("defaults to field from agentName when from is absent", () => {
    const msg = normaliseChat({ agentName: "io-agent", content: "hi" });
    expect(msg.from).toBe("io-agent");
  });

  it("defaults to 'user' when to is absent", () => {
    const msg = normaliseChat({ from: "agent", content: "hi" });
    expect(msg.to).toBe("user");
  });

  it("converts Python second timestamp", () => {
    const msg = normaliseChat({ timestamp: 1_700_000_000 });
    expect(msg.timestampMs).toBe(1_700_000_000_000);
  });

  it("handles null payload", () => {
    const msg = normaliseChat(null);
    expect(msg.content).toBe("");
    expect(msg.to).toBe("user");
  });
});

// ── normaliseStatus ───────────────────────────────────────────────────────────

describe("normaliseStatus", () => {
  it("maps camelCase fields", () => {
    const s = normaliseStatus({
      agentId: "20260325T151725.0000Z-alpha",
      agentName: "alpha",
      state: "running",
      messagesReceived: 10,
      messagesProcessed: 8,
      messagesFailed: 2,
    });
    expect(s.agentId).toBe("20260325T151725.0000Z-alpha");
    expect(s.agentName).toBe("alpha");
  });

  it("maps snake_case actor_id", () => {
    const s = normaliseStatus({ actor_id: "20260325T151725.0000Z-bravo", name: "bravo" });
    expect(s.agentId).toBe("20260325T151725.0000Z-bravo");
    expect(s.agentName).toBe("bravo");
  });
});

// ── MQTTClient ────────────────────────────────────────────────────────────────

describe("MQTTClient", () => {
  let client: MQTTClient;

  beforeEach(() => {
    vi.clearAllMocks();
    mockConnected = true;
    client = new MQTTClient("ws://localhost/mqtt");
    client.connect();
    // Simulate "connect" event so subscriptions fire
    mockHandlers["connect"]?.();
  });

  afterEach(() => {
    client.disconnect();
  });

  // ── lifecycle ───────────────────────────────────────────────────────────────

  it("emits 'connected' on broker connect", () => {
    const spy = vi.fn();
    const c = new MQTTClient();
    c.on("connected", spy);
    c.connect();
    mockHandlers["connect"]?.();
    expect(spy).toHaveBeenCalled();
  });

  it("emits 'disconnected' on broker disconnect", () => {
    const spy = vi.fn();
    client.on("disconnected", spy);
    mockHandlers["disconnect"]?.();
    expect(spy).toHaveBeenCalled();
  });

  it("emits 'disconnected' on TCP-level close event", () => {
    const spy = vi.fn();
    client.on("disconnected", spy);
    mockHandlers["close"]?.();
    expect(spy).toHaveBeenCalled();
  });

  it("emits 'error' on broker error", () => {
    const spy = vi.fn();
    client.on("error", spy);
    const err = new Error("connection refused");
    mockHandlers["error"]?.(err);
    expect(spy).toHaveBeenCalledWith(err);
  });

  it("disconnect() calls end and clears client", () => {
    client.disconnect();
    expect(mockMqttClient.end).toHaveBeenCalledWith(true);
  });

  // ── publish ─────────────────────────────────────────────────────────────────

  it("publish() sends JSON and returns true when connected", () => {
    const ok = client.publish("io/chat", { content: "hello" });
    expect(ok).toBe(true);
    expect(mockMqttClient.publish).toHaveBeenCalledWith(
      "io/chat",
      JSON.stringify({ content: "hello" }),
      { qos: 1 },
    );
  });

  it("publish() returns false when not connected", () => {
    mockConnected = false;
    expect(client.publish("x", {})).toBe(false);
  });

  // ── on / off ────────────────────────────────────────────────────────────────

  it("off() removes a specific listener", () => {
    const spy = vi.fn();
    client.on("heartbeat", spy);
    client.off("heartbeat", spy);
    triggerMessage("agents/id/heartbeat", { agentId: "x", agentName: "x", state: "running", sequence: 0, timestampMs: Date.now() });
    expect(spy).not.toHaveBeenCalled();
  });

  it("off() on unknown event is a no-op", () => {
    const spy = vi.fn();
    expect(() => client.off("heartbeat", spy)).not.toThrow();
  });

  // ── heartbeat routing ────────────────────────────────────────────────────────

  it("routes agents/{id}/heartbeat → 'heartbeat'", () => {
    const spy = vi.fn();
    client.on("heartbeat", spy);
    triggerMessage("agents/20260325T151725.0000Z-alpha/heartbeat", {
      agentId: "20260325T151725.0000Z-alpha",
      agentName: "alpha",
      state: "running",
      sequence: 1,
      timestampMs: 1_700_000_000_000,
    });
    expect(spy).toHaveBeenCalledOnce();
    expect(spy.mock.calls[0]![0].agentName).toBe("alpha");
  });

  // ── status routing ───────────────────────────────────────────────────────────

  it("routes agents/{id}/status → 'status'", () => {
    const spy = vi.fn();
    client.on("status", spy);
    triggerMessage("agents/abc/status", {
      agentId: "abc",
      agentName: "abc",
      state: "running",
      messagesReceived: 1,
      messagesProcessed: 1,
      messagesFailed: 0,
    });
    expect(spy).toHaveBeenCalledOnce();
  });

  // ── alert routing ────────────────────────────────────────────────────────────

  it("routes agents/{id}/alert → 'alert'", () => {
    const spy = vi.fn();
    client.on("alert", spy);
    triggerMessage("agents/abc/alert", {
      agentId: "abc",
      agentName: "abc",
      severity: "warning",
      message: "disk low",
      timestampMs: Date.now(),
    });
    expect(spy).toHaveBeenCalledOnce();
  });

  // ── chat routing ─────────────────────────────────────────────────────────────

  it("routes agents/{id}/chat → 'chat'", () => {
    const spy = vi.fn();
    client.on("chat", spy);
    triggerMessage("agents/abc/chat", { id: "m1", from: "abc", to: "user", content: "hi", timestampMs: Date.now() });
    expect(spy).toHaveBeenCalledOnce();
    expect(spy.mock.calls[0]![0].content).toBe("hi");
  });

  // ── spawn routing ────────────────────────────────────────────────────────────

  it("routes agents/{id}/spawn → 'spawn'", () => {
    const spy = vi.fn();
    client.on("spawn", spy);
    triggerMessage("agents/abc/spawn", { agentId: "abc", agentName: "abc", agentType: "dynamic", timestampMs: Date.now() });
    expect(spy).toHaveBeenCalledOnce();
  });

  it("routes system/spawn → 'spawn'", () => {
    const spy = vi.fn();
    client.on("spawn", spy);
    triggerMessage("system/spawn", { agentId: "x", agentName: "x", agentType: "monitor", timestampMs: Date.now() });
    expect(spy).toHaveBeenCalledOnce();
  });

  // ── system topics ─────────────────────────────────────────────────────────────

  it("routes system/qa-flag → 'qa-flag'", () => {
    const spy = vi.fn();
    client.on("qa-flag", spy);
    triggerMessage("system/qa-flag", { agentId: "x", agentName: "x", from: "user", category: "safety", severity: "high", excerpt: "...", message: "bad", timestampMs: Date.now() });
    expect(spy).toHaveBeenCalledOnce();
  });

  it("routes system/health → 'system-health'", () => {
    const spy = vi.fn();
    client.on("system-health", spy);
    triggerMessage("system/health", { status: "ok" });
    expect(spy).toHaveBeenCalledOnce();
  });

  // ── system/host ───────────────────────────────────────────────────────────────

  it("routes system/host → 'host-stats' with camelCase fields", () => {
    const spy = vi.fn();
    client.on("host-stats", spy);
    triggerMessage("system/host", { cpu: 45.2, memUsedMb: 1024, memTotalMb: 8192 });
    expect(spy).toHaveBeenCalledOnce();
    const s = spy.mock.calls[0]![0];
    expect(s.cpu).toBe(45.2);
    expect(s.memUsedMb).toBe(1024);
    expect(s.memTotalMb).toBe(8192);
  });

  it("routes system/host → 'host-stats' with snake_case alt fields", () => {
    const spy = vi.fn();
    client.on("host-stats", spy);
    triggerMessage("system/host", { cpu_pct: 60.0, mem_used_mb: 2048, mem_total_mb: 16384 });
    const s = spy.mock.calls[0]![0];
    expect(s.cpu).toBe(60.0);
    expect(s.memUsedMb).toBe(2048);
    expect(s.memTotalMb).toBe(16384);
  });

  it("routes system/host → 'host-stats' omitting non-numeric fields", () => {
    const spy = vi.fn();
    client.on("host-stats", spy);
    triggerMessage("system/host", { cpu: "high", memUsedMb: null, memTotalMb: undefined });
    const s = spy.mock.calls[0]![0];
    expect(s.cpu).toBeUndefined();
    expect(s.memUsedMb).toBeUndefined();
    expect(s.memTotalMb).toBeUndefined();
  });

  it("routes system/coin → 'coin'", () => {
    const spy = vi.fn();
    client.on("coin", spy);
    triggerMessage("system/coin", { balance: 42 });
    expect(spy).toHaveBeenCalledOnce();
    expect(spy.mock.calls[0]![0].balance).toBe(42);
  });

  // ── metrics ───────────────────────────────────────────────────────────────────

  it("routes agents/{id}/metrics → 'metrics' with camelCase fields", () => {
    const spy = vi.fn();
    client.on("metrics", spy);
    triggerMessage("agents/abc123/metrics", {
      agentName: "my-agent",
      costUsd: 0.01,
      inputTokens: 100,
      outputTokens: 50,
      messagesProcessed: 5,
      uptime: 3600,
    });
    expect(spy).toHaveBeenCalledOnce();
    const m = spy.mock.calls[0]![0];
    expect(m.agentId).toBe("abc123");
    expect(m.costUsd).toBe(0.01);
    expect(m.inputTokens).toBe(100);
    expect(m.uptime).toBe(3600);
  });

  it("routes agents/{id}/metrics → 'metrics' with snake_case fields", () => {
    const spy = vi.fn();
    client.on("metrics", spy);
    triggerMessage("agents/abc123/metrics", {
      name: "my-agent",
      cost_usd: 0.02,
      input_tokens: 200,
      output_tokens: 100,
      messages_processed: 10,
    });
    const m = spy.mock.calls[0]![0];
    expect(m.costUsd).toBe(0.02);
    expect(m.inputTokens).toBe(200);
    expect(m.outputTokens).toBe(100);
    expect(m.messagesProcessed).toBe(10);
  });

  it("uses short agentId as name fallback in metrics", () => {
    const spy = vi.fn();
    client.on("metrics", spy);
    triggerMessage("agents/abcdefgh/metrics", { costUsd: 0.001 });
    expect(spy.mock.calls[0]![0].agentName).toBe("abcdefgh");
  });

  // ── logs ──────────────────────────────────────────────────────────────────────

  it("routes agents/{id}/logs → 'logs'", () => {
    const spy = vi.fn();
    client.on("logs", spy);
    triggerMessage("agents/abc/logs", { agentName: "abc", message: "started" });
    expect(spy).toHaveBeenCalledOnce();
    expect(spy.mock.calls[0]![0].message).toBe("started");
  });

  it("routes agents/{id}/logs with 'text' field", () => {
    const spy = vi.fn();
    client.on("logs", spy);
    triggerMessage("agents/abc/logs", { text: "output line" });
    expect(spy.mock.calls[0]![0].message).toBe("output line");
  });

  // ── completed ─────────────────────────────────────────────────────────────────

  it("routes agents/{id}/completed → 'completed'", () => {
    const spy = vi.fn();
    client.on("completed", spy);
    triggerMessage("agents/abc/completed", { agentName: "abc" });
    expect(spy).toHaveBeenCalledOnce();
    expect(spy.mock.calls[0]![0].agentId).toBe("abc");
  });

  // ── node heartbeat ────────────────────────────────────────────────────────────

  it("routes nodes/{name}/heartbeat → 'node-heartbeat'", () => {
    const spy = vi.fn();
    client.on("node-heartbeat", spy);
    triggerMessage("nodes/alpha/heartbeat", { agents: ["a", "b"], node_id: "nid-1" });
    expect(spy).toHaveBeenCalledOnce();
    const p = spy.mock.calls[0]![0];
    expect(p.node).toBe("alpha");
    expect(p.agents).toEqual(["a", "b"]);
    expect(p.nodeId).toBe("nid-1");
  });

  it("node-heartbeat without node_id omits nodeId", () => {
    const spy = vi.fn();
    client.on("node-heartbeat", spy);
    triggerMessage("nodes/beta/heartbeat", { agents: [] });
    expect(spy.mock.calls[0]![0].nodeId).toBeUndefined();
  });

  // ── raw fallthrough ────────────────────────────────────────────────────────────

  it("emits 'raw' for unrecognised topics", () => {
    const spy = vi.fn();
    client.on("raw", spy);
    triggerMessage("unknown/topic/here", { foo: "bar" });
    expect(spy).toHaveBeenCalledOnce();
    expect(spy.mock.calls[0]![0].topic).toBe("unknown/topic/here");
  });

  it("silently ignores malformed JSON", () => {
    const raw = Buffer.from("not json at all");
    expect(() => mockHandlers["message"]?.("agents/x/heartbeat", raw)).not.toThrow();
  });

  // ── listener error isolation ───────────────────────────────────────────────────

  it("continues emitting to other listeners when one throws", () => {
    const failing = vi.fn(() => { throw new Error("oops"); });
    const ok = vi.fn();
    client.on("coin", failing);
    client.on("coin", ok);
    triggerMessage("system/coin", { balance: 1 });
    expect(ok).toHaveBeenCalledOnce();
  });
});
