/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The orchestrator's action blocks, kept out of what the chat shows.
 *
 * Main's model acts by writing `<spawn>`, `<delete>` and `<delegate>` blocks
 * into its reply; main runs them and the server leaves them out of what it
 * sends. They are removed here as well, so a reply recorded before the server
 * did so, or reaching the page by another route, still reads as text for the
 * user. Mirrors the backend's `ACTION_TAGS` in `wactorz/agents/main/blocks.py`.
 */

const COMPLETE_BLOCK = /<(spawn|delete|delegate)>[\s\S]*?<\/\1>/g;

/** An opening tag with no closing one after it, up to the end of the text. */
const UNCLOSED_BLOCK = /<(spawn|delete|delegate)>[\s\S]*$/;

/** The start of an opening tag cut off at the end of the text. */
const PARTIAL_OPENING = /<(s(p(a(wn?)?)?)?|d(e(l(e(g(a(te?)?)?|te?)?)?)?)?)?$/;

/**
 * Remove every complete action block from `text`.
 *
 * With `streaming`, the text is a reply still arriving: an opening tag whose
 * block has not closed yet, or the first characters of one, are hidden too, so
 * a block never shows half-written. A finished reply keeps an unclosed block,
 * as the server does, so a reply cut short stays visible.
 */
export function stripActionBlocks(text: string, { streaming = false }: { streaming?: boolean } = {}): string {
    const stripped = text.replace(COMPLETE_BLOCK, "");
    if (!streaming) {
        return stripped;
    }
    return stripped.replace(UNCLOSED_BLOCK, "").replace(PARTIAL_OPENING, "");
}
