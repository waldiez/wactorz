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

/** Render Markdown source into a DocumentFragment of styled DOM nodes. */
export function renderMarkdown(src: string): DocumentFragment {
  const frag = document.createDocumentFragment();
  const lines = src.replace(/\r\n?/g, "\n").split("\n");

  let i = 0;
  while (i < lines.length) {
    const line = lines[i] ?? "";

    // ── Fenced code block ──────────────────────────────────────────────────
    const fence = /^\s*```/.exec(line);
    if (fence) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i] ?? "")) {
        body.push(lines[i] ?? "");
        i++;
      }
      i++; // consume closing fence (if present)
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      code.textContent = body.join("\n");
      pre.appendChild(code);
      frag.appendChild(pre);
      continue;
    }

    // ── Blank line ─────────────────────────────────────────────────────────
    if (line.trim() === "") {
      i++;
      continue;
    }

    // ── Horizontal rule ────────────────────────────────────────────────────
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      frag.appendChild(document.createElement("hr"));
      i++;
      continue;
    }

    // ── Heading ────────────────────────────────────────────────────────────
    const heading = /^\s*(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      const level = Math.min(heading[1]!.length, 3);
      const h = document.createElement(`h${level}`);
      appendInline(h, heading[2]!.trim());
      frag.appendChild(h);
      i++;
      continue;
    }

    // ── Table (GitHub-flavoured: header row + |---| delimiter) ──────────────
    if (startsTable(lines, i)) {
      const aligns = parseAligns(lines[i + 1] ?? "");
      const table = document.createElement("table");
      const thead = document.createElement("thead");
      thead.appendChild(buildRow(parseRow(line), aligns, "th"));
      table.appendChild(thead);
      i += 2; // consume header + delimiter
      const tbody = document.createElement("tbody");
      while (
        i < lines.length &&
        (lines[i] ?? "").includes("|") &&
        (lines[i] ?? "").trim() !== ""
      ) {
        tbody.appendChild(buildRow(parseRow(lines[i] ?? ""), aligns, "td"));
        i++;
      }
      if (tbody.childNodes.length) table.appendChild(tbody);
      frag.appendChild(table);
      continue;
    }

    // ── List (consecutive items, nested by indentation) ────────────────────
    if (isListItem(line)) {
      const { node, next } = parseList(lines, i);
      frag.appendChild(node);
      i = next;
      continue;
    }

    // ── Blockquote (consecutive > lines) ───────────────────────────────────
    if (/^\s*>\s?/.test(line)) {
      const quote = document.createElement("blockquote");
      const parts: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i] ?? "")) {
        parts.push((lines[i] ?? "").replace(/^\s*>\s?/, ""));
        i++;
      }
      appendInlineMultiline(quote, parts);
      frag.appendChild(quote);
      continue;
    }

    // ── Paragraph (consecutive plain lines, <br>-joined) ───────────────────
    const para: string[] = [];
    while (
      i < lines.length &&
      (lines[i] ?? "").trim() !== "" &&
      !/^\s*```/.test(lines[i] ?? "") &&
      !/^\s*(#{1,6})\s+/.test(lines[i] ?? "") &&
      !isListItem(lines[i] ?? "") &&
      !/^\s*>\s?/.test(lines[i] ?? "") &&
      !/^\s*([-*_])(\s*\1){2,}\s*$/.test(lines[i] ?? "") &&
      !startsTable(lines, i)
    ) {
      para.push(lines[i] ?? "");
      i++;
    }
    const p = document.createElement("p");
    appendInlineMultiline(p, para);
    frag.appendChild(p);
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

/** Parse a run of list lines into a (possibly nested) <ul>/<ol> tree.
 *  Nesting is driven by indentation; deeper-indented items become a child
 *  list inside the preceding item. Returns the root list and the index of the
 *  first line after the list block. */
function parseList(lines: string[], start: number): { node: HTMLElement; next: number } {
  interface Frame {
    indent: number;
    list: HTMLElement;
    lastLi: HTMLLIElement | null;
  }
  let i = start;
  const rootList = document.createElement(isOrdered(lines[i] ?? "") ? "ol" : "ul");
  const stack: Frame[] = [{ indent: indentOf(lines[i] ?? ""), list: rootList, lastLi: null }];

  while (i < lines.length && isListItem(lines[i] ?? "")) {
    const line = lines[i] ?? "";
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

    const li = document.createElement("li");
    appendInline(li, line.replace(/^\s*(?:[-*+]|\d+\.)\s+/, ""));
    top.list.appendChild(li);
    top.lastLi = li;
    i++;
  }
  return { node: rootList, next: i };
}

// ── Tables ────────────────────────────────────────────────────────────────────

type Align = "left" | "center" | "right" | "";

/** A table starts where a row of cells is followed by a |---| delimiter row. */
function startsTable(lines: string[], idx: number): boolean {
  return (lines[idx] ?? "").includes("|") && isDelimiterRow(lines[idx + 1] ?? "");
}

/** True for the `| --- | :-: | --: |` separator line under a table header. */
function isDelimiterRow(line: string): boolean {
  const t = line.trim();
  if (!t.includes("|") || !t.includes("-")) return false;
  return stripOuterPipes(t)
    .split("|")
    .every((c) => /^\s*:?-+:?\s*$/.test(c));
}

function stripOuterPipes(s: string): string {
  let t = s;
  if (t.startsWith("|")) t = t.slice(1);
  if (t.endsWith("|")) t = t.slice(0, -1);
  return t;
}

function parseRow(line: string): string[] {
  return stripOuterPipes(line.trim()).split("|").map((c) => c.trim());
}

function parseAligns(delim: string): Align[] {
  return parseRow(delim).map((c) => {
    const left = c.startsWith(":");
    const right = c.endsWith(":");
    if (left && right) return "center";
    if (right) return "right";
    if (left) return "left";
    return "";
  });
}

function buildRow(cells: string[], aligns: Align[], tag: "th" | "td"): HTMLTableRowElement {
  const tr = document.createElement("tr");
  cells.forEach((cell, idx) => {
    const el = document.createElement(tag);
    const align = aligns[idx];
    if (align) el.style.textAlign = align;
    appendInline(el, cell);
    tr.appendChild(el);
  });
  return tr;
}

/** Append several source lines into `el`, separating them with <br>. */
function appendInlineMultiline(el: HTMLElement, srcLines: string[]): void {
  srcLines.forEach((l, idx) => {
    if (idx > 0) el.appendChild(document.createElement("br"));
    appendInline(el, l);
  });
}

// ── Inline parsing ────────────────────────────────────────────────────────────

interface InlineRule {
  re: RegExp;
  build: (m: RegExpExecArray) => Node;
}

// Order matters: code spans first so their contents stay literal, then links,
// then bold (greedy ** / __) before italic (* / _).
const INLINE_RULES: InlineRule[] = [
  {
    re: /`([^`]+)`/,
    build: (m) => {
      const code = document.createElement("code");
      code.textContent = m[1]!;
      return code;
    },
  },
  {
    re: /\[([^\]]+)\]\(([^)\s]+)\)/,
    build: (m) => {
      const text = m[1]!;
      const href = m[2]!;
      if (!URL_SAFE.test(href)) return document.createTextNode(m[0]!);
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
    build: (m) => {
      let url = m[0]!;
      const trail = /[.,;:!?)\]}'"]+$/.exec(url)?.[0] ?? "";
      if (trail) url = url.slice(0, url.length - trail.length);
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = url;
      if (!trail) return a;
      const frag = document.createDocumentFragment();
      frag.append(a, document.createTextNode(trail));
      return frag;
    },
  },
  {
    re: /\*\*([^*]+)\*\*|__([^_]+)__/,
    build: (m) => {
      const strong = document.createElement("strong");
      appendInline(strong, m[1] ?? m[2] ?? "");
      return strong;
    },
  },
  {
    re: /~~([^~]+)~~/,
    build: (m) => {
      const del = document.createElement("del");
      appendInline(del, m[1]!);
      return del;
    },
  },
  {
    re: /\*([^*]+)\*|(?<![A-Za-z0-9_])_([^_]+)_(?![A-Za-z0-9_])/,
    build: (m) => {
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
  if (rest) el.appendChild(document.createTextNode(rest));
}
