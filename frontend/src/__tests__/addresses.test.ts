/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Addresses on the page, spelled and resolved as the server does
 * (`wactorz/core/addresses.py`).
 */
import { describe, it, expect } from "vitest";
import { addressOf, labelAmong, nodeOf, resolveAmong } from "../agents/addresses";

const onMain = { name: "counter", id: "a" };
const onPi = { name: "counter", node: "pi", id: "b" };
const weather = { name: "weather", node: "pi", id: "c" };

describe("an agent's address", () => {
    it("is name@node, with main for an agent that reports no node", () => {
        expect(addressOf(onPi)).toBe("counter@pi");
        expect(addressOf(onMain)).toBe("counter@main");
    });

    it("takes every name main goes by as main", () => {
        expect(["", "local", "LOCAL", "main", " main "].map(nodeOf)).toEqual(Array(5).fill("main"));
        expect(nodeOf("pi")).toBe("pi");
    });
});

describe("what the page calls an agent", () => {
    it("is its name while no other agent has it", () => {
        expect(labelAmong(weather, [onMain, weather])).toBe("weather");
    });

    it("is its address once another agent has the name", () => {
        expect(labelAmong(onMain, [onMain, onPi, weather])).toBe("counter@main");
        expect(labelAmong(onPi, [onMain, onPi, weather])).toBe("counter@pi");
    });
});

describe("what a name or an address finds", () => {
    const all = [onMain, onPi, weather];

    it("a name one agent has finds it", () => {
        expect(resolveAmong("weather", all)).toEqual([weather]);
    });

    it("a name two agents have finds both, in order", () => {
        expect(resolveAmong("counter", all)).toEqual([onMain, onPi]);
    });

    it("an address finds the one on its node, main by any of its names", () => {
        expect(resolveAmong("counter@pi", all)).toEqual([onPi]);
        expect(resolveAmong("counter@main", all)).toEqual([onMain]);
        expect(resolveAmong(" counter@local ", all)).toEqual([onMain]);
    });

    it("an address with no node after the separator is the bare name", () => {
        expect(resolveAmong("counter@", all)).toEqual([onMain, onPi]);
    });

    it("an address on the wrong node, or a name nobody has, finds nothing", () => {
        expect(resolveAmong("weather@main", all)).toEqual([]);
        expect(resolveAmong("ghost", all)).toEqual([]);
    });

    it("an agent whose whole name holds the separator is found by that name", () => {
        const old = { name: "counter@pi", id: "d" };

        expect(resolveAmong("counter@pi", [...all, old])).toEqual([old]);
    });
});
