/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * HLC-WID generation — a vendored subset of `@waldiez/wid`.
 *
 * Copied from `waldiez/wid`, `typescript/src/{hlc,time,wid}.ts` at `ea0a8d6`.
 * `HLCWidGen` is verbatim so it can be diffed against upstream; only the
 * helpers it calls came with it. Validation, parsing, the async generators and
 * the manifest formats were not copied.
 *
 * Format: `YYYYMMDDTHHMMSS.<lcW>Z-<node>[-<padZ>]`
 */

/** Supported time-precision units. */
export type TimeUnit = "sec" | "ms";

/** W > 18 would overflow an int64 logical counter. */
const MAX_W = 18;
/** Z > 64 exceeds the C implementation's WID_MAX_Z. */
const MAX_Z = 64;

/** Largest second tick that still formats as a 4-digit year. */
const MAX_SEC_TICK = 253402300799;

/** Node identifier pattern. */
const NODE_RE = /^[A-Za-z0-9_]+$/;

/**
 * Saturate a tick to the formattable range instead of emitting a malformed
 * >8-digit-year id: a corrupted resume state degrades to a pinned timestamp.
 */
function clampTick(tick: number, unit: TimeUnit): number {
    const max = unit === "ms" ? MAX_SEC_TICK * 1000 + 999 : MAX_SEC_TICK;
    if (tick < 0) {
        return 0;
    }
    if (tick > max) {
        return max;
    }
    return tick;
}

function formatTickTimestamp(tick: number, unit: TimeUnit): string {
    const sec = unit === "ms" ? Math.floor(tick / 1000) : tick;
    const ms = unit === "ms" ? tick % 1000 : 0;
    const d = new Date(sec * 1000);
    const year = d.getUTCFullYear();
    const month = String(d.getUTCMonth() + 1).padStart(2, "0");
    const day = String(d.getUTCDate()).padStart(2, "0");
    const hour = String(d.getUTCHours()).padStart(2, "0");
    const minute = String(d.getUTCMinutes()).padStart(2, "0");
    const second = String(d.getUTCSeconds()).padStart(2, "0");
    const base = `${year}${month}${day}T${hour}${minute}${second}`;
    return unit === "ms" ? `${base}${String(ms).padStart(3, "0")}` : base;
}

function randomHexChars(Z: number): string {
    if (!globalThis.crypto?.getRandomValues) {
        throw new Error("Secure random generator unavailable in this runtime");
    }
    const bytes = new Uint8Array(Math.ceil(Z / 2));
    globalThis.crypto.getRandomValues(bytes);
    return Array.from(bytes, b => b.toString(16).padStart(2, "0"))
        .join("")
        .slice(0, Z);
}

function isValidNode(node: string): boolean {
    return NODE_RE.test(node);
}

/** Generator state, for resuming across a restart. */
export interface HLCState {
    pt: number;
    lc: number;
}

export interface HLCWidGenOptions {
    /** Unique node identifier suffix. */
    node: string;
    /** Width of the logical counter (default 4). */
    W?: number;
    /** Optional padding length (default 0). */
    Z?: number;
    /** Time precision (defaults to `sec`). */
    timeUnit?: TimeUnit;
}

export class HLCWidGen {
    private readonly W: number;
    private readonly Z: number;
    private readonly node: string;
    private readonly timeUnit: TimeUnit;
    private readonly maxLC: number;
    private pt = 0;
    private lc = 0;
    private cachedTick = -1;
    private cachedTs = "";

    constructor(options: HLCWidGenOptions) {
        const { node, W = 4, Z = 0, timeUnit = "sec" } = options;
        if (W <= 0 || W > MAX_W) {
            throw new Error("W must be between 1 and 18");
        }
        if (Z < 0 || Z > MAX_Z) {
            throw new Error("Z must be between 0 and 64");
        }
        if (!isValidNode(node)) {
            throw new Error("node must match [A-Za-z0-9_]+");
        }
        this.W = W;
        this.Z = Z;
        this.node = node;
        this.timeUnit = timeUnit;
        this.maxLC = Math.min(Math.pow(10, W) - 1, Number.MAX_SAFE_INTEGER - 1);
    }

    private nowTick(): number {
        if (this.timeUnit === "ms") {
            return Date.now();
        }
        return Math.floor(Date.now() / 1000);
    }

    private tsForTick(rawTick: number): string {
        const tick = clampTick(rawTick, this.timeUnit);
        if (tick !== this.cachedTick) {
            this.cachedTick = tick;
            this.cachedTs = formatTickTimestamp(tick, this.timeUnit);
        }
        return this.cachedTs;
    }

    private rollover(): void {
        if (this.lc > this.maxLC) {
            this.pt += 1;
            this.lc = 0;
        }
    }

    observe(remotePT: number, remoteLC: number): void {
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

    next(): string {
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
            wid += `-${randomHexChars(this.Z)}`;
        }
        return wid;
    }

    nextN(n: number): string[] {
        return Array.from({ length: n }, () => this.next());
    }

    get state(): HLCState {
        return { pt: this.pt, lc: this.lc };
    }

    restoreState(pt: number, lc: number): void {
        if (pt < 0 || lc < 0) {
            throw new Error("invalid state");
        }
        this.pt = pt;
        this.lc = lc;
    }
}
