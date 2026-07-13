/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Dashboard chrome: the top header (logo, connection badge, health, view tabs,
 * audio + reset popovers) and the mobile bottom nav. View switching is routed
 * back via `onSetView`; the active-view highlight is maintained by the caller.
 */
import type { View, ConnState } from "./types";
import { uid } from "../../ids";
import { buildAudioPopover, buildResetPopover, type ResetPopover } from "./popovers";
import { iconMarkup, type IconName } from "./icons";

export interface HeaderOpts {
    view: View;
    connState: ConnState;
    onSetView: (v: View) => void;
    /** HA base URL (from /api/config) for the external "Devices" link; null hides it. */
    haUrl: string | null;
}

/** Only http(s) links are safe in an href — `javascript:`/`data:` carry no HTML
 *  metacharacters so escaping won't neutralise them; collapse anything else. */
function safeHref(url: string): string {
    return /^https?:\/\//i.test(url.trim()) ? url : "#";
}

/** Point a Devices link at the HA UI, or hide it when no URL is configured. */
function applyHaNavUrl(a: HTMLAnchorElement, haUrl: string | null): void {
    if (haUrl) {
        a.href = safeHref(haUrl);
        a.title = `Open Home Assistant — ${haUrl}`;
        a.style.display = "";
    } else {
        a.removeAttribute("href");
        a.style.display = "none";
    }
}

/** The "Devices" entry is an external link to the HA UI (new tab), not a view. */
function buildHaNavLink(haUrl: string | null, mobile: boolean): HTMLAnchorElement {
    const a = document.createElement("a");
    a.className = mobile ? "af-view-btn af-bottom-tab af-ha-nav-link" : "af-view-btn af-ha-nav-link";
    a.target = "_blank";
    a.rel = "noopener";
    a.setAttribute("aria-label", "Open Home Assistant in a new tab");
    a.innerHTML = mobile
        ? `<span class="af-bottom-tab-icon">${iconMarkup("home", 20)}</span><span class="af-bottom-tab-label">Devices</span><span class="af-ha-ext" aria-hidden="true">↗</span>`
        : `${iconMarkup("home")}<span class="af-view-label">Devices</span><span class="af-ha-ext" aria-hidden="true">↗</span>`;
    applyHaNavUrl(a, haUrl);
    return a;
}

/** Update every Devices link under `root` after the HA URL is seeded from /api/config. */
export function setHaNavUrl(root: HTMLElement, haUrl: string | null): void {
    root.querySelectorAll<HTMLAnchorElement>(".af-ha-nav-link").forEach(a => applyHaNavUrl(a, haUrl));
}

/** Toggle `popover` from `btn`, positioning it under the button, closing on
 *  outside click. `onClose` runs whenever the popover is dismissed. */
function wirePopover(btn: HTMLElement, popover: HTMLElement, onClose?: (pop: HTMLElement) => void): void {
    document.body.appendChild(popover);
    if (!popover.id) {
        popover.id = uid("af-pop");
    }
    btn.setAttribute("aria-haspopup", "true");
    btn.setAttribute("aria-controls", popover.id);
    btn.setAttribute("aria-expanded", "false");
    btn.addEventListener("click", e => {
        e.stopPropagation();
        const open = popover.classList.toggle("open");
        btn.setAttribute("aria-expanded", String(open));
        if (open) {
            const r = btn.getBoundingClientRect();
            popover.style.top = `${r.bottom + 6}px`;
            popover.style.right = `${window.innerWidth - r.right}px`;
        } else {
            onClose?.(popover);
        }
    });
    // Page-lifetime listener: CardDashboard is a single instance never remounted,
    // so this is intentionally not removed. If that assumption ever changes, this leaks.
    document.addEventListener("click", e => {
        if (!popover.contains(e.target as Node)) {
            onClose?.(popover);
            popover.classList.remove("open");
            btn.setAttribute("aria-expanded", "false");
        }
    });
}

function buildHeaderLeft(connState: ConnState): HTMLElement {
    const left = document.createElement("div");
    left.className = "af-header-left";

    const icon = document.createElement("img");
    icon.src = "./favicon.svg";
    icon.width = 22;
    icon.height = 22;
    icon.alt = "Wactorz";
    icon.style.opacity = "0.9";

    const title = document.createElement("span");
    title.className = "af-title";
    title.textContent = "Wactorz";

    const connBadge = document.createElement("span");
    connBadge.className = `af-conn-badge af-conn-${connState}`;
    connBadge.textContent = "○ Connecting…";

    left.append(icon, title, connBadge);
    return left;
}

function buildHeaderRight(view: View, onSetView: (v: View) => void, haUrl: string | null): HTMLElement {
    const right = document.createElement("div");
    right.className = "af-header-right";

    const views: { key: View; label: string; icon: IconName }[] = [
        { key: "overview", label: "Overview", icon: "grid" },
        { key: "feed", label: "Feed", icon: "list" },
        { key: "chat", label: "Chat", icon: "chat" },
        { key: "fuseki", label: "Graph", icon: "hexagon" },
        { key: "identity", label: "Identity", icon: "key" },
        { key: "settings", label: "Settings", icon: "settings" },
    ];
    views.forEach(({ key, label, icon }) => {
        const btn = document.createElement("button");
        btn.className = `af-view-btn${key === view ? " active" : ""}`;
        btn.dataset["view"] = key;
        if (key === view) {
            btn.setAttribute("aria-current", "page");
        }
        btn.innerHTML = `${iconMarkup(icon)}<span class="af-view-label">${label}</span>`;
        btn.addEventListener("click", () => onSetView(key));
        right.appendChild(btn);
    });
    // Devices links out to the HA UI rather than embedding a controllable view.
    right.appendChild(buildHaNavLink(haUrl, false));

    const audioBtn = document.createElement("button");
    audioBtn.className = "af-view-btn af-view-btn-icon";
    audioBtn.title = "Audio settings";
    audioBtn.setAttribute("aria-label", "Audio settings");
    audioBtn.innerHTML = iconMarkup("volume");
    right.appendChild(audioBtn);
    wirePopover(audioBtn, buildAudioPopover());

    const resetBtn = document.createElement("button");
    resetBtn.className = "af-view-btn af-view-btn-icon";
    resetBtn.title = "Clear stored state";
    resetBtn.setAttribute("aria-label", "Clear stored state");
    resetBtn.innerHTML = iconMarkup("reset");
    right.appendChild(resetBtn);
    wirePopover(resetBtn, buildResetPopover(), pop => (pop as ResetPopover)._resetArmed());

    return right;
}

/** Build the top header (logo, connection badge, health, view tabs, audio + reset popovers). */
export function buildHeader(opts: HeaderOpts): HTMLElement {
    const header = document.createElement("div");
    header.className = "af-header";

    const center = document.createElement("div");
    center.className = "af-header-center";
    const health = document.createElement("span");
    health.className = "af-health";
    health.textContent = "0/0 wa healthy";
    center.appendChild(health);

    header.append(
        buildHeaderLeft(opts.connState),
        center,
        buildHeaderRight(opts.view, opts.onSetView, opts.haUrl),
    );
    return header;
}

function bottomTab(key: View, icon: IconName, label: string, view: View, extra: string): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.className = `af-view-btn af-bottom-tab${extra}${key === view ? " active" : ""}`;
    btn.dataset["view"] = key;
    if (key === view) {
        btn.setAttribute("aria-current", "page");
    }
    btn.innerHTML = `<span class="af-bottom-tab-icon">${iconMarkup(icon, 20)}</span><span class="af-bottom-tab-label">${label}</span>`;
    return btn;
}

/** Mobile bottom nav with a slide-up "More" sheet for secondary views. */
export function buildBottomNav(opts: {
    view: View;
    onSetView: (v: View) => void;
    haUrl: string | null;
}): HTMLElement {
    const { view, onSetView, haUrl } = opts;
    const nav = document.createElement("nav");
    nav.className = "af-bottom-nav";

    const sheet = document.createElement("div");
    sheet.className = "af-bottom-sheet";
    const moreBtn = document.createElement("button");
    moreBtn.className = "af-bottom-tab af-bottom-more-btn";
    moreBtn.innerHTML = `<span class="af-bottom-tab-icon">${iconMarkup("more", 20)}</span><span class="af-bottom-tab-label">More</span>`;

    const primary: { key: View; icon: IconName; label: string }[] = [
        { key: "overview", icon: "grid", label: "Overview" },
        { key: "feed", icon: "list", label: "Feed" },
        { key: "chat", icon: "chat", label: "Chat" },
    ];
    primary.forEach(({ key, icon, label }) => {
        const btn = bottomTab(key, icon, label, view, "");
        btn.addEventListener("click", () => {
            sheet.classList.remove("open");
            onSetView(key);
        });
        nav.appendChild(btn);
    });
    // Devices links out to the HA UI (new tab) rather than switching views.
    nav.appendChild(buildHaNavLink(haUrl, true));

    const secondary: { key: View; icon: IconName; label: string }[] = [
        { key: "fuseki", icon: "hexagon", label: "Graph" },
        { key: "identity", icon: "key", label: "Identity" },
        { key: "settings", icon: "settings", label: "Settings" },
    ];
    secondary.forEach(({ key, icon, label }) => {
        const btn = bottomTab(key, icon, label, view, " af-bottom-sheet-btn");
        btn.addEventListener("click", () => {
            sheet.classList.remove("open");
            moreBtn.classList.remove("active");
            onSetView(key);
        });
        sheet.appendChild(btn);
    });

    moreBtn.setAttribute("aria-haspopup", "true");
    moreBtn.setAttribute("aria-expanded", "false");
    moreBtn.addEventListener("click", e => {
        e.stopPropagation();
        sheet.classList.toggle("open");
        const open = sheet.classList.contains("open");
        moreBtn.classList.toggle("active", open);
        moreBtn.setAttribute("aria-expanded", String(open));
    });
    // Page-lifetime listener (see note above): single-instance, not removed by design.
    document.addEventListener("click", () => {
        sheet.classList.remove("open");
        moreBtn.classList.remove("active");
        moreBtn.setAttribute("aria-expanded", "false");
    });

    nav.append(moreBtn, sheet);
    return nav;
}
