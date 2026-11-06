/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The wiring as `GET /api/wiring` says it: who publishes what, who reads it,
 * where the data comes from outside, and what looks wrong. Mirrors
 * `wactorz/core/wiring.py`; agents are named by id throughout.
 */

/** A model an agent serves, and where its pointers stand. */
export interface WiringModel {
    /** The key the agent loads it under. */
    key: string;
    /** The model's name in the store. */
    name: string;
    /** The version `active` points at; null when unset. */
    active: number | null;
    /** The version on trial; null when none is. */
    shadow: number | null;
}

/** What an agent has asked a person. */
export interface WiringAsks {
    /** Questions still waiting for an answer. */
    open: number;
    /** Questions answered, as many as its book keeps. */
    answered: number;
    /** Where its questions went. */
    channels: string[];
}

/** One agent, as the registry holds it. */
export interface WiringAgent {
    /** Its actor id, or its name while its id is not known. */
    id: string;
    /** Its name. */
    name: string;
    /** The host it runs on: `main` or a node's name. */
    node: string;
    /** What it says it does. */
    description: string;
    /** The topics it declares it publishes. */
    publishes: string[];
    /** The topic filters it subscribes to. */
    subscribes: string[];
    /** The fields it declares it reads. */
    reads_fields: string[];
    /** The models it serves. */
    models: WiringModel[];
    /** What it has asked a person; null when nothing. */
    asks: WiringAsks | null;
}

/** One topic: a published one, one fed from outside, or a subscription nothing fills. */
export interface WiringTopic {
    /** The topic, or the filter for a subscription nothing fills. */
    topic: string;
    /** The ids of the agents that publish it. */
    producers: string[];
    /** The ids of the agents that read it. */
    consumers: string[];
    /** Whether it comes from outside the system: no agent publishes it, under a world root. */
    outside: boolean;
    /** Whether it is what a model on trial publishes. */
    shadow: boolean;
    /** Whether main keeps payloads from where it is, so its fields can be known. */
    sampled: boolean;
    /** The fields it carries, by name, with their types; null when nothing is known. */
    fields: Record<string, string> | null;
    /** Where `fields` come from: a payload seen, or its producer's declaration. */
    fields_from: "seen" | "declared" | null;
    /** The last payload seen on it; null when none was, or it was too long to send whole. */
    example: unknown;
    /** The last payload as JSON text, cut, when it was too long. */
    example_text?: string;
    /** Set when `example_text` was cut. */
    example_cut?: boolean;
    /** When the last payload came, in seconds since the epoch; null when none was seen. */
    seen_at: number | null;
}

/** Topics no agent publishes, under one root, grouped as one source. */
export interface WiringSource {
    /** The root: `sensors`, `homeassistant`... */
    root: string;
    /** Its topics. */
    topics: string[];
}

/** A topic an agent publishes that no agent reads. */
export interface UnreadWarning {
    kind: "unread";
    topic: string;
    producers: string[];
}

/** A subscription that nothing publishes or was seen on, outside the world's roots. */
export interface NoProducerWarning {
    kind: "no_producer";
    topic: string;
    consumers: string[];
}

/** A field an agent declares it reads that its topic does not carry. */
export interface FieldMismatchWarning {
    kind: "field_mismatch";
    topic: string;
    consumer: string;
    missing: string[];
    carried: string[];
    carried_from: "seen" | "declared" | null;
}

/** What looks wrong in the wiring. */
export type WiringWarning = UnreadWarning | NoProducerWarning | FieldMismatchWarning;

/** The whole answer of `GET /api/wiring`. */
export interface Wiring {
    agents: WiringAgent[];
    topics: WiringTopic[];
    sources: WiringSource[];
    warnings: WiringWarning[];
}
