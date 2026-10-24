/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Main's action blocks are instructions for main, never text for the user,
 * whether a reply is still arriving or already whole.
 */
import { describe, it, expect } from "vitest";

import { stripActionBlocks } from "../agents/actionBlocks";

describe("stripActionBlocks on a finished reply", () => {
    it.each([
        ['Starting it.\n<spawn>\n{"name": "a"}\n</spawn>\n\nDone.', "Starting it.\n\n\nDone."],
        ['<delete>{"name": "a"}</delete>', ""],
        ['Asking. <delegate>{"agent": "w"}</delegate> Wait.', "Asking.  Wait."],
        ['one <spawn>{"a": 1}</spawn> two <delete>{"b": 2}</delete> three', "one  two  three"],
    ])("removes every block from %j", (text, shown) => {
        expect(stripActionBlocks(text)).toBe(shown);
    });

    it("leaves prose with angle brackets alone", () => {
        expect(stripActionBlocks("a < b and c <spa d </spawn> e")).toBe("a < b and c <spa d </spawn> e");
    });

    it("keeps a block the reply never closed, so a reply cut short stays visible", () => {
        expect(stripActionBlocks('cut short <spawn>{"name": "a"')).toBe('cut short <spawn>{"name": "a"');
    });

    it("does not let one tag close another", () => {
        expect(stripActionBlocks("a <spawn>x</delete> b")).toBe("a <spawn>x</delete> b");
    });
});

describe("stripActionBlocks on a reply still arriving", () => {
    it("hides a block that has opened and not closed yet", () => {
        expect(stripActionBlocks('Starting it.\n<spawn>\n{"na', { streaming: true })).toBe("Starting it.\n");
    });

    it.each(["<", "<s", "<spaw", "<spawn", "<d", "<dele", "<delet", "<deleg", "<delegate"])(
        "hides the start of an opening tag cut off as %j",
        tail => {
            expect(stripActionBlocks(`ok ${tail}`, { streaming: true })).toBe("ok ");
        },
    );

    it("shows prose whose angle bracket cannot start a block", () => {
        expect(stripActionBlocks("a <b", { streaming: true })).toBe("a <b");
    });

    it("shows nothing of a block at any point while it arrives", () => {
        const reply = 'ok <delegate>{"agent": "w"}</delegate> fine';
        for (let cut = 0; cut <= reply.length; cut++) {
            const shown = stripActionBlocks(reply.slice(0, cut), { streaming: true });
            expect("ok  fine".startsWith(shown), `${cut}: ${shown}`).toBe(true);
        }
    });
});
