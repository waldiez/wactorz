/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Which character an agent gets: the ready-made one its kind names, the parts
 * its spawn asked for, or one made up from its name, so that the same agent
 * looks the same on every page and after every restart.
 *
 * What an agent says of its avatar arrives from the server and is read as
 * untrusted: a part it names is used only when it is one this folder draws,
 * and anything else falls back to what the character had.
 */
import type { AgentState, AvatarSpec } from "../../types/agent";
import type { Hat, Neckwear, Outfit } from "./body";
import type { Look } from "./draw";
import type { Antenna, Mouth, Specs } from "./head";
import { PROP_NAMES } from "./props";
import { GOLD, isColour } from "./svg";

/** What an agent's character shows of how its agent is doing. */
export type Mood = "running" | "initializing" | "late" | "stopped" | "failed";

const OUTFITS: readonly Outfit[] = ["vest", "suit", "stripes"];
const NECKWEAR: readonly Neckwear[] = ["gem", "tie", "bowtie", "none"];
const SPECS: readonly Specs[] = ["round", "square", "shades", "monocle", "none"];
const MOUTHS: readonly Mouth[] = ["smile", "blush", "beard", "mustache", "open"];
const HATS: readonly Hat[] = ["", "gradcap", "hardhat", "tophat", "captain", "beanie", "headset"];
const ANTENNAE: readonly Antenna[] = [0, 1, 2, "left", "right"];

/** The character every other one starts from. */
const BASE: Look = {
    accent: "#3fbf5f",
    outfit: "vest",
    neck: "gem",
    specs: "round",
    face: "smile",
    look: "awake",
    top: "",
    ant: 1,
    prop: "",
};

/** A ready-made character: what differs from the base, and whether agents of the kind differ among themselves. */
interface Member extends Partial<Look> {
    /** Many agents share this kind, so each also takes a colour and a few parts from its own name. */
    varied?: boolean;
}

/** The ready-made characters, by the kind an agent's class or its catalogue recipe names. */
const CAST: Record<string, Member> = {
    main: { accent: GOLD, outfit: "suit", neck: "tie", top: "captain" },
    planner: { accent: "#ff9f43", outfit: "suit", neck: "tie", ant: 0, prop: "clipboard" },
    llm: { accent: "#a66cff", neck: "bowtie", ant: 0, prop: "thought", varied: true },
    monitor: { accent: "#38c4d6", specs: "square", neck: "bowtie", ant: 0, prop: "chart" },
    "home-assistant": { accent: "#41bdf5", outfit: "suit", neck: "tie", top: "headset", prop: "house" },
    "ha-actuator": { accent: "#2bb3a3", neck: "gem", ant: 1, prop: "switch" },
    "ha-map": { accent: "#52b788", neck: "bowtie", ant: 0, prop: "pin" },
    "ha-state-bridge": { accent: "#6c8cff", specs: "square", neck: "gem", ant: 2, prop: "sync" },
    installer: { accent: "#ff8a3d", outfit: "stripes", neck: "gem", top: "hardhat", prop: "gear" },
    train: { accent: "#7b61ff", outfit: "suit", neck: "bowtie", top: "gradcap" },
    recorder: { accent: "#ef5b6b", specs: "square", neck: "bowtie", ant: 1, prop: "database" },
    rule: {
        accent: "#8a94a6",
        outfit: "suit",
        neck: "bowtie",
        specs: "monocle",
        face: "mustache",
        top: "tophat",
    },
    scheduled: { accent: "#ff6fa5", neck: "bowtie", ant: 1, prop: "clock" },
    function: { accent: "#3fbf5f", prop: "function", varied: true },
    dynamic: { prop: "code", varied: true },
    catalog: {
        accent: "#c98a4b",
        specs: "square",
        neck: "tie",
        outfit: "suit",
        ant: 0,
        prop: "magnifier",
    },
    "gmail-agent": { accent: "#ea4335", neck: "gem", ant: 1, prop: "envelope", face: "blush" },
    "google-calendar-agent": { accent: "#4285f4", neck: "bowtie", ant: 0, prop: "calendar" },
    "anomaly-detector": {
        accent: "#ff7043",
        specs: "square",
        neck: "tie",
        outfit: "suit",
        ant: 1,
        prop: "spike",
    },
    "doc-to-pptx-agent": { accent: "#d24726", neck: "bowtie", ant: 0, prop: "slides" },
    flic: { accent: "#ff5c8a", neck: "gem", ant: 2, prop: "button", face: "blush" },
    "manual-agent": { accent: "#c08a4a", neck: "bowtie", specs: "square", ant: 1, prop: "book" },
    "reachy-mini": { accent: "#7c5cff", neck: "gem", ant: 2, prop: "mic", face: "open" },
    "smart-energy": { accent: "#ffd23f", outfit: "stripes", specs: "shades", ant: 2, prop: "bolt" },
    "timeseries-collector": { accent: "#26a69a", specs: "square", neck: "gem", ant: 1, prop: "wave" },
    "weather-agent": { accent: "#5b8def", top: "beanie", face: "open", prop: "sun" },
};

/** The kinds there is a ready-made character for. */
export const KINDS: readonly string[] = Object.keys(CAST);

/** The colours a character made up from a name is given. */
const ACCENTS = ["#3fbf5f", "#ff9f43", "#38c4d6", "#a66cff", "#41bdf5", "#ef5b6b", "#ff6fa5", "#26a69a"];

/** Four numbers from a name, the same every time, that differ between like names. */
function fromName(name: string): [number, number, number, number] {
    let h = 0x811c9dc5;
    for (let i = 0; i < name.length; i++) {
        h = Math.imul(h ^ name.charCodeAt(i), 0x01000193);
    }
    // Stirred once more, so that names differing in their last letter differ in every number.
    h = Math.imul(h ^ (h >>> 16), 0x85ebca6b);
    h = Math.imul(h ^ (h >>> 13), 0xc2b2ae35);
    h ^= h >>> 16;
    return [h & 0xff, (h >>> 8) & 0xff, (h >>> 16) & 0xff, (h >>> 24) & 0xff];
}

/** What a name alone decides: a colour, the neckwear, the glasses and the antennae. */
function namedParts(name: string): Partial<Look> {
    const [a, b, c, d] = fromName(name);
    return {
        accent: ACCENTS[a % ACCENTS.length]!,
        neck: (["gem", "bowtie", "tie"] as const)[b % 3]!,
        specs: (["round", "round", "square"] as const)[c % 3]!,
        ant: ([0, 1, 2] as const)[d % 3]!,
    };
}

/** `value` when it is one of `allowed`, `fallback` when it is anything else. */
function oneOf<T>(value: unknown, allowed: readonly T[], fallback: T): T {
    return allowed.includes(value as T) ? (value as T) : fallback;
}

/** `look` with the parts `spec` names put in, each only when it is a part there is. */
function withParts(look: Look, spec: AvatarSpec): Look {
    return {
        accent: isColour(spec["accent"]) ? spec["accent"] : look.accent,
        outfit: oneOf(spec["outfit"], OUTFITS, look.outfit),
        neck: oneOf(spec["neck"], NECKWEAR, look.neck),
        specs: oneOf(spec["specs"], SPECS, look.specs),
        face: oneOf(spec["face"], MOUTHS, look.face),
        look: look.look,
        top: oneOf(spec["top"], HATS, look.top),
        ant: oneOf(spec["ant"], ANTENNAE, look.ant),
        prop: spec["prop"] === "" ? "" : oneOf(spec["prop"], PROP_NAMES, look.prop),
    };
}

/** What each mood changes of a character: its eyes, and its mouth when it has crashed. */
const MOODS: Record<Mood, Partial<Look>> = {
    running: {},
    initializing: { look: "happy" },
    late: { look: "busy" },
    stopped: { look: "asleep" },
    failed: { look: "crashed", face: "open" },
};

/** The mood an agent's state puts its character in; `late` is for one that runs but has not been heard from. */
export function moodOf(state: AgentState, late = false): Mood {
    if (typeof state === "object") {
        return "failed";
    }
    if (state === "stopped" || state === "initializing") {
        return state;
    }
    return late ? "late" : "running";
}

/**
 * The character for an agent called `name` that says `spec` of its avatar, in
 * `mood`. The eyes are the point of an agent that is stopped or has failed, so
 * dark glasses come off for those two.
 */
export function lookFor(name: string, spec: AvatarSpec | undefined, mood: Mood): Look {
    const given = spec ?? {};
    const kind = given["kind"];
    const member = typeof kind === "string" && Object.hasOwn(CAST, kind) ? CAST[kind]! : null;
    const { varied, ...parts } = member ?? {};
    const drawnFromName = member === null || varied === true;
    const look = withParts({ ...BASE, ...parts, ...(drawnFromName ? namedParts(name) : {}) }, given);
    const eyesHidden = look.specs === "shades" && (mood === "stopped" || mood === "failed");
    return { ...look, ...MOODS[mood], ...(eyesHidden ? { specs: "round" as const } : {}) };
}
