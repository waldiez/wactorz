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
import { BUILTIN_VIEWS, SETTINGS_VIEW } from "./types";
import { uid } from "../../ids";
import { buildAudioPopover, buildResetPopover, type ResetPopover } from "./popovers";
import { escapeHtml } from "../escapeHtml";
import { iconMarkup, type IconName } from "./icons";

export interface HeaderOpts {
    view: View;
    connState: ConnState;
    onSetView: (v: View) => void;
    /** HA base URL (from /api/config) for the external "Devices" link; null hides it. */
    haUrl: string | null;
    /** Extension-registered extra nav buttons. */
    extraViews: { key: View; label: string; icon: IconName }[];
}

/** Only http(s) links are safe in an href — `javascript:`/`data:` carry no HTML
 *  metacharacters so escaping won't neutralise them; collapse anything else. */
function safeHref(url: string): string {
    return /^https?:\/\//i.test(url.trim()) ? url : "#";
}

/** Container-internal HA URLs (the add-on's supervisor proxy,
 *  `http://supervisor/core`) only resolve inside the add-on network. */
function isContainerInternalUrl(url: string): boolean {
    try {
        return new URL(url).hostname === "supervisor";
    } catch {
        return false;
    }
}

/** Resolve the HA URL the Devices link should point at. A container-internal
 *  URL (supervisor proxy mode in the HA add-on) cannot resolve in the user's
 *  browser, so it is rewritten to the page's own origin — under HA ingress
 *  that origin IS the Home Assistant UI. Anything else passes through. */
export function resolveHaNavUrl(haUrl: string | null): string | null {
    if (haUrl && isContainerInternalUrl(haUrl)) {
        return window.location.origin;
    }
    return haUrl;
}

/** Point a Devices link at the HA UI, or hide it when no URL is configured. */
function applyHaNavUrl(a: HTMLAnchorElement, haUrl: string | null): void {
    const resolved = resolveHaNavUrl(haUrl);
    if (resolved) {
        a.href = safeHref(resolved);
        a.title = `Open Home Assistant — ${resolved}`;
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

/**
 * Popovers live on `document.body`, not inside the header, so that they can
 * overflow it. That means replacing the header does not dispose of them: the
 * elements and their outside-click listeners on `document` survive, and every
 * rebuild adds another set. Each one is recorded here so a rebuild can undo it.
 */
const openPopovers: { el: HTMLElement; outsideClick: EventListener; keydown: EventListener }[] = [];

/** A popover with extra teardown beyond its outside-click listener (e.g. the
 *  audio popover's `tts-voices-loaded` subscription). Same `_`-prefixed hook
 *  convention as `ResetPopover._resetArmed`. */
export interface ReleasablePopover extends HTMLElement {
    _release?: () => void;
}

/** Dispose of the popovers the current header installed. Call before rebuilding
 *  the header, and on teardown; safe to call when there are none. */
export function releaseHeaderPopovers(): void {
    for (const { el, outsideClick, keydown } of openPopovers.splice(0)) {
        document.removeEventListener("click", outsideClick);
        document.removeEventListener("keydown", keydown);
        (el as ReleasablePopover)._release?.();
        el.remove();
    }
}

/** Toggle `popover` from `btn`, positioning it under the button, closing on
 *  outside click. `onClose` runs whenever the popover is dismissed. */
function wirePopover(btn: HTMLElement, popover: HTMLElement, onClose?: (pop: HTMLElement) => void): void {
    document.body.appendChild(popover);
    popover.dataset.afPopover = "";
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
    // The button's own listener dies with the header; this one is on `document`
    // and must be taken down explicitly (see releaseHeaderPopovers).
    const outsideClick: EventListener = e => {
        if (!popover.contains(e.target as Node)) {
            onClose?.(popover);
            popover.classList.remove("open");
            btn.setAttribute("aria-expanded", "false");
        }
    };
    document.addEventListener("click", outsideClick);
    // Escape closes an open popover and returns focus to its trigger — tracked
    // alongside the outside-click listener so rebuilds don't strand it.
    const keydown: EventListener = e => {
        if ((e as KeyboardEvent).key === "Escape" && popover.classList.contains("open")) {
            onClose?.(popover);
            popover.classList.remove("open");
            btn.setAttribute("aria-expanded", "false");
            btn.focus();
        }
    };
    document.addEventListener("keydown", keydown);
    openPopovers.push({ el: popover, outsideClick, keydown });
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

function buildHeaderRight(
    view: View,
    onSetView: (v: View) => void,
    haUrl: string | null,
    extraViews: { key: View; label: string; icon: IconName }[],
): HTMLElement {
    const right = document.createElement("div");
    right.className = "af-header-right";

    const allViews = [...BUILTIN_VIEWS, ...extraViews, SETTINGS_VIEW];
    allViews.forEach(({ key, label, icon }) => {
        const btn = document.createElement("button");
        btn.className = `af-view-btn${key === view ? " active" : ""}`;
        btn.dataset["view"] = key;
        if (key === view) {
            btn.setAttribute("aria-current", "page");
        }
        // escapeHtml: `label` comes from `extraViews`, which extensions supply.
        btn.innerHTML = `${iconMarkup(icon)}<span class="af-view-label">${escapeHtml(label)}</span>`;
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
        buildHeaderRight(opts.view, opts.onSetView, opts.haUrl, opts.extraViews),
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

/** The bottom nav's document-level listeners live on `document`, so replacing
 *  the nav strands them. The live pair is tracked here so a rebuild can retire
 *  it and teardown can remove it. */
let bottomNavListeners: { click: EventListener; keydown: EventListener } | null = null;

/** Remove the bottom nav's document-level listeners, if any.
 *  Safe to call when there are none. */
export function releaseBottomNav(): void {
    if (bottomNavListeners) {
        document.removeEventListener("click", bottomNavListeners.click);
        document.removeEventListener("keydown", bottomNavListeners.keydown);
        bottomNavListeners = null;
    }
}

/** Wire the More button to toggle its slide-up sheet, keeping `aria-expanded`
 *  in step. Only listeners on the button itself, which die with it — dismissing
 *  the sheet from outside is `buildBottomNav`'s job, since those listeners sit
 *  on `document` and outlive a rebuild. */
function wireMoreSheet(sheet: HTMLElement, moreBtn: HTMLButtonElement): void {
    moreBtn.setAttribute("aria-haspopup", "true");
    moreBtn.setAttribute("aria-expanded", "false");
    moreBtn.addEventListener("click", e => {
        e.stopPropagation();
        sheet.classList.toggle("open");
        const open = sheet.classList.contains("open");
        moreBtn.classList.toggle("active", open);
        moreBtn.setAttribute("aria-expanded", String(open));
    });
}

/** Append the always-visible tabs to the nav. Choosing one also dismisses the
 *  More sheet, so a tap never leaves it covering the view it just opened. */
function wirePrimaryBottomNav(
    nav: HTMLElement,
    sheet: HTMLElement,
    view: View,
    onSetView: (v: View) => void,
): void {
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
}

/** Fill the More sheet with the overflow tabs — extension-registered views plus
 *  Settings — which is why the nav is rebuilt whenever a view registers late. */
function wireSecondaryBottomNav(
    sheet: HTMLElement,
    moreBtn: HTMLButtonElement,
    view: View,
    extraViews: { key: View; label: string; icon: IconName }[],
    onSetView: (v: View) => void,
): void {
    const secondary: { key: View; icon: IconName; label: string }[] = [
        ...extraViews,
        { key: SETTINGS_VIEW.key, icon: SETTINGS_VIEW.icon, label: SETTINGS_VIEW.label },
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
}

/** Mobile bottom nav with a slide-up "More" sheet for secondary views. */
export function buildBottomNav(opts: {
    view: View;
    onSetView: (v: View) => void;
    haUrl: string | null;
    extraViews: { key: View; label: string; icon: IconName }[];
}): HTMLElement {
    const { view, onSetView, haUrl, extraViews } = opts;
    const nav = document.createElement("nav");
    nav.className = "af-bottom-nav";

    const sheet = document.createElement("div");
    sheet.className = "af-bottom-sheet";
    const moreBtn = document.createElement("button");
    moreBtn.className = "af-bottom-tab af-bottom-more-btn";
    moreBtn.innerHTML = `<span class="af-bottom-tab-icon">${iconMarkup("more", 20)}</span><span class="af-bottom-tab-label">More</span>`;
    wireMoreSheet(sheet, moreBtn);

    wirePrimaryBottomNav(nav, sheet, view, onSetView);
    wireSecondaryBottomNav(sheet, moreBtn, view, extraViews, onSetView);
    // Devices links out to the HA UI (new tab) rather than switching views.
    nav.appendChild(buildHaNavLink(haUrl, true));

    nav.append(moreBtn, sheet);

    releaseBottomNav();
    const closeSheet = () => {
        sheet.classList.remove("open");
        moreBtn.classList.remove("active");
        moreBtn.setAttribute("aria-expanded", "false");
    };
    const keydown: EventListener = e => {
        if ((e as KeyboardEvent).key === "Escape" && sheet.classList.contains("open")) {
            closeSheet();
            moreBtn.focus();
        }
    };
    document.addEventListener("click", closeSheet);
    document.addEventListener("keydown", keydown);
    bottomNavListeners = { click: closeSheet, keydown };
    return nav;
}
