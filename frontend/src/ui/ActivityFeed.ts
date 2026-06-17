/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Activity feed — collapsible right panel showing all MQTT events.
 *
 * - Collapsed by default; toggle with #feed-toggle button
 * - Badge on toggle button shows count when collapsed; shows "500+" when capped
 * - Colour-coded rows: spawn=green, error=red, warning=amber, chat=cyan, stopped=dim
 * - Max 500 items; auto-scrolls; pauses on hover; cap banner shown when exceeded
 */

import { displayName } from "../agents/naming";
import { iconMarkup, type IconName } from "./dashboard/icons";

export type FeedEventType =
    | "spawn"
    | "heartbeat"
    | "chat"
    | "alert-error"
    | "alert-warning"
    | "stopped"
    | "health"
    | "qa-flag";

export interface FeedItem {
    type: FeedEventType;
    label: string;
    agentName: string;
    timestamp: number;
    /** Speaker role for chat rows ("user" | "assistant"); absent for other types. */
    role?: string | undefined;
}

const MAX_ITEMS = 500;

const TYPE_CLASS: Record<FeedEventType, string> = {
    spawn: "af-feed-spawn",
    heartbeat: "af-feed-heartbeat",
    chat: "af-feed-chat",
    "alert-error": "af-feed-alert",
    "alert-warning": "af-feed-alert",
    stopped: "",
    health: "af-feed-heartbeat",
    "qa-flag": "af-feed-chat",
};

const TYPE_ICON: Record<FeedEventType, IconName> = {
    spawn: "zap",
    heartbeat: "heart",
    chat: "chat",
    "alert-error": "alertCircle",
    "alert-warning": "alertTriangle",
    stopped: "square",
    health: "activity",
    "qa-flag": "flag",
};

/** Singleton popover shown on hover for truncated feed messages. */
class FeedTooltip {
    private el: HTMLDivElement;
    private hideTimer = 0;

    constructor() {
        this.el = document.createElement("div");
        this.el.id = "af-feed-tooltip";
        this.el.style.cssText = [
            "position:fixed",
            "z-index:9999",
            "max-width:360px",
            "padding:8px 12px",
            "border-radius:8px",
            "background:rgba(8,14,28,0.96)",
            "border:1px solid rgba(99,139,255,0.25)",
            "backdrop-filter:blur(12px)",
            "box-shadow:0 8px 32px rgba(0,0,0,0.6)",
            "font-size:12px",
            "line-height:1.55",
            "color:rgba(255,255,255,0.82)",
            "white-space:pre-wrap",
            "word-break:break-word",
            "pointer-events:none",
            "opacity:0",
            "transition:opacity 0.12s ease",
        ].join(";");
        document.body.appendChild(this.el);
    }

    show(anchor: HTMLElement, fullText: string): void {
        clearTimeout(this.hideTimer);
        this.el.textContent = fullText;

        // Measure after setting text (width may change)
        const rect = anchor.getBoundingClientRect();
        this.el.style.visibility = "hidden";
        this.el.style.opacity = "0";
        this.el.style.display = "block";
        const tw = Math.min(this.el.offsetWidth, 360);
        const th = this.el.offsetHeight;

        const { top, left } = this._placeAt(rect, tw, th);
        this.el.style.top = `${top}px`;
        this.el.style.left = `${left}px`;
        this.el.style.visibility = "";
        this.el.style.opacity = "1";
        this.el.style.display = "";
    }

    /** Position above the anchor, flipping below / clamping to the viewport. */
    private _placeAt(rect: DOMRect, tw: number, th: number): { top: number; left: number } {
        const gap = 6;
        const vw = window.innerWidth;
        const vh = window.innerHeight;

        let top = rect.top - th - gap;
        if (top < 8) {
            top = rect.bottom + gap;
        }
        if (top + th > vh - 8) {
            top = vh - th - 8;
        }

        let left = rect.left;
        if (left + tw > vw - 8) {
            left = vw - tw - 8;
        }
        if (left < 8) {
            left = 8;
        }

        return { top, left };
    }

    hide(delay = 80): void {
        this.hideTimer = window.setTimeout(() => {
            this.el.style.opacity = "0";
        }, delay);
    }
}

const _tooltip = new FeedTooltip();

export class ActivityFeed {
    private panel: HTMLElement;
    private list: HTMLElement;
    private toggleBtn: HTMLButtonElement;
    private badge: HTMLElement;

    private items: FeedItem[] = [];
    private isOpen = false;
    private isPaused = false;
    private unseenCount = 0;
    private totalReceived = 0;
    private capBanner: HTMLElement | null = null;

    constructor() {
        this.panel = document.getElementById("activity-feed")!;
        this.list = document.getElementById("feed-list")!;
        this.toggleBtn = document.getElementById("feed-toggle") as HTMLButtonElement;
        this.badge = document.getElementById("feed-badge")!;

        this.toggleBtn.addEventListener("click", () => this.toggle());
        this.list.addEventListener("mouseenter", () => {
            this.isPaused = true;
        });
        this.list.addEventListener("mouseleave", () => {
            this.isPaused = false;
        });
    }

    /** Push a new event into the feed. */
    push(item: FeedItem): void {
        this.totalReceived++;
        this.items.push(item);

        if (this.items.length > MAX_ITEMS) {
            this.items.shift();
            // Remove oldest DOM row (after the cap banner, if present)
            const firstRow = this.capBanner ? this.capBanner.nextElementSibling : this.list.firstElementChild;
            firstRow?.remove();
        }

        if (this.totalReceived > MAX_ITEMS) {
            this._updateCapBanner();
        }

        this.renderItem(item);

        if (!this.isOpen) {
            this.unseenCount++;
            this.updateBadge();
        } else if (!this.isPaused) {
            this.list.scrollTop = this.list.scrollHeight;
        }
    }

    /** Empty the feed — used when a reset clears the server-side activity log. */
    clear(): void {
        this.items = [];
        this.list.innerHTML = "";
        this.capBanner = null;
        this.unseenCount = 0;
        this.totalReceived = 0;
        this.updateBadge();
    }

    private toggle(): void {
        this.isOpen = !this.isOpen;
        this.panel.classList.toggle("open", this.isOpen);
        this.toggleBtn.classList.toggle("active", this.isOpen);

        if (this.isOpen) {
            this.unseenCount = 0;
            this.updateBadge();
            this.list.scrollTop = this.list.scrollHeight;
        }
    }

    /** Create a `<span class>` carrying `text`. */
    private _span(className: string, text: string): HTMLSpanElement {
        const el = document.createElement("span");
        el.className = className;
        el.textContent = text;
        return el;
    }

    private renderItem(item: FeedItem): void {
        const row = document.createElement("div");
        row.className = `af-feed-item ${TYPE_CLASS[item.type] ?? ""}`.trim();

        const time = new Date(item.timestamp).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
        });
        const label = item.label ?? "";
        // Display up to 120 chars; full text shown in hover popover
        const trimmed = label.length > 120 ? label.slice(0, 120) + "…" : label;

        // User chat turns are persisted under the agent's name; show "you" so
        // the feed doesn't read as the agent talking to itself.
        const isUserTurn = item.role === "user" || item.agentName === "user";
        const agentSpan = this._span("af-feed-agent", isUserTurn ? "you" : displayName(item.agentName));
        if (isUserTurn) {
            agentSpan.classList.add("af-feed-agent-user");
        }

        const iconSpan = document.createElement("span");
        iconSpan.className = "af-feed-icon";
        iconSpan.innerHTML = iconMarkup(TYPE_ICON[item.type], 14);
        row.appendChild(iconSpan);
        row.appendChild(this._span("af-feed-time", time));
        row.appendChild(agentSpan);
        row.appendChild(this._span("af-feed-text", trimmed));

        if (label.length > 0) {
            row.addEventListener("mouseenter", () => _tooltip.show(row, `${item.agentName}  ·  ${label}`));
            row.addEventListener("mouseleave", () => _tooltip.hide());
        }

        this.list.appendChild(row);
    }

    private updateBadge(): void {
        if (this.unseenCount > 0 && !this.isOpen) {
            const capped = this.totalReceived > MAX_ITEMS;
            this.badge.textContent = capped
                ? `${MAX_ITEMS}+`
                : String(this.unseenCount > 99 ? "99+" : this.unseenCount);
            this.badge.style.display = "inline-flex";
        } else {
            this.badge.style.display = "none";
        }
    }

    private _updateCapBanner(): void {
        if (!this.capBanner) {
            this.capBanner = document.createElement("div");
            this.capBanner.className = "af-feed-cap-banner";
            this.capBanner.style.cssText =
                "text-align:center;padding:4px 8px;font-size:10px;color:rgba(255,255,255,0.35);border-bottom:1px solid rgba(255,255,255,0.07);position:sticky;top:0;background:rgba(10,10,20,0.85);backdrop-filter:blur(4px);z-index:1;";
            this.list.prepend(this.capBanner);
        }
        this.capBanner.textContent = `${MAX_ITEMS}+ events — showing latest ${MAX_ITEMS}`;
    }
}
