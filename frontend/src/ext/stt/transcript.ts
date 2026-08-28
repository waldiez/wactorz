/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Assembling what the recogniser hears into text for the composer.
 *
 * A reading carries the whole current text of its segment, not an addition to
 * it: a transducer revises an open segment as more audio arrives, so a guess
 * formed from room noise is withdrawn once speech gives the decoder enough to go
 * on. Text is therefore kept per segment and replaced. Appending would keep
 * every discarded guess and put the correction after it.
 */

/** Text the composer already held when listening began, and the segments since. */
export class Transcript {
    private _base = "";
    private _segments = new Map<number, string>();

    /** Begin, keeping whatever the composer already had. */
    start(existing: string): void {
        this._base = existing.trim();
        this._segments.clear();
    }

    /** Record one reading, replacing anything previously heard for its segment. */
    hear(text: string, segment: number): void {
        this._segments.set(segment, normalise(text));
    }

    /** Everything heard so far, after whatever was already written. */
    get text(): string {
        const spoken = [...this._segments.entries()]
            .sort((a, b) => a[0] - b[0])
            .map(([, text]) => text)
            .filter(Boolean)
            .join(" ");
        return [this._base, spoken].filter(Boolean).join(" ");
    }

    /** Forget everything, including what the composer started with. */
    clear(): void {
        this._base = "";
        this._segments.clear();
    }
}

/**
 * Make a reading readable, without damaging one that already is.
 *
 * A recogniser trained on capitalised corpora returns `HELLO THERE`; one that
 * punctuates returns `Hello there.`. Only the first needs help, so the shouting
 * case is detected rather than every reading being lowercased — which would take
 * the case and punctuation off the recogniser that got them right.
 *
 * Proper nouns are flattened by this, which is the price of not running a second
 * model over text a person is about to read and edit anyway.
 *
 * The shouting test only means anything in scripts that have letter case, so
 * text in a script that does not is returned untouched rather than mangled.
 */
export function normalise(text: string): string {
    if (!text || text !== text.toUpperCase() || !/[A-Z]/.test(text)) {
        return text;
    }
    const lowered = text.toLowerCase();
    return lowered.charAt(0).toUpperCase() + lowered.slice(1);
}
