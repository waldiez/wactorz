// typescript/src/time.ts
function parseTimeUnit(input) {
  if (input === "sec" || input === "ms") {
    return input;
  }
  throw new Error("time-unit must be sec or ms");
}
function timeDigits(unit) {
  return unit === "ms" ? 9 : 6;
}

// typescript/src/wid.ts
var MemoryWidStateStore = class {
  constructor() {
    this.memory = /* @__PURE__ */ new Map();
  }
  load(key) {
    const hit = this.memory.get(key);
    return hit ? { ...hit } : null;
  }
  save(key, state) {
    this.memory.set(key, { ...state });
  }
};
var BrowserLocalStorageWidStateStore = class {
  constructor(prefix = "wid") {
    this.prefix = prefix;
  }
  keyOf(key) {
    return `${this.prefix}:${key}`;
  }
  localStorageLike() {
    const g = globalThis;
    const ls = g.localStorage;
    return ls ?? null;
  }
  load(key) {
    const ls = this.localStorageLike();
    if (!ls) return null;
    const raw = ls.getItem(this.keyOf(key));
    if (!raw) return null;
    try {
      const parsed = JSON.parse(raw);
      if (typeof parsed.lastSec === "number" && Number.isFinite(parsed.lastSec) && typeof parsed.lastSeq === "number" && Number.isFinite(parsed.lastSeq)) {
        return { lastSec: parsed.lastSec, lastSeq: parsed.lastSeq };
      }
    } catch {
      return null;
    }
    return null;
  }
  save(key, state) {
    const ls = this.localStorageLike();
    if (!ls) return;
    ls.setItem(this.keyOf(key), JSON.stringify(state));
  }
};
function createBrowserWidStateStore(prefix = "wid") {
  return new BrowserLocalStorageWidStateStore(prefix);
}
var NodeSqliteWidStateStore = class {
  constructor(databasePath, prefix = "wid") {
    this.prefix = prefix;
    const DatabaseSync = resolveNodeSqliteDatabaseSync();
    this.db = new DatabaseSync(databasePath);
    this.db.exec(
      "CREATE TABLE IF NOT EXISTS wid_state (k TEXT PRIMARY KEY, last_sec INTEGER NOT NULL, last_seq INTEGER NOT NULL)"
    );
  }
  fullKey(key) {
    return `${this.prefix}:${key}`;
  }
  load(key) {
    const row = this.db.prepare("SELECT last_sec, last_seq FROM wid_state WHERE k = ?").get(this.fullKey(key));
    if (!row) return null;
    if (typeof row.last_sec !== "number" || typeof row.last_seq !== "number") return null;
    return { lastSec: row.last_sec, lastSeq: row.last_seq };
  }
  save(key, state) {
    this.db.prepare(
      "INSERT INTO wid_state (k, last_sec, last_seq) VALUES (?, ?, ?) ON CONFLICT(k) DO UPDATE SET last_sec=excluded.last_sec, last_seq=excluded.last_seq"
    ).run(this.fullKey(key), state.lastSec, state.lastSeq);
  }
  close() {
    this.db.close?.();
  }
};
function resolveNodeSqliteDatabaseSync() {
  const proc = globalThis.process;
  if (!proc?.versions?.node) {
    throw new Error("SQLite state store requires Node.js");
  }
  const builtin = typeof proc.getBuiltinModule === "function" ? proc.getBuiltinModule("node:sqlite") : null;
  if (builtin && typeof builtin === "object" && "DatabaseSync" in builtin) {
    return builtin.DatabaseSync;
  }
  throw new Error("node:sqlite unavailable in this Node runtime");
}
function createNodeSqliteWidStateStore(databasePath, prefix = "wid") {
  return new NodeSqliteWidStateStore(databasePath, prefix);
}
var WID_BASE_RE_CACHE = /* @__PURE__ */ new Map();
var HEX_RE_CACHE = /* @__PURE__ */ new Map();
var SCOPE_PATTERN = /^[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*$/;
function widBaseRe(W, unit) {
  const key = `${W}:${unit}`;
  const cached = WID_BASE_RE_CACHE.get(key);
  if (cached) return cached;
  const re = new RegExp(`^(\\d{8})T(\\d{${timeDigits(unit)}})\\.(\\d{${W}})Z(.*)?$`);
  WID_BASE_RE_CACHE.set(key, re);
  return re;
}
function hexRe(Z) {
  const cached = HEX_RE_CACHE.get(Z);
  if (cached) return cached;
  const re = new RegExp(`^[0-9a-f]{${Z}}$`);
  HEX_RE_CACHE.set(Z, re);
  return re;
}
function bytesToHex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
function randomHexChars(Z) {
  if (!globalThis.crypto?.getRandomValues) {
    throw new Error("Secure random generator unavailable in this runtime");
  }
  const bytes = new Uint8Array(Math.ceil(Z / 2));
  globalThis.crypto.getRandomValues(bytes);
  return bytesToHex(bytes).slice(0, Z);
}
function parseSuffix(suffix, Z) {
  if (!suffix) {
    return { scope: null, padding: null };
  }
  if (!suffix.startsWith("-")) {
    return null;
  }
  const body = suffix.slice(1);
  if (!body) {
    return null;
  }
  let scope = null;
  let padding = null;
  if (Z > 0) {
    const splitAt = body.lastIndexOf("-");
    if (splitAt >= 0) {
      const maybeScope = body.slice(0, splitAt);
      const maybePadding = body.slice(splitAt + 1);
      if (hexRe(Z).test(maybePadding)) {
        padding = maybePadding;
        scope = maybeScope || null;
      } else if (maybePadding.length === Z && /^[0-9A-Fa-f]+$/.test(maybePadding)) {
        return null;
      } else {
        scope = body;
      }
    } else if (hexRe(Z).test(body)) {
      padding = body;
    } else if (body.length === Z && /^[0-9A-Fa-f]+$/.test(body)) {
      return null;
    } else {
      scope = body;
    }
  } else {
    scope = body;
  }
  if (scope !== null && !SCOPE_PATTERN.test(scope)) {
    return null;
  }
  if (padding !== null && !hexRe(Z).test(padding)) {
    return null;
  }
  return { scope, padding };
}
function parseTimestamp(dateStr, timeStr, timeUnit) {
  const year = parseInt(dateStr.slice(0, 4), 10);
  const month = parseInt(dateStr.slice(4, 6), 10);
  const day = parseInt(dateStr.slice(6, 8), 10);
  const hour = parseInt(timeStr.slice(0, 2), 10);
  const minute = parseInt(timeStr.slice(2, 4), 10);
  const second = parseInt(timeStr.slice(4, 6), 10);
  const millis = timeUnit === "ms" ? parseInt(timeStr.slice(6, 9), 10) : 0;
  if (month < 1 || month > 12) return null;
  if (day < 1 || day > 31) return null;
  if (hour > 23 || minute > 59 || second > 59) return null;
  if (millis < 0 || millis > 999) return null;
  const timestamp = new Date(Date.UTC(year, month - 1, day, hour, minute, second, millis));
  if (isNaN(timestamp.getTime())) return null;
  if (timestamp.getUTCDate() !== day || timestamp.getUTCMonth() + 1 !== month) return null;
  return timestamp;
}
function parseCore(wid, W, Z, timeUnit) {
  if (W <= 0 || Z < 0) return null;
  const match = widBaseRe(W, timeUnit).exec(wid);
  if (!match) return null;
  const [, dateStr, timeStr, seqStr, suffixRaw] = match;
  const suffix = suffixRaw ?? "";
  const timestamp = parseTimestamp(dateStr, timeStr, timeUnit);
  if (!timestamp) return null;
  const parsedSuffix = parseSuffix(suffix, Z);
  if (!parsedSuffix) return null;
  return {
    raw: wid,
    timestamp,
    sequence: parseInt(seqStr, 10),
    scope: parsedSuffix.scope,
    padding: parsedSuffix.padding
  };
}
function validateWid(wid, W = 4, Z = 6, timeUnit = "sec") {
  return parseCore(wid, W, Z, timeUnit) !== null;
}
function parseWid(wid, W = 4, Z = 6, timeUnit = "sec") {
  return parseCore(wid, W, Z, timeUnit);
}
async function asyncNextWid(options = {}) {
  return new WidGen(options).next();
}
async function* asyncWidStream(options = {}) {
  const { count = 0, intervalMs = 0, ...genOpts } = options;
  if (count < 0) throw new Error("count must be >= 0");
  if (intervalMs < 0) throw new Error("intervalMs must be >= 0");
  const gen = new WidGen(genOpts);
  let emitted = 0;
  while (count === 0 || emitted < count) {
    yield gen.next();
    emitted += 1;
    if (intervalMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
  }
}
var WidGen = class {
  /** Create a generator with optional persistence and precision control. */
  constructor(options = {}) {
    this.lastSec = 0;
    this.lastSeq = -1;
    this.cachedSec = -1;
    this.cachedTs = "";
    const {
      W = 4,
      Z = 6,
      scope,
      timeUnit = "sec",
      stateStore,
      stateKey = "wid",
      autoPersist = false
    } = options;
    if (W <= 0) throw new Error("W must be > 0");
    if (Z < 0) throw new Error("Z must be >= 0");
    if (scope && !SCOPE_PATTERN.test(scope)) {
      throw new Error("Invalid scope format");
    }
    this.W = W;
    this.Z = Z;
    this.scope = scope ?? null;
    this.timeUnit = timeUnit;
    this.maxSeq = Math.pow(10, W) - 1;
    this.stateStore = stateStore ?? null;
    this.stateKey = stateKey;
    this.autoPersist = autoPersist;
    if (this.autoPersist && this.stateStore) {
      const loaded = this.stateStore.load(this.stateKey);
      if (loaded && Number.isFinite(loaded.lastSec) && Number.isFinite(loaded.lastSeq) && loaded.lastSec >= 0 && loaded.lastSeq >= -1) {
        this.lastSec = loaded.lastSec;
        this.lastSeq = loaded.lastSeq;
      }
    }
  }
  persistState() {
    if (!this.autoPersist || !this.stateStore) return;
    try {
      this.stateStore.save(this.stateKey, { lastSec: this.lastSec, lastSeq: this.lastSeq });
    } catch {
    }
  }
  tsForTick(tick) {
    if (tick !== this.cachedSec) {
      this.cachedSec = tick;
      const sec = this.timeUnit === "ms" ? Math.floor(tick / 1e3) : tick;
      const ms = this.timeUnit === "ms" ? tick % 1e3 : 0;
      const d = new Date(sec * 1e3);
      const year = d.getUTCFullYear();
      const month = String(d.getUTCMonth() + 1).padStart(2, "0");
      const day = String(d.getUTCDate()).padStart(2, "0");
      const hour = String(d.getUTCHours()).padStart(2, "0");
      const minute = String(d.getUTCMinutes()).padStart(2, "0");
      const second = String(d.getUTCSeconds()).padStart(2, "0");
      const milli = String(ms).padStart(3, "0");
      this.cachedTs = this.timeUnit === "ms" ? `${year}${month}${day}T${hour}${minute}${second}${milli}` : `${year}${month}${day}T${hour}${minute}${second}`;
    }
    return this.cachedTs;
  }
  nowTick() {
    if (this.timeUnit === "ms") {
      return Date.now();
    }
    return Math.floor(Date.now() / 1e3);
  }
  next() {
    const now = this.nowTick();
    let tick = now > this.lastSec ? now : this.lastSec;
    let seq = tick === this.lastSec ? this.lastSeq + 1 : 0;
    if (seq > this.maxSeq) {
      tick += 1;
      seq = 0;
    }
    this.lastSec = tick;
    this.lastSeq = seq;
    const ts = this.tsForTick(tick);
    const seqStr = String(seq).padStart(this.W, "0");
    let wid = `${ts}.${seqStr}Z`;
    if (this.scope) {
      wid += `-${this.scope}`;
    }
    if (this.Z > 0) {
      wid += `-${randomHexChars(this.Z)}`;
    }
    this.persistState();
    return wid;
  }
  nextN(n) {
    return Array.from({ length: n }, () => this.next());
  }
  get state() {
    return { lastSec: this.lastSec, lastSeq: this.lastSeq };
  }
  restoreState(lastSec, lastSeq) {
    this.lastSec = lastSec;
    this.lastSeq = lastSeq;
    this.persistState();
  }
};

// typescript/src/hlc.ts
var HLC_BASE_RE_CACHE = /* @__PURE__ */ new Map();
var HEX_RE_CACHE2 = /* @__PURE__ */ new Map();
var NODE_RE = /^[A-Za-z0-9_]+$/;
function hlcBaseRe(W, unit) {
  const key = `${W}:${unit}`;
  const cached = HLC_BASE_RE_CACHE.get(key);
  if (cached) return cached;
  const re = new RegExp(`^(\\d{8})T(\\d{${timeDigits(unit)}})\\.(\\d{${W}})Z-([A-Za-z0-9_]+)(.*)$`);
  HLC_BASE_RE_CACHE.set(key, re);
  return re;
}
function hexRe2(Z) {
  const cached = HEX_RE_CACHE2.get(Z);
  if (cached) return cached;
  const re = new RegExp(`^[0-9a-f]{${Z}}$`);
  HEX_RE_CACHE2.set(Z, re);
  return re;
}
function randomHexChars2(Z) {
  if (!globalThis.crypto?.getRandomValues) {
    throw new Error("Secure random generator unavailable in this runtime");
  }
  const bytes = new Uint8Array(Math.ceil(Z / 2));
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("").slice(0, Z);
}
function isValidNode(node) {
  return NODE_RE.test(node);
}
function parseTimestamp2(dateStr, timeStr, timeUnit) {
  const year = parseInt(dateStr.slice(0, 4), 10);
  const month = parseInt(dateStr.slice(4, 6), 10);
  const day = parseInt(dateStr.slice(6, 8), 10);
  const hour = parseInt(timeStr.slice(0, 2), 10);
  const minute = parseInt(timeStr.slice(2, 4), 10);
  const second = parseInt(timeStr.slice(4, 6), 10);
  const millis = timeUnit === "ms" ? parseInt(timeStr.slice(6, 9), 10) : 0;
  if (month < 1 || month > 12) return null;
  if (day < 1 || day > 31) return null;
  if (hour > 23 || minute > 59 || second > 59) return null;
  if (millis < 0 || millis > 999) return null;
  const timestamp = new Date(Date.UTC(year, month - 1, day, hour, minute, second, millis));
  if (isNaN(timestamp.getTime())) return null;
  if (timestamp.getUTCDate() !== day || timestamp.getUTCMonth() + 1 !== month) return null;
  return timestamp;
}
function validateHlcWid(wid, W = 4, Z = 0, timeUnit = "sec") {
  return parseHlcWid(wid, W, Z, timeUnit) !== null;
}
function parseHlcWid(wid, W = 4, Z = 0, timeUnit = "sec") {
  if (W <= 0 || Z < 0) return null;
  const match = hlcBaseRe(W, timeUnit).exec(wid);
  if (!match) return null;
  const [, dateStr, timeStr, lcStr, node, suffixRaw] = match;
  const suffix = suffixRaw ?? "";
  if (!isValidNode(node)) return null;
  const timestamp = parseTimestamp2(dateStr, timeStr, timeUnit);
  if (!timestamp) return null;
  const logicalCounter = parseInt(lcStr, 10);
  let padding = null;
  if (suffix) {
    if (!suffix.startsWith("-")) return null;
    const seg = suffix.slice(1);
    if (Z === 0) return null;
    if (!hexRe2(Z).test(seg)) return null;
    padding = seg;
  }
  return { raw: wid, timestamp, logicalCounter, node, padding };
}
var HLCWidGen = class {
  constructor(options) {
    this.pt = 0;
    this.lc = 0;
    this.cachedTick = -1;
    this.cachedTs = "";
    const { node, W = 4, Z = 0, timeUnit = "sec" } = options;
    if (W <= 0) throw new Error("W must be > 0");
    if (Z < 0) throw new Error("Z must be >= 0");
    if (!isValidNode(node)) {
      throw new Error("node must match [A-Za-z0-9_]+");
    }
    this.W = W;
    this.Z = Z;
    this.node = node;
    this.timeUnit = timeUnit;
    this.maxLC = Math.pow(10, W) - 1;
  }
  nowTick() {
    if (this.timeUnit === "ms") {
      return Date.now();
    }
    return Math.floor(Date.now() / 1e3);
  }
  tsForTick(tick) {
    if (tick !== this.cachedTick) {
      this.cachedTick = tick;
      const sec = this.timeUnit === "ms" ? Math.floor(tick / 1e3) : tick;
      const ms = this.timeUnit === "ms" ? tick % 1e3 : 0;
      const d = new Date(sec * 1e3);
      const year = d.getUTCFullYear();
      const mo = String(d.getUTCMonth() + 1).padStart(2, "0");
      const dy = String(d.getUTCDate()).padStart(2, "0");
      const hr = String(d.getUTCHours()).padStart(2, "0");
      const mi = String(d.getUTCMinutes()).padStart(2, "0");
      const sc = String(d.getUTCSeconds()).padStart(2, "0");
      const milli = String(ms).padStart(3, "0");
      this.cachedTs = this.timeUnit === "ms" ? `${year}${mo}${dy}T${hr}${mi}${sc}${milli}` : `${year}${mo}${dy}T${hr}${mi}${sc}`;
    }
    return this.cachedTs;
  }
  rollover() {
    if (this.lc > this.maxLC) {
      this.pt += 1;
      this.lc = 0;
    }
  }
  observe(remotePT, remoteLC) {
    if (remotePT < 0 || remoteLC < 0) {
      throw new Error("remote values must be non-negative");
    }
    const now = this.nowTick();
    const newPT = Math.max(now, this.pt, remotePT);
    if (newPT === this.pt && newPT === remotePT) {
      this.lc = Math.max(this.lc, remoteLC) + 1;
    } else if (newPT === this.pt) {
      this.lc += 1;
    } else if (newPT === remotePT) {
      this.lc = remoteLC + 1;
    } else {
      this.lc = 0;
    }
    this.pt = newPT;
    this.rollover();
  }
  next() {
    const now = this.nowTick();
    if (now > this.pt) {
      this.pt = now;
      this.lc = 0;
    } else {
      this.lc += 1;
    }
    this.rollover();
    const ts = this.tsForTick(this.pt);
    const lcStr = String(this.lc).padStart(this.W, "0");
    let wid = `${ts}.${lcStr}Z-${this.node}`;
    if (this.Z > 0) {
      wid += `-${randomHexChars2(this.Z)}`;
    }
    return wid;
  }
  nextN(n) {
    return Array.from({ length: n }, () => this.next());
  }
  get state() {
    return { pt: this.pt, lc: this.lc };
  }
  restoreState(pt, lc) {
    if (pt < 0 || lc < 0) throw new Error("invalid state");
    this.pt = pt;
    this.lc = lc;
  }
};
async function asyncNextHlcWid(options) {
  return new HLCWidGen(options).next();
}
async function* asyncHlcWidStream(options) {
  const { count = 0, intervalMs = 0, ...genOpts } = options;
  if (count < 0) throw new Error("count must be >= 0");
  if (intervalMs < 0) throw new Error("intervalMs must be >= 0");
  const gen = new HLCWidGen(genOpts);
  let emitted = 0;
  while (count === 0 || emitted < count) {
    yield gen.next();
    emitted++;
    if (intervalMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
  }
}

// typescript/src/manifest.ts
var MANIFEST_MAGIC = new Uint8Array([83, 89, 78, 77]);
var MANIFEST_VERSION = 1;
var MAX_MANIFEST_SIZE = 64 * 1024;
var HEADER_SIZE = 10;
var DataType = /* @__PURE__ */ ((DataType2) => {
  DataType2["Unknown"] = "unknown";
  DataType2["Text"] = "text/plain";
  DataType2["Json"] = "application/json";
  DataType2["Binary"] = "application/octet-stream";
  return DataType2;
})(DataType || {});
function concatBytes(parts) {
  const total = parts.reduce((acc, p) => acc + p.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.length;
  }
  return out;
}
function equalBytes(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}
function toHex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
async function sha256Hex(data) {
  if (!globalThis.crypto?.subtle) {
    throw new Error("Web Crypto API (crypto.subtle) is unavailable in this runtime");
  }
  const digest = await globalThis.crypto.subtle.digest("SHA-256", data);
  return toHex(new Uint8Array(digest));
}
function utf8Encode(s) {
  return new TextEncoder().encode(s);
}
function utf8Decode(bytes) {
  return new TextDecoder().decode(bytes);
}
var Manifest = class _Manifest {
  constructor(data) {
    this.id = data.id;
    this.version = data.version ?? MANIFEST_VERSION;
    this.node = data.node ?? "";
    this.data_type = data.data_type ?? "unknown" /* Unknown */;
    this.data_size = data.data_size ?? 0;
    this.data_hash = data.data_hash ?? "";
    this.metadata = data.metadata ?? {};
  }
  toJson() {
    return JSON.stringify(this, null, 2);
  }
  toBytes() {
    return utf8Encode(this.toJson());
  }
  static fromJson(data) {
    return new _Manifest(JSON.parse(data));
  }
  static fromBytes(data) {
    return _Manifest.fromJson(utf8Decode(data));
  }
};
var SynapseFile = class _SynapseFile {
  constructor(manifest, payload = new Uint8Array(0)) {
    this.manifest = manifest;
    this.payload = payload;
  }
  async toBytes() {
    this.manifest.data_size = this.payload.length;
    this.manifest.data_hash = await sha256Hex(this.payload);
    const manifestBytes = this.manifest.toBytes();
    if (manifestBytes.length > MAX_MANIFEST_SIZE) {
      throw new Error(`Manifest too large: ${manifestBytes.length} bytes`);
    }
    const header = new Uint8Array(HEADER_SIZE);
    header.set(MANIFEST_MAGIC, 0);
    const view = new DataView(header.buffer, header.byteOffset, header.byteLength);
    view.setUint16(4, MANIFEST_VERSION, false);
    view.setUint32(6, manifestBytes.length, false);
    return concatBytes([header, manifestBytes, this.payload]);
  }
  static fromBytes(data) {
    if (data.length < HEADER_SIZE) {
      throw new Error("Data too small for SYNAPSE file");
    }
    const magic = data.subarray(0, 4);
    if (!equalBytes(magic, MANIFEST_MAGIC)) {
      throw new Error(`Invalid magic: ${utf8Decode(magic)}`);
    }
    const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
    const manifestSize = view.getUint32(6, false);
    if (manifestSize > MAX_MANIFEST_SIZE) {
      throw new Error(`Manifest too large: ${manifestSize} bytes`);
    }
    const manifestEnd = HEADER_SIZE + manifestSize;
    const manifestBytes = data.subarray(HEADER_SIZE, manifestEnd);
    const manifest = Manifest.fromBytes(manifestBytes);
    const payload = data.subarray(manifestEnd);
    return new _SynapseFile(manifest, payload);
  }
  async verify() {
    const actualHash = await sha256Hex(this.payload);
    return actualHash === this.manifest.data_hash;
  }
};

export {
  parseTimeUnit,
  MemoryWidStateStore,
  createBrowserWidStateStore,
  createNodeSqliteWidStateStore,
  validateWid,
  parseWid,
  asyncNextWid,
  asyncWidStream,
  WidGen,
  validateHlcWid,
  parseHlcWid,
  HLCWidGen,
  asyncNextHlcWid,
  asyncHlcWidStream,
  MANIFEST_MAGIC,
  MANIFEST_VERSION,
  DataType,
  Manifest,
  SynapseFile
};
//# sourceMappingURL=chunk-EX3ZJPJ6.mjs.map