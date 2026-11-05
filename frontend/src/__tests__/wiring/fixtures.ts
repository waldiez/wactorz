/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * A wiring as the server sends it, with one of everything a view has to say:
 * a topic read and one nobody reads, one fed from outside and one main does
 * not sample, a field mismatch, a subscription nothing fills, a model on
 * trial, a cut payload, an agent that asks a person, and the system's own.
 */
import type { Wiring, WiringAgent, WiringTopic } from "../../types/wiring";

export function agent(id: string, over: Partial<WiringAgent> = {}): WiringAgent {
    return {
        id,
        name: id,
        node: "main",
        description: "",
        publishes: [],
        subscribes: [],
        reads_fields: [],
        models: [],
        asks: null,
        ...over,
    };
}

export function topic(name: string, over: Partial<WiringTopic> = {}): WiringTopic {
    return {
        topic: name,
        producers: [],
        consumers: [],
        outside: false,
        shadow: false,
        sampled: true,
        fields: null,
        fields_from: null,
        example: null,
        seen_at: null,
        ...over,
    };
}

export function wiring(): Wiring {
    return {
        agents: [
            agent("detect", {
                node: "edge",
                publishes: ["air/alarms"],
                subscribes: ["sensors/air/#"],
                models: [{ key: "model", name: "air-quality", active: 1, shadow: 2 }],
            }),
            agent("watch", {
                subscribes: ["air/+/score"],
                asks: { open: 1, answered: 2, channels: ["dashboard", "telegram"] },
            }),
            agent("comfort", { subscribes: ["sensors/air/#"], reads_fields: ["temperature_c"] }),
            agent("planner", {
                subscribes: ["plans/today"],
                models: [{ key: "m", name: "plan", active: null, shadow: null }],
            }),
            agent("main", { subscribes: ["homeassistant/#"] }),
        ],
        topics: [
            topic("air/alarms", { producers: ["detect"] }),
            topic("air/kitchen/score", {
                producers: ["detect"],
                consumers: ["watch"],
                fields: { score: "float" },
                fields_from: "seen",
                example: { score: 6.1 },
                seen_at: 1_700_000_000,
            }),
            topic("shadow/air/kitchen/score", { producers: ["detect"], shadow: true }),
            topic("sensors/air/kitchen", {
                consumers: ["detect", "comfort"],
                outside: true,
                fields: { temperature: "float" },
                fields_from: "seen",
                example: null,
                example_text: '{"blob": "xxxxxxxx',
                example_cut: true,
                seen_at: 1_700_000_000,
            }),
            topic("plans/today", { consumers: ["planner"] }),
            topic("homeassistant/#", { consumers: ["main"], outside: true, sampled: false }),
        ],
        sources: [
            { root: "sensors", topics: ["sensors/air/kitchen"] },
            { root: "homeassistant", topics: ["homeassistant/#"] },
        ],
        warnings: [
            { kind: "unread", topic: "air/alarms", producers: ["detect"] },
            {
                kind: "field_mismatch",
                topic: "sensors/air/kitchen",
                consumer: "comfort",
                missing: ["temperature_c"],
                carried: ["temperature"],
                carried_from: "seen",
            },
            { kind: "no_producer", topic: "plans/today", consumers: ["planner"] },
        ],
    };
}
