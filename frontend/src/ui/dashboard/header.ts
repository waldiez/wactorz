/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Dashboard chrome: the top header (logo, connection badge, health, view tabs,
 * audio + reset popovers) and the mobile bottom nav. View switching is routed
 * back via `onSetView`; the active-view highlight is maintained by the caller.
 */
import type { ConnState } from "../../types/ws";
import type { View } from "./types";
import { BUILTIN_VIEWS, SETTINGS_VIEW } from "./types";
import { uid } from "../../ids";
import { buildAudioPopover, buildResetPopover, type ResetPopover } from "./popovers";
import { VERSION_KEY } from "../../config/serverConfig";
import { safeStorage } from "../../safeStorage";
import { buildSignOutButton } from "./signOut";
import { iconMarkup, type IconName } from "./icons";
import { button, el, expanded, externalLink, focusables, iconButton, live, testId } from "../dom";

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

/**
 * A bottom tab's contents: its icon in the wrapper the layout sizes, then its
 * label. The label is set as text — extension views supply their own.
 */
function bottomTabContent(icon: IconName, label: string): HTMLElement[] {
    const iconWrap = el("span", "af-bottom-tab-icon");
    iconWrap.innerHTML = iconMarkup(icon, 20);
    return [iconWrap, el("span", "af-bottom-tab-label", label)];
}

/** A header view button's contents: its icon, then its label as text. */
function viewButtonContent(target: HTMLElement, icon: IconName, label: string): void {
    target.innerHTML = iconMarkup(icon);
    target.appendChild(el("span", "af-view-label", label));
}

/** The "Devices" entry is an external link to the HA UI (new tab), not a view. */
function buildHaNavLink(haUrl: string | null, mobile: boolean): HTMLAnchorElement {
    const a = externalLink(
        "",
        mobile ? "af-view-btn af-bottom-tab af-ha-nav-link" : "af-view-btn af-ha-nav-link",
    );
    a.setAttribute("aria-label", "Open Home Assistant in a new tab");
    if (mobile) {
        a.append(...bottomTabContent("home", "Devices"));
    } else {
        viewButtonContent(a, "home", "Devices");
    }
    const ext = el("span", "af-ha-ext", "↗");
    ext.setAttribute("aria-hidden", "true");
    a.appendChild(ext);
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
 *  outside click. `onClose` runs whenever the popover is dismissed.
 *
 *  A disclosure, not a menu: the button says whether the panel is open and
 *  which one it is, and the panel is a group named after the button. A menu
 *  role would promise arrow-key navigation between its entries. */
function wirePopover(btn: HTMLElement, popover: HTMLElement, onClose?: (pop: HTMLElement) => void): void {
    document.body.appendChild(popover);
    popover.dataset.afPopover = "";
    if (!popover.id) {
        popover.id = uid("af-pop");
    }
    popover.setAttribute("role", "group");
    popover.setAttribute("aria-label", btn.getAttribute("aria-label") ?? "");
    btn.setAttribute("aria-controls", popover.id);
    expanded(btn, false);
    btn.addEventListener("click", e => {
        e.stopPropagation();
        const open = popover.classList.toggle("open");
        expanded(btn, open);
        if (open) {
            const r = btn.getBoundingClientRect();
            popover.style.top = `${r.bottom + 6}px`;
            popover.style.right = `${window.innerWidth - r.right}px`;
            // The popover sits at the end of the page, so Tab from the button
            // would never reach it: focus goes in, and Escape brings it back.
            focusables(popover)[0]?.focus();
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
            expanded(btn, false);
        }
    };
    document.addEventListener("click", outsideClick);
    // Escape closes an open popover and returns focus to its trigger — tracked
    // alongside the outside-click listener so rebuilds don't strand it.
    const keydown: EventListener = e => {
        if ((e as KeyboardEvent).key === "Escape" && popover.classList.contains("open")) {
            onClose?.(popover);
            popover.classList.remove("open");
            expanded(btn, false);
            btn.focus();
        }
    };
    document.addEventListener("keydown", keydown);
    openPopovers.push({ el: popover, outsideClick, keydown });
}

/** The running version, beside the name.
 *
 *  There rather than in a corner: it answers "which one is this?", which is
 *  asked while looking at the thing it names. Empty until the server has said,
 *  so it never shows a guess -- and it is the server's answer, not the bundle's,
 *  because `static/app` is committed and can lag the wheel serving it. */
function buildVersion(): HTMLElement {
    const version = el("span", "af-version");
    const running = safeStorage.get(VERSION_KEY);
    if (running) {
        version.textContent = running;
        version.title = `Wactorz ${running}`;
    }
    return version;
}

function buildHeaderLeft(connState: ConnState): HTMLElement {
    const left = el("div", "af-header-left");

    const icon = el("img");
    icon.src = "./favicon.svg";
    icon.width = 22;
    icon.height = 22;
    icon.alt = "Wactorz";
    icon.style.opacity = "0.9";

    const title = el("span", "af-title", "Wactorz");
    // Read out when it changes, so losing the server is heard and not only seen.
    const connBadge = testId(
        live(el("span", `af-conn-badge af-conn-${connState}`, CONN_TEXT[connState]), "status"),
        "connection",
    );

    left.append(icon, title, buildVersion(), connBadge);
    return left;
}

/** What the connection badge says in each state. */
const CONN_TEXT: Record<ConnState, string> = {
    live: "● live",
    connecting: "○ Connecting…",
    disconnected: "◎ Disconnected…",
};

/** Show `state` on the connection badge under `root`, if the header is there. */
export function renderConnBadge(root: ParentNode, state: ConnState): void {
    const badge = root.querySelector<HTMLElement>(".af-conn-badge");
    if (badge) {
        badge.className = `af-conn-badge af-conn-${state}`;
        badge.textContent = CONN_TEXT[state];
    }
}

/** Show on the Inbox tabs under `root` how many questions wait, or nothing when none do.
 *  Inside the tab's text, not an `aria-label`: a tab's name has to hold what it shows. */
export function renderInboxCount(root: ParentNode, waiting: number): void {
    root.querySelectorAll<HTMLElement>('.af-view-btn[data-view="inbox"]').forEach(tab => {
        tab.querySelector(".af-nav-count")?.remove();
        if (waiting > 0) {
            const count = el("span", "af-nav-count", ` ${waiting}`);
            count.appendChild(el("span", "af-screen-reader", " waiting"));
            tab.appendChild(count);
        }
    });
}

/** A square icon-only header button, labelled for anyone not seeing the icon. */
function headerIconButton(label: string, icon: IconName): HTMLButtonElement {
    return iconButton("af-view-btn af-view-btn-icon", label, iconMarkup(icon));
}

function buildHeaderRight(
    view: View,
    onSetView: (v: View) => void,
    haUrl: string | null,
    extraViews: { key: View; label: string; icon: IconName }[],
): HTMLElement {
    const right = el("div", "af-header-right");

    // Laid out as if its buttons were the header's own (`display: contents`).
    const nav = el("nav", "af-view-nav");
    nav.setAttribute("aria-label", "Views");
    right.appendChild(nav);
    const allViews = [...BUILTIN_VIEWS, ...extraViews, SETTINGS_VIEW];
    allViews.forEach(({ key, label, icon }) => {
        const btn = button(`af-view-btn${key === view ? " active" : ""}`);
        btn.dataset["view"] = key;
        if (key === view) {
            btn.setAttribute("aria-current", "page");
        }
        viewButtonContent(btn, icon, label);
        btn.addEventListener("click", () => onSetView(key));
        nav.appendChild(btn);
    });
    // Devices links out to the HA UI rather than embedding a controllable view.
    right.appendChild(buildHaNavLink(haUrl, false));

    const audioBtn = headerIconButton("Audio settings", "volume");
    right.appendChild(audioBtn);
    wirePopover(audioBtn, buildAudioPopover());

    const resetBtn = headerIconButton("Clear stored state", "reset");
    right.appendChild(resetBtn);
    wirePopover(resetBtn, buildResetPopover(), pop => (pop as ResetPopover)._resetArmed());

    // Last, and hidden on an install with no sign-in — revealed by
    // setSignOutVisible once /api/config has answered.
    right.appendChild(buildSignOutButton());

    return right;
}

/** Build the top header (logo, connection badge, health, view tabs, audio + reset popovers). */
export function buildHeader(opts: HeaderOpts): HTMLElement {
    const header = el("div", "af-header");

    const center = el("div", "af-header-center");
    center.appendChild(el("span", "af-health", "0/0 wa healthy"));

    header.append(
        buildHeaderLeft(opts.connState),
        center,
        buildHeaderRight(opts.view, opts.onSetView, opts.haUrl, opts.extraViews),
    );
    return header;
}

function bottomTab(key: View, icon: IconName, label: string, view: View, extra: string): HTMLButtonElement {
    const btn = button(`af-view-btn af-bottom-tab${extra}${key === view ? " active" : ""}`);
    btn.dataset["view"] = key;
    if (key === view) {
        btn.setAttribute("aria-current", "page");
    }
    btn.append(...bottomTabContent(icon, label));
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
    expanded(moreBtn, false);
    moreBtn.addEventListener("click", e => {
        e.stopPropagation();
        sheet.classList.toggle("open");
        const open = sheet.classList.contains("open");
        moreBtn.classList.toggle("active", open);
        expanded(moreBtn, open);
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
    BUILTIN_VIEWS.filter(v => PHONE_BAR.includes(v.key)).forEach(({ key, icon, label }) => {
        const btn = bottomTab(key, icon, label, view, "");
        btn.addEventListener("click", () => {
            sheet.classList.remove("open");
            onSetView(key);
        });
        nav.appendChild(btn);
    });
}

/** The views the phone's bar always shows; every other one is in the More sheet.
 *  The bar is for reacting: what needs the person, and who to talk to about it. */
const PHONE_BAR: readonly View[] = ["overview", "chat", "inbox"];

/** Fill the More sheet with the overflow tabs — the built-in views the bar has
 *  no room for, extension-registered views, then Settings — which is why the nav
 *  is rebuilt whenever a view registers late. */
function wireSecondaryBottomNav(
    sheet: HTMLElement,
    moreBtn: HTMLButtonElement,
    view: View,
    extraViews: { key: View; label: string; icon: IconName }[],
    onSetView: (v: View) => void,
): void {
    const secondary: { key: View; icon: IconName; label: string }[] = [
        ...BUILTIN_VIEWS.filter(v => !PHONE_BAR.includes(v.key)),
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
    const nav = el("nav", "af-bottom-nav");
    nav.setAttribute("aria-label", "Views");

    const sheet = el("div", "af-bottom-sheet");
    const moreBtn = button("af-bottom-tab af-bottom-more-btn");
    moreBtn.append(...bottomTabContent("more", "More"));
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
        expanded(moreBtn, false);
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
