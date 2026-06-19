/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * AmbientManager — procedural background soundscapes via Web Audio API.
 * Ported from the reader project's useAmbient hook (no audio files needed).
 *
 * Tracks: none | rain | forest | beach | cafe
 * Auto-ducks to DUCK_VOLUME when TTS is speaking.
 */

const DUCK_VOLUME = 0.12;
const LS_TRACK = "wactorz.ambientTrack";
const LS_VOLUME = "wactorz.ambientVolume";

export type AmbientTrackId = "none" | "rain" | "forest" | "beach" | "cafe";

export const AMBIENT_TRACKS: { id: AmbientTrackId; label: string }[] = [
    { id: "none", label: "Off" },
    { id: "rain", label: "🌧 Rain" },
    { id: "forest", label: "🌲 Forest" },
    { id: "beach", label: "🌊 Beach" },
    { id: "cafe", label: "☕ Cafe" },
];

type Stopper = () => void;

function makeWhiteNoise(ctx: AudioContext): AudioBufferSourceNode {
    const len = ctx.sampleRate * 2;
    const buf = ctx.createBuffer(1, len, ctx.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < len; i++) {
        d[i] = Math.random() * 2 - 1;
    }
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.loop = true;
    return src;
}

function makePinkNoise(ctx: AudioContext): AudioBufferSourceNode {
    const len = ctx.sampleRate * 2;
    const buf = ctx.createBuffer(1, len, ctx.sampleRate);
    const d = buf.getChannelData(0);
    let b0 = 0,
        b1 = 0,
        b2 = 0,
        b3 = 0,
        b4 = 0,
        b5 = 0,
        b6 = 0;
    for (let i = 0; i < len; i++) {
        const w = Math.random() * 2 - 1;
        b0 = 0.99886 * b0 + w * 0.0555179;
        b1 = 0.99332 * b1 + w * 0.0750759;
        b2 = 0.969 * b2 + w * 0.153852;
        b3 = 0.8665 * b3 + w * 0.3104856;
        b4 = 0.55 * b4 + w * 0.5329522;
        b5 = -0.7616 * b5 - w * 0.016898;
        d[i] = (b0 + b1 + b2 + b3 + b4 + b5 + b6 + w * 0.5362) * 0.11;
        b6 = w * 0.115926;
    }
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.loop = true;
    return src;
}

function makeBrownNoise(ctx: AudioContext): AudioBufferSourceNode {
    const len = ctx.sampleRate * 2;
    const buf = ctx.createBuffer(1, len, ctx.sampleRate);
    const d = buf.getChannelData(0);
    let last = 0;
    for (let i = 0; i < len; i++) {
        last = (last + 0.02 * (Math.random() * 2 - 1)) / 1.02;
        d[i] = last * 3.5;
    }
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.loop = true;
    return src;
}

function lfoMod(ctx: AudioContext, param: AudioParam, min: number, max: number, periodSec: number): Stopper {
    const mid = (min + max) / 2,
        depth = (max - min) / 2;
    param.value = mid;
    const osc = ctx.createOscillator();
    osc.type = "sine";
    osc.frequency.value = 1 / periodSec;
    const g = ctx.createGain();
    g.gain.value = depth;
    osc.connect(g);
    g.connect(param);
    osc.start();
    return () => {
        try {
            osc.stop();
        } catch {}
    };
}

function buildRain(ctx: AudioContext, out: GainNode): Stopper {
    const noise = makeWhiteNoise(ctx);
    const hp = ctx.createBiquadFilter();
    hp.type = "highpass";
    hp.frequency.value = 400;
    hp.Q.value = 0.5;
    const shape = ctx.createBiquadFilter();
    shape.type = "peaking";
    shape.frequency.value = 1400;
    shape.gain.value = 6;
    shape.Q.value = 1;
    const env = ctx.createGain();
    const stopLfo = lfoMod(ctx, env.gain, 0.55, 1.0, 4.5);
    noise.connect(hp);
    hp.connect(shape);
    shape.connect(env);
    env.connect(out);
    noise.start();
    return () => {
        stopLfo();
        noise.stop();
    };
}

/** Spec for one noise layer: a source routed through a filter into a gain. */
interface LayerSpec {
    filterType: BiquadFilterType;
    frequency: number;
    q?: number;
    /** Static gain value (used when `lfo` is absent). */
    gain?: number;
    /** Modulated gain as [min, max, periodSec] — takes precedence over `gain`. */
    lfo?: [min: number, max: number, periodSec: number];
}

/**
 * Wire `source → filter → gain → out`, start the source and return a stopper
 * that tears the layer (and any LFO) down. Shared by the soundscape builders.
 */
function addNoiseLayer(
    ctx: AudioContext,
    out: GainNode,
    source: AudioBufferSourceNode,
    spec: LayerSpec,
): Stopper {
    const filter = ctx.createBiquadFilter();
    filter.type = spec.filterType;
    filter.frequency.value = spec.frequency;
    if (spec.q !== undefined) {
        filter.Q.value = spec.q;
    }

    const gain = ctx.createGain();
    let stopLfo: Stopper | null = null;
    if (spec.lfo) {
        stopLfo = lfoMod(ctx, gain.gain, spec.lfo[0], spec.lfo[1], spec.lfo[2]);
    } else if (spec.gain !== undefined) {
        gain.gain.value = spec.gain;
    }

    source.connect(filter);
    filter.connect(gain);
    gain.connect(out);
    source.start();

    return () => {
        stopLfo?.();
        source.stop();
    };
}

function buildForest(ctx: AudioContext, out: GainNode): Stopper {
    const stopWind = addNoiseLayer(ctx, out, makePinkNoise(ctx), {
        filterType: "lowpass",
        frequency: 700,
        q: 0.3,
        lfo: [0.2, 0.7, 8],
    });
    const stopLeaf = addNoiseLayer(ctx, out, makeWhiteNoise(ctx), {
        filterType: "bandpass",
        frequency: 3500,
        q: 2,
        lfo: [0.04, 0.18, 3.2],
    });
    return () => {
        stopWind();
        stopLeaf();
    };
}

function buildBeach(ctx: AudioContext, out: GainNode): Stopper {
    const stopWave = addNoiseLayer(ctx, out, makeBrownNoise(ctx), {
        filterType: "lowpass",
        frequency: 550,
        q: 0.4,
        lfo: [0.08, 0.85, 7],
    });
    const stopBreeze = addNoiseLayer(ctx, out, makePinkNoise(ctx), {
        filterType: "highpass",
        frequency: 1200,
        gain: 0.15,
    });
    return () => {
        stopWave();
        stopBreeze();
    };
}

function buildCafe(ctx: AudioContext, out: GainNode): Stopper {
    const stopRumble = addNoiseLayer(ctx, out, makeBrownNoise(ctx), {
        filterType: "lowpass",
        frequency: 280,
        gain: 0.3,
    });
    const stopChat = addNoiseLayer(ctx, out, makePinkNoise(ctx), {
        filterType: "bandpass",
        frequency: 1000,
        q: 1.2,
        lfo: [0.15, 0.5, 5.5],
    });
    return () => {
        stopRumble();
        stopChat();
    };
}

export class AmbientManager {
    private _track: AmbientTrackId;
    private _volume: number;
    private _ducked = false;
    private _ctx: AudioContext | null = null;
    private _master: GainNode | null = null;
    private _stop: Stopper | null = null;

    constructor() {
        this._track = (localStorage.getItem(LS_TRACK) as AmbientTrackId) ?? "none";
        this._volume = parseFloat(localStorage.getItem(LS_VOLUME) ?? "0.4");
    }

    get track(): AmbientTrackId {
        return this._track;
    }
    get volume(): number {
        return this._volume;
    }

    setTrack(id: AmbientTrackId): void {
        this._track = id;
        localStorage.setItem(LS_TRACK, id);
        this._restart();
    }

    setVolume(v: number): void {
        this._volume = Math.max(0, Math.min(1, v));
        localStorage.setItem(LS_VOLUME, String(this._volume));
        this._applyVolume();
    }

    duck(on: boolean): void {
        this._ducked = on;
        this._applyVolume();
    }

    destroy(): void {
        this._stopCurrent();
        this._ctx?.close().catch(() => {});
        this._ctx = null;
    }

    private _ensureCtx(): { ctx: AudioContext; master: GainNode } {
        if (!this._ctx) {
            this._ctx = new AudioContext();
            this._master = this._ctx.createGain();
            this._master.connect(this._ctx.destination);
        }
        if (this._ctx.state === "suspended") {
            this._ctx.resume().catch(() => {});
        }
        return { ctx: this._ctx, master: this._master! };
    }

    private _stopCurrent(): void {
        if (this._stop) {
            try {
                this._stop();
            } catch {}
            this._stop = null;
        }
    }

    private _restart(): void {
        this._stopCurrent();
        if (this._track === "none") {
            return;
        }
        const { ctx, master } = this._ensureCtx();
        this._applyVolume();
        switch (this._track) {
            case "rain":
                this._stop = buildRain(ctx, master);
                break;
            case "forest":
                this._stop = buildForest(ctx, master);
                break;
            case "beach":
                this._stop = buildBeach(ctx, master);
                break;
            case "cafe":
                this._stop = buildCafe(ctx, master);
                break;
        }
    }

    private _applyVolume(): void {
        if (this._master) {
            this._master.gain.value = this._ducked ? DUCK_VOLUME * this._volume : this._volume;
        }
    }
}

export const ambient = new AmbientManager();
