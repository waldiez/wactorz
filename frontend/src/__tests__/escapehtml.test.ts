/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { escapeHtml } from "../ui/escapeHtml";

describe("escapeHtml", () => {
    it("escapes the five HTML-significant characters", () => {
        expect(escapeHtml(`<a href="x" title='y'>&</a>`)).toBe(
            "&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;&lt;/a&gt;",
        );
    });

    it("leaves text without markup characters untouched", () => {
        expect(escapeHtml("report-2026.pdf")).toBe("report-2026.pdf");
    });

    it("escapes a filename that would otherwise break markup", () => {
        expect(escapeHtml("a<b>.png")).toBe("a&lt;b&gt;.png");
    });

    it("coerces null/undefined to an empty string", () => {
        expect(escapeHtml(null)).toBe("");
        expect(escapeHtml(undefined)).toBe("");
    });

    it("stringifies non-string values before escaping", () => {
        expect(escapeHtml(42)).toBe("42");
        expect(escapeHtml(true)).toBe("true");
    });
});
