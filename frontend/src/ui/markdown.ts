/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Minimal, dependency-free Markdown renderer for chat bubbles.
 *
 * Renders a deliberately small subset — the formatting LLM replies actually
 * use — and builds the result as real DOM nodes (createElement + textContent),
 * never via innerHTML. Model output therefore cannot inject HTML/script, so no
 * sanitiser dependency is required.
 *
 * Supported:
 *   - fenced code blocks  ```lang … ```
 *   - ATX headings        # … ###### (level clamped to 3 — chat-sized)
 *   - unordered lists     -, *, +   (nested by indentation)
 *   - ordered lists       1.  2.  … (nested by indentation)
 *   - blockquotes         > …
 *   - horizontal rules    ---  ***  ___
 *   - tables              | a | b |  with a | --- | :-: | delimiter row
 *   - paragraphs (single newlines become <br>)
 *   - inline: **bold** *italic* ~~strikethrough~~ `code` [text](url), bare URLs
 *
 * Intentionally NOT supported: raw HTML, images, reference links. Such input
 * degrades to readable plain text.
 */

const URL_SAFE = /^(https?:|mailto:)/i;

/**
 * A block handler inspects `lines[i]`; if it owns that block it appends nodes
 * to `frag` and returns the index of the first unconsumed line, otherwise it
 * returns `null` so the next handler can try.
 */
type BlockHandler = (lines: string[], i: number, frag: DocumentFragment) => number | null;

function handleFence(lines: string[], i: number, frag: DocumentFragment): number | null {
    if (!/^\s*```/.test(lines[i] ?? "")) {
        return null;
    }
    const body: string[] = [];
    let j = i + 1;
    while (j < lines.length && !/^\s*```/.test(lines[j] ?? "")) {
        body.push(lines[j] ?? "");
        j++;
    }
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    code.textContent = body.join("\n");
    pre.appendChild(code);
    frag.appendChild(pre);
    return j + 1; // consume closing fence (if present)
}

function handleBlank(lines: string[], i: number): number | null {
    return (lines[i] ?? "").trim() === "" ? i + 1 : null;
}

function handleHr(lines: string[], i: number, frag: DocumentFragment): number | null {
    if (!/^\s*([-*_])(\s*\1){2,}\s*$/.test(lines[i] ?? "")) {
        return null;
    }
    frag.appendChild(document.createElement("hr"));
    return i + 1;
}

function handleHeading(lines: string[], i: number, frag: DocumentFragment): number | null {
    const m = /^\s*(#{1,6})\s+(.*)$/.exec(lines[i] ?? "");
    if (!m) {
        return null;
    }
    const level = Math.min(m[1]!.length, 3);
    const h = document.createElement(`h${level}`);
    appendInline(h, m[2]!.trim());
    frag.appendChild(h);
    return i + 1;
}

function handleTable(lines: string[], i: number, frag: DocumentFragment): number | null {
    if (!startsTable(lines, i)) {
        return null;
    }
    const aligns = parseAligns(lines[i + 1] ?? "");
    const table = document.createElement("table");
    const thead = document.createElement("thead");
    thead.appendChild(buildRow(parseRow(lines[i] ?? ""), aligns, "th"));
    table.appendChild(thead);

    let j = i + 2; // consume header + delimiter
    const tbody = document.createElement("tbody");
    while (j < lines.length && (lines[j] ?? "").includes("|") && (lines[j] ?? "").trim() !== "") {
        tbody.appendChild(buildRow(parseRow(lines[j] ?? ""), aligns, "td"));
        j++;
    }
    if (tbody.childNodes.length) {
        table.appendChild(tbody);
    }
    frag.appendChild(table);
    return j;
}

function handleList(lines: string[], i: number, frag: DocumentFragment): number | null {
    if (!isListItem(lines[i] ?? "")) {
        return null;
    }
    const { node, next } = parseList(lines, i);
    frag.appendChild(node);
    return next;
}

function handleBlockquote(lines: string[], i: number, frag: DocumentFragment): number | null {
    if (!/^\s*>\s?/.test(lines[i] ?? "")) {
        return null;
    }
    const quote = document.createElement("blockquote");
    const parts: string[] = [];
    let j = i;
    while (j < lines.length && /^\s*>\s?/.test(lines[j] ?? "")) {
        parts.push((lines[j] ?? "").replace(/^\s*>\s?/, ""));
        j++;
    }
    appendInlineMultiline(quote, parts);
    frag.appendChild(quote);
    return j;
}

/** True when `lines[j]` opens a non-paragraph block — i.e. ends a paragraph. */
function isBlockStart(lines: string[], j: number): boolean {
    const line = lines[j] ?? "";
    return (
        /^\s*```/.test(line) ||
        /^\s*(#{1,6})\s+/.test(line) ||
        isListItem(line) ||
        /^\s*>\s?/.test(line) ||
        /^\s*([-*_])(\s*\1){2,}\s*$/.test(line) ||
        startsTable(lines, j)
    );
}

function handleParagraph(lines: string[], i: number, frag: DocumentFragment): number {
    const para: string[] = [];
    let j = i;
    while (j < lines.length && (lines[j] ?? "").trim() !== "" && !isBlockStart(lines, j)) {
        para.push(lines[j] ?? "");
        j++;
    }
    const p = document.createElement("p");
    appendInlineMultiline(p, para);
    frag.appendChild(p);
    return j;
}

// Order matters: more specific block types before the paragraph fallback.
const BLOCK_HANDLERS: BlockHandler[] = [
    handleFence,
    handleBlank,
    handleHr,
    handleHeading,
    handleTable,
    handleList,
    handleBlockquote,
];

/** Render Markdown source into a DocumentFragment of styled DOM nodes. */
export function renderMarkdown(src: string): DocumentFragment {
    const frag = document.createDocumentFragment();
    const lines = src.replace(/\r\n?/g, "\n").split("\n");

    let i = 0;
    while (i < lines.length) {
        let next: number | null = null;
        for (const handle of BLOCK_HANDLERS) {
            next = handle(lines, i, frag);
            if (next !== null) {
                break;
            }
        }
        i = next ?? handleParagraph(lines, i, frag);
    }

    return frag;
}

function isListItem(line: string): boolean {
    return /^\s*(?:[-*+]|\d+\.)\s+/.test(line);
}

/** Leading-whitespace width of a line (tabs count as two columns). */
function indentOf(line: string): number {
    return (/^[ \t]*/.exec(line)?.[0] ?? "").replace(/\t/g, "  ").length;
}

function isOrdered(line: string): boolean {
    return /^\s*\d+\.\s+/.test(line);
}

interface ListFrame {
    indent: number;
    list: HTMLElement;
    lastLi: HTMLLIElement | null;
}

/** Adjust the open-list stack for `line`'s indentation and return the frame
 *  that its <li> belongs to (opening a nested list on indent). */
function adjustListDepth(stack: ListFrame[], line: string): ListFrame {
    const indent = indentOf(line);

    // Dedent: close lists deeper than the current indentation.
    while (stack.length > 1 && indent < stack[stack.length - 1]!.indent) {
        stack.pop();
    }
    let top = stack[stack.length - 1]!;

    // Indent: open a child list inside the previous item.
    if (indent > top.indent) {
        const nested = document.createElement(isOrdered(line) ? "ol" : "ul");
        (top.lastLi ?? top.list).appendChild(nested);
        top = { indent, list: nested, lastLi: null };
        stack.push(top);
    }
    return top;
}

/** Parse a run of list lines into a (possibly nested) <ul>/<ol> tree.
 *  Nesting is driven by indentation; deeper-indented items become a child
 *  list inside the preceding item. Returns the root list and the index of the
 *  first line after the list block. */
function parseList(lines: string[], start: number): { node: HTMLElement; next: number } {
    let i = start;
    const rootList = document.createElement(isOrdered(lines[i] ?? "") ? "ol" : "ul");
    const stack: ListFrame[] = [{ indent: indentOf(lines[i] ?? ""), list: rootList, lastLi: null }];

    while (i < lines.length && isListItem(lines[i] ?? "")) {
        const line = lines[i] ?? "";
        const top = adjustListDepth(stack, line);
        const li = document.createElement("li");
        appendInline(li, line.replace(/^\s*(?:[-*+]|\d+\.)\s+/, ""));
        top.list.appendChild(li);
        top.lastLi = li;
        i++;
    }
    return { node: rootList, next: i };
}

type Align = "left" | "center" | "right" | "";

/** A table starts where a row of cells is followed by a |---| delimiter row. */
function startsTable(lines: string[], idx: number): boolean {
    return (lines[idx] ?? "").includes("|") && isDelimiterRow(lines[idx + 1] ?? "");
}

/** True for the `| --- | :-: | --: |` separator line under a table header. */
function isDelimiterRow(line: string): boolean {
    const t = line.trim();
    if (!t.includes("|") || !t.includes("-")) {
        return false;
    }
    return stripOuterPipes(t)
        .split("|")
        .every(c => /^\s*:?-+:?\s*$/.test(c));
}

function stripOuterPipes(s: string): string {
    let t = s;
    if (t.startsWith("|")) {
        t = t.slice(1);
    }
    if (t.endsWith("|")) {
        t = t.slice(0, -1);
    }
    return t;
}

function parseRow(line: string): string[] {
    return stripOuterPipes(line.trim())
        .split("|")
        .map(c => c.trim());
}

function parseAligns(delim: string): Align[] {
    return parseRow(delim).map(c => {
        const left = c.startsWith(":");
        const right = c.endsWith(":");
        if (left && right) {
            return "center";
        }
        if (right) {
            return "right";
        }
        if (left) {
            return "left";
        }
        return "";
    });
}

function buildRow(cells: string[], aligns: Align[], tag: "th" | "td"): HTMLTableRowElement {
    const tr = document.createElement("tr");
    cells.forEach((cell, idx) => {
        const el = document.createElement(tag);
        const align = aligns[idx];
        if (align) {
            el.style.textAlign = align;
        }
        appendInline(el, cell);
        tr.appendChild(el);
    });
    return tr;
}

/** Append several source lines into `el`, separating them with <br>. */
function appendInlineMultiline(el: HTMLElement, srcLines: string[]): void {
    srcLines.forEach((l, idx) => {
        if (idx > 0) {
            el.appendChild(document.createElement("br"));
        }
        appendInline(el, l);
    });
}

interface InlineRule {
    re: RegExp;
    build: (m: RegExpExecArray) => Node;
}

// Order matters: code spans first so their contents stay literal, then links,
// then bold (greedy ** / __) before italic (* / _).
const INLINE_RULES: InlineRule[] = [
    {
        re: /`([^`]+)`/,
        build: m => {
            const code = document.createElement("code");
            code.textContent = m[1]!;
            return code;
        },
    },
    {
        re: /\[([^\]]+)\]\(([^)\s]+)\)/,
        build: m => {
            const text = m[1]!;
            const href = m[2]!;
            if (!URL_SAFE.test(href)) {
                return document.createTextNode(m[0]!);
            }
            const a = document.createElement("a");
            a.href = href;
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            appendInline(a, text);
            return a;
        },
    },
    {
        // Bare URL autolink. Trailing sentence punctuation is pushed back out as
        // text so "see https://x.com." doesn't swallow the full stop into the link.
        re: /\bhttps?:\/\/[^\s<]+/,
        build: m => {
            let url = m[0]!;
            const trail = /[.,;:!?)\]}'"]+$/.exec(url)?.[0] ?? "";
            if (trail) {
                url = url.slice(0, url.length - trail.length);
            }
            const a = document.createElement("a");
            a.href = url;
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            a.textContent = url;
            if (!trail) {
                return a;
            }
            const frag = document.createDocumentFragment();
            frag.append(a, document.createTextNode(trail));
            return frag;
        },
    },
    {
        re: /\*\*([^*]+)\*\*|__([^_]+)__/,
        build: m => {
            const strong = document.createElement("strong");
            appendInline(strong, m[1] ?? m[2] ?? "");
            return strong;
        },
    },
    {
        re: /~~([^~]+)~~/,
        build: m => {
            const del = document.createElement("del");
            appendInline(del, m[1]!);
            return del;
        },
    },
    {
        re: /\*([^*]+)\*|(?<![A-Za-z0-9_])_([^_]+)_(?![A-Za-z0-9_])/,
        build: m => {
            const em = document.createElement("em");
            appendInline(em, m[1] ?? m[2] ?? "");
            return em;
        },
    },
];

/** Parse inline markdown in `text` and append the resulting nodes to `el`. */
function appendInline(el: HTMLElement, text: string): void {
    let rest = text;
    // Guard against pathological inputs causing an unbounded loop.
    let guard = 0;
    while (rest && guard++ < 10000) {
        let best: { idx: number; rule: InlineRule; m: RegExpExecArray } | null = null;
        for (const rule of INLINE_RULES) {
            const m = rule.re.exec(rest);
            if (m && (best === null || m.index < best.idx)) {
                best = { idx: m.index, rule, m };
            }
        }
        if (!best) {
            el.appendChild(document.createTextNode(rest));
            return;
        }
        if (best.idx > 0) {
            el.appendChild(document.createTextNode(rest.slice(0, best.idx)));
        }
        el.appendChild(best.rule.build(best.m));
        rest = rest.slice(best.idx + best.m[0]!.length);
    }
    if (rest) {
        el.appendChild(document.createTextNode(rest));
    }
}
