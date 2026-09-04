/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Turning readings into composer text.
 *
 * The behaviour worth protecting: a reading replaces its segment rather than
 * adding to it, so a guess the recogniser withdraws leaves nothing behind.
 */
import { describe, it, expect, beforeEach } from "vitest";

import { Transcript, normalise } from "../../../ext/stt/transcript";

let transcript: Transcript;

beforeEach(() => {
    transcript = new Transcript();
    transcript.start("");
});

describe("a segment being revised", () => {
    it("shows the latest reading, not the sum of them", () => {
        transcript.hear("HELLO", 0);
        transcript.hear("HELLO THERE", 0);

        expect(transcript.text).toBe("Hello there");
    });

    it("lets a reading shrink", () => {
        transcript.hear("MMM", 0);
        transcript.hear("HI", 0);

        // Room noise, withdrawn once speech gives the decoder something better.
        // Appending would keep it and put the correction after it.
        expect(transcript.text).toBe("Hi");
    });
});

describe("several utterances in one session", () => {
    it("keeps them in order", () => {
        transcript.hear("TURN ON THE LIGHT", 0);
        transcript.hear("AND CLOSE THE BLINDS", 1);

        expect(transcript.text).toBe("Turn on the light And close the blinds");
    });

    it("revises one without disturbing the other", () => {
        transcript.hear("FIRST", 0);
        transcript.hear("SECON", 1);
        transcript.hear("SECOND", 1);

        expect(transcript.text).toBe("First Second");
    });

    it("ignores a segment that came back empty", () => {
        transcript.hear("SOMETHING", 0);
        transcript.hear("", 1);

        expect(transcript.text).toBe("Something");
    });
});

describe("text already in the composer", () => {
    it("is kept, with the speech after it", () => {
        transcript.start("note:");
        transcript.hear("BUY MILK", 0);

        expect(transcript.text).toBe("note: Buy milk");
    });

    it("is dropped when a new session starts", () => {
        transcript.start("note:");
        transcript.hear("ONE", 0);
        transcript.start("");

        expect(transcript.text).toBe("");
    });
});

describe("making a reading readable", () => {
    it("sentence-cases a shouting recogniser", () => {
        expect(normalise("HELLO THERE CAN YOU HEAR ME")).toBe("Hello there can you hear me");
    });

    it("leaves text that is already cased alone", () => {
        // The other recogniser returns case and punctuation of its own; a blanket
        // lowercase would take both away.
        expect(normalise("Hello there, can you hear me?")).toBe("Hello there, can you hear me?");
    });

    it("leaves text with no letters alone", () => {
        expect(normalise("123")).toBe("123");
        expect(normalise("")).toBe("");
    });

    it("flattens proper nouns, which is the known cost", () => {
        expect(normalise("CALL JOHN")).toBe("Call john");
    });

    it("clearing forgets the composer's own text too", () => {
        const transcript = new Transcript();
        transcript.start("a draft");
        transcript.hear("SPOKEN", 0);

        transcript.clear();

        expect(transcript.text).toBe("");
    });

    it("keeps an edit made before the words being spoken", () => {
        const transcript = new Transcript();
        transcript.start("hi");
        transcript.hear("there", 0);

        transcript.rebase("hello there");
        transcript.hear("there friend", 0);

        expect(transcript.text).toBe("hello there friend");
    });

    it("keeps an edit made after the words being spoken", () => {
        const transcript = new Transcript();
        transcript.start("");
        transcript.hear("call me", 0);

        // Typed at the end while the recogniser was still revising.
        transcript.rebase("call me later");
        transcript.hear("call me back", 0);

        expect(transcript.text).toBe("call me back later");
    });

    it("keeps edits on both sides at once", () => {
        const transcript = new Transcript();
        transcript.start("");
        transcript.hear("buy milk", 0);

        transcript.rebase("please buy milk today");
        transcript.hear("buy milk and eggs", 0);

        expect(transcript.text).toBe("please buy milk and eggs today");
    });

    it("does not bring back a reading that was deleted", () => {
        const transcript = new Transcript();
        transcript.start("");
        transcript.hear("wrong words", 0);

        transcript.rebase("never mind");
        transcript.hear("wrong words again", 0);

        // The recogniser is still revising that segment, but the person has
        // already said what they want in its place.
        expect(transcript.text).toBe("never mind");
    });

    it("still hears the next thing said after a deletion", () => {
        const transcript = new Transcript();
        transcript.start("");
        transcript.hear("wrong words", 0);
        transcript.rebase("never mind");

        transcript.hear("here we go", 1);

        expect(transcript.text).toBe("never mind here we go");
    });
});
