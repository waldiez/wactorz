/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Inline line-icon set (Lucide-derived) for the dashboard chrome and feed.
 *
 * One stroke weight, one viewBox, drawn with ``currentColor`` so every icon
 * inherits the surrounding text colour (and the active-tab highlight) and
 * renders pixel-identically across platforms — unlike the colour emoji they
 * replace, which varied in size and style between OSes.
 *
 * Extensions register custom icons via ``registerIcon(name, svgPaths)``
 * before calling ``registerView()`` with the same icon name.
 */

// ---------------------------------------------------------------------------
// Icon registry
// ---------------------------------------------------------------------------

/** Icon identifier — any string registered via ``registerIcon``. */
export type IconName = string;

const _paths = new Map<string, string>();

/** SVG shapes an icon may be built from. Anything else — `<script>`,
 *  `<foreignObject>`, `<image>`, `<use>` — is dropped. */
const SHAPE_TAGS = new Set(["g", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon"]);

/** Attributes those shapes may carry. Event handlers and every `href` variant
 *  are absent by construction rather than by blocklist. */
const SHAPE_ATTRS = new Set([
    "d",
    "cx",
    "cy",
    "r",
    "rx",
    "ry",
    "x",
    "y",
    "x1",
    "y1",
    "x2",
    "y2",
    "width",
    "height",
    "points",
    "transform",
    "opacity",
    "fill",
    "fill-rule",
    "clip-rule",
    "stroke",
    "stroke-width",
    "stroke-linecap",
    "stroke-linejoin",
]);

/** Drop every element and attribute outside the allow-lists, in place. */
function prune(parent: Element): void {
    for (const child of [...parent.children]) {
        if (!SHAPE_TAGS.has(child.tagName.toLowerCase())) {
            child.remove();
            continue;
        }
        for (const attr of [...child.attributes]) {
            if (!SHAPE_ATTRS.has(attr.name.toLowerCase())) {
                child.removeAttribute(attr.name);
            }
        }
        prune(child);
    }
}

/** Register a custom icon so ``iconMarkup(name)`` renders it.
 *
 * The markup is reduced to shapes at registration, not at render: it is stored
 * once and drawn many times, and `iconMarkup` interpolates it straight into
 * `innerHTML`. Extensions supply this string, so without the reduction an icon
 * could carry `<foreignObject><img src=x onerror=…>` and run script wherever
 * the icon appears — which is the header, the nav and every card.
 *
 * Parsing happens inside a `<template>`, whose content is inert: nothing
 * executes and no resource is fetched while the markup is being inspected.
 */
export function registerIcon(name: string, svgPaths: string): void {
    const tpl = document.createElement("template");
    tpl.innerHTML = `<svg>${svgPaths}</svg>`;
    const svg = tpl.content.querySelector("svg");
    if (!svg) {
        return;
    }
    prune(svg);
    _paths.set(name, svg.innerHTML);
}

/** Markup for an inline icon at `size` px (default 16). Inherits `currentColor`. */
export function iconMarkup(name: IconName, size = 16): string {
    const paths = _paths.get(name);
    if (!paths) {
        return "";
    }
    return (
        `<svg class="af-icon" viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" ` +
        `stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ` +
        `aria-hidden="true">${paths}</svg>`
    );
}

// ---------------------------------------------------------------------------
// Built-in icons (pre-registered)
// ---------------------------------------------------------------------------

registerIcon(
    "grid",
    '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/>',
);
registerIcon(
    "list",
    '<line x1="3" x2="21" y1="6" y2="6"/><line x1="3" x2="21" y1="12" y2="12"/><line x1="3" x2="21" y1="18" y2="18"/>',
);
registerIcon("chat", '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>');
registerIcon(
    "home",
    '<path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>',
);
registerIcon(
    "settings",
    '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
);
registerIcon(
    "volume",
    '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>',
);
registerIcon("reset", '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>');
registerIcon(
    "more",
    '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
);
registerIcon("zap", '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>');
registerIcon(
    "heart",
    '<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/>',
);
registerIcon(
    "alertCircle",
    '<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/>',
);
registerIcon(
    "alertTriangle",
    '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
);
registerIcon("square", '<rect width="18" height="18" x="3" y="3" rx="2"/>');
registerIcon("activity", '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>');
registerIcon(
    "flag",
    '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" x2="4" y1="22" y2="15"/>',
);
registerIcon(
    "mic",
    '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/>',
);
registerIcon(
    "file",
    '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v6h6"/>',
);
registerIcon(
    "sign-out",
    '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" x2="9" y1="12" y2="12"/>',
);
