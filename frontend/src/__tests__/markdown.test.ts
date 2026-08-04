/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { renderMarkdown } from "../ui/markdown";

/** Render `src` into a detached div and return it for querying. */
function render(src: string): HTMLDivElement {
    const div = document.createElement("div");
    div.appendChild(renderMarkdown(src));
    return div;
}

describe("renderMarkdown", () => {
    it("renders a plain line as a single paragraph", () => {
        const el = render("Hello there");
        expect(el.querySelectorAll("p")).toHaveLength(1);
        expect(el.textContent).toBe("Hello there");
    });

    it("renders bold and italic inline", () => {
        const el = render("a **bold** and *italic* word");
        expect(el.querySelector("strong")?.textContent).toBe("bold");
        expect(el.querySelector("em")?.textContent).toBe("italic");
    });

    it("renders strikethrough", () => {
        const el = render("this is ~~gone~~ now");
        expect(el.querySelector("del")?.textContent).toBe("gone");
    });

    it("renders inline code without interpreting markup inside it", () => {
        const el = render("call `foo **bar**` now");
        const code = el.querySelector("code");
        expect(code?.textContent).toBe("foo **bar**");
        expect(el.querySelector("strong")).toBeNull();
    });

    it("renders a fenced code block verbatim", () => {
        const el = render("```js\nconst x = 1;\n**not bold**\n```");
        const pre = el.querySelector("pre code");
        expect(pre?.textContent).toBe("const x = 1;\n**not bold**");
        expect(el.querySelector("strong")).toBeNull();
    });

    it("clamps heading levels to h3", () => {
        const el = render("# One\n\n###### Six");
        expect(el.querySelector("h1")?.textContent).toBe("One");
        expect(el.querySelector("h3")?.textContent).toBe("Six");
        expect(el.querySelector("h6")).toBeNull();
    });

    it("renders unordered and ordered lists", () => {
        const ul = render("- a\n- b");
        expect(ul.querySelector("ul")).not.toBeNull();
        expect(ul.querySelectorAll("li")).toHaveLength(2);

        const ol = render("1. first\n2. second");
        expect(ol.querySelector("ol")).not.toBeNull();
        expect(ol.querySelectorAll("li")).toHaveLength(2);
    });

    it("nests list items by indentation", () => {
        const el = render("- a\n  - b\n  - c\n- d");
        const root = el.querySelector("ul")!;
        const directLis = [...root.children].filter(n => n.tagName === "LI");
        expect(directLis).toHaveLength(2); // a (with nested) and d
        expect(directLis[0]!.firstChild?.textContent).toBe("a");
        const nested = directLis[0]!.querySelector("ul")!;
        expect([...nested.children].filter(n => n.tagName === "LI")).toHaveLength(2);
    });

    it("returns to the parent level after a nested block (dedent)", () => {
        const el = render("- a\n  - b\n- c");
        const root = el.querySelector("ul")!;
        const directLis = [...root.children].filter(n => n.tagName === "LI");
        expect(directLis).toHaveLength(2);
        expect(directLis[1]!.textContent).toBe("c");
    });

    it("supports an ordered list nested under an unordered one", () => {
        const el = render("- top\n  1. one\n  2. two");
        const nestedOl = el.querySelector("ul li > ol");
        expect(nestedOl).not.toBeNull();
        expect([...nestedOl!.children].filter(n => n.tagName === "LI")).toHaveLength(2);
    });

    it("renders safe links and drops unsafe schemes", () => {
        const ok = render("[site](https://example.com)");
        const a = ok.querySelector("a");
        expect(a?.getAttribute("href")).toBe("https://example.com");
        expect(a?.getAttribute("target")).toBe("_blank");
        expect(a?.getAttribute("rel")).toBe("noopener noreferrer");

        const bad = render("[x](javascript:alert(1))");
        expect(bad.querySelector("a")).toBeNull();
        expect(bad.textContent).toContain("[x](javascript:alert(1))");
    });

    it("autolinks bare URLs and excludes trailing punctuation", () => {
        const el = render("see https://example.com/path, ok?");
        const a = el.querySelector("a");
        expect(a?.getAttribute("href")).toBe("https://example.com/path");
        expect(a?.textContent).toBe("https://example.com/path");
        expect(a?.getAttribute("rel")).toBe("noopener noreferrer");
        expect(el.textContent).toContain("https://example.com/path, ok?");
    });

    it("does not double-link a URL already inside [text](url)", () => {
        const el = render("[home](https://example.com)");
        expect(el.querySelectorAll("a")).toHaveLength(1);
        expect(el.querySelector("a")?.textContent).toBe("home");
    });

    it("never emits HTML from raw angle brackets (XSS-safe)", () => {
        const el = render("<img src=x onerror=alert(1)> and <script>bad()</script>");
        expect(el.querySelector("img")).toBeNull();
        expect(el.querySelector("script")).toBeNull();
        expect(el.textContent).toContain("<img src=x onerror=alert(1)>");
    });

    it("renders blockquotes and horizontal rules", () => {
        const el = render("> quoted\n\n---");
        expect(el.querySelector("blockquote")?.textContent).toBe("quoted");
        expect(el.querySelector("hr")).not.toBeNull();
    });

    it("joins consecutive paragraph lines with <br>", () => {
        const el = render("line one\nline two");
        const p = el.querySelector("p");
        expect(p?.querySelectorAll("br")).toHaveLength(1);
    });

    it("renders a GitHub-flavoured table", () => {
        const el = render("| Name | Age |\n| --- | --- |\n| Ann | 30 |\n| Bob | 25 |");
        const table = el.querySelector("table");
        expect(table).not.toBeNull();
        expect(el.querySelectorAll("thead th")).toHaveLength(2);
        expect(el.querySelector("thead th")?.textContent).toBe("Name");
        expect(el.querySelectorAll("tbody tr")).toHaveLength(2);
        expect(el.querySelectorAll("tbody tr")[1]?.querySelectorAll("td")[0]?.textContent).toBe("Bob");
    });

    it("applies column alignment from the delimiter row", () => {
        const el = render("| L | C | R |\n| :-- | :-: | --: |\n| a | b | c |");
        const th = el.querySelectorAll("thead th");
        expect((th[0] as HTMLElement).style.textAlign).toBe("left");
        expect((th[1] as HTMLElement).style.textAlign).toBe("center");
        expect((th[2] as HTMLElement).style.textAlign).toBe("right");
    });

    it("parses inline markup inside table cells", () => {
        const el = render("| H |\n| --- |\n| **bold** |");
        expect(el.querySelector("tbody strong")?.textContent).toBe("bold");
    });

    it("starts a table even with no blank line after a paragraph", () => {
        const el = render("Results:\n| A | B |\n| - | - |\n| 1 | 2 |");
        expect(el.querySelector("p")?.textContent).toBe("Results:");
        expect(el.querySelector("table")).not.toBeNull();
        expect(el.querySelectorAll("tbody td")).toHaveLength(2);
    });

    it("treats a pipe line with no delimiter row as plain text", () => {
        const el = render("a | b | c");
        expect(el.querySelector("table")).toBeNull();
        expect(el.textContent).toBe("a | b | c");
    });

    it("keeps inline formatting on long messages (iteration bound scales with length)", () => {
        // More inline tokens than the old fixed 10000-iteration cap, which used
        // to render the tail as plain text instead of <em>.
        const n = 12000;
        const el = render("*a*".repeat(n));
        expect(el.querySelectorAll("em")).toHaveLength(n);
    });
});

describe("renderMarkdown images", () => {
    const PNG = "data:image/png;base64,iVBORw0KGgo=";

    it("renders an inline data image an agent replied with", () => {
        const el = render(`![camera.front](${PNG})`);
        const img = el.querySelector("img");
        expect(img).not.toBeNull();
        expect(img!.getAttribute("src")).toBe(PNG);
        expect(img!.alt).toBe("camera.front");
    });

    it("accepts the jpeg a camera snapshot actually arrives as", () => {
        const el = render("![shot](data:image/jpeg;base64,/9j/4AAQ)");
        expect(el.querySelector("img")).not.toBeNull();
    });

    describe("accepts every raster type in the allow-list", () => {
        // Pinned in full: with only png and jpeg covered, narrowing the pattern
        // would leave the rest failing silently at runtime instead of here.
        const accepted: [string, string][] = [
            ["png", "data:image/png;base64,iVBORw0KGgo="],
            ["jpeg", "data:image/jpeg;base64,/9j/4AAQ"],
            ["jpg", "data:image/jpg;base64,/9j/4AAQ"],
            ["gif", "data:image/gif;base64,R0lGODlhAQ=="],
            ["webp", "data:image/webp;base64,UklGRh4AAABXRUJQ"],
            ["avif", "data:image/avif;base64,AAAAIGZ0eXBhdmlm"],
            ["blob", "blob:http://localhost:8888/9f1c-0e2a"],
            ["http", "http://node-a.local/plot.png"],
        ];

        it.each(accepted)("accepts %s", (_label, src) => {
            expect(render(`![x](${src})`).querySelector("img")).not.toBeNull();
        });
    });

    it("accepts an https image, which is how a remote node returns one", () => {
        const el = render("![chart](https://node-a.local/plot.png)");
        expect(el.querySelector("img")!.getAttribute("src")).toBe("https://node-a.local/plot.png");
    });

    it("gives an unlabelled image a non-empty alt", () => {
        // Screen readers announce nothing useful for alt="".
        expect(render(`![](${PNG})`).querySelector("img")!.alt).toBe("image");
    });

    it("does not turn an image into a link", () => {
        // The image rule runs first, so no stray "!" and no anchor.
        const el = render(`![shot](${PNG})`);
        expect(el.querySelector("a")).toBeNull();
        expect(el.textContent).toBe("");
    });

    it("still renders an ordinary link", () => {
        const el = render("[docs](https://example.com)");
        expect(el.querySelector("a")!.getAttribute("href")).toBe("https://example.com");
    });

    describe("refuses anything that is not an image", () => {
        const rejected: [string, string][] = [
            ["javascript:", "javascript:alert(1)"],
            ["an html data url", "data:text/html;base64,PHNjcmlwdD4="],
            ["svg, which can carry script", "data:image/svg+xml;base64,PHN2Zz4="],
            ["a non-base64 data url", "data:image/png,notbase64"],
            ["a bare file path", "/tmp/snapshot.png"],
        ];

        it.each(rejected)("rejects %s", (_label, src) => {
            const el = render(`![x](${src})`);
            expect(el.querySelector("img")).toBeNull();
            // Left as the text it was, so nothing vanishes without explanation.
            expect(el.textContent).toContain("![x]");
        });
    });
});
