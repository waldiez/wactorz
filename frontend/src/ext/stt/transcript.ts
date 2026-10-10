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

/**
 * What the composer holds: the words around the live reading, and the reading.
 *
 * The reading sits between a prefix and a suffix rather than simply after the
 * text, because a person can type on either side of it while it is still being
 * revised. Keeping the two apart is what lets an edit survive the next reading,
 * which would otherwise overwrite the whole field about once a second.
 */
export class Transcript {
    private _prefix = "";
    private _suffix = "";
    private _segments = new Map<number, string>();
    private _ignore = new Set<number>();

    /** Begin, keeping whatever the composer already had. */
    start(existing: string): void {
        this._prefix = existing.trim();
        this._suffix = "";
        this._segments.clear();
    }

    /** Record one reading, replacing anything previously heard for its segment. */
    hear(text: string, segment: number): void {
        if (this._ignore.has(segment)) {
            return;
        }
        this._segments.set(segment, normalise(text));
    }

    /** The words heard, in the order they were said. */
    get spoken(): string {
        return [...this._segments.entries()]
            .sort((a, b) => a[0] - b[0])
            .map(([, text]) => text)
            .filter(Boolean)
            .join(" ");
    }

    /** Everything heard so far, among whatever was already written. */
    get text(): string {
        return [this._prefix, this.spoken, this._suffix].filter(Boolean).join(" ");
    }

    /**
     * Take what the composer now holds as the truth, without losing the reading.
     *
     * The reading is found in that text and the words on either side of it
     * become the new surroundings, so an edit anywhere outside it survives every
     * revision that follows. An edit that leaves no trace of the reading is
     * taken at its word: those segments are dropped and will not come back, even
     * though the recogniser is still revising them.
     */
    rebase(current: string): void {
        const spoken = this.spoken;
        const at = spoken ? current.lastIndexOf(spoken) : -1;
        if (at < 0) {
            this._segments.forEach((_, segment) => this._ignore.add(segment));
            this._segments.clear();
            this._prefix = current.trim();
            this._suffix = "";
            return;
        }
        this._prefix = current.slice(0, at).trim();
        this._suffix = current.slice(at + spoken.length).trim();
    }

    /** Forget everything, including what the composer started with. */
    clear(): void {
        this._prefix = "";
        this._suffix = "";
        this._segments.clear();
        this._ignore.clear();
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
