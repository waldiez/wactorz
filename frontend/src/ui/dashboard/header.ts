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
import { buildAudioPopover, buildResetPopover } from "./popovers";
import { iconMarkup, type IconName } from "./icons";

export interface HeaderOpts {
    view: View;
    connState: ConnState;
    onSetView: (v: View) => void;
}

/** Toggle `popover` from `btn`, positioning it under the button, closing on
 *  outside click. `onClose` runs whenever the popover is dismissed. */
function wirePopover(btn: HTMLElement, popover: HTMLElement, onClose?: (pop: HTMLElement) => void): void {
    document.body.appendChild(popover);
    btn.addEventListener("click", e => {
        e.stopPropagation();
        const open = popover.classList.toggle("open");
        if (open) {
            const r = btn.getBoundingClientRect();
            popover.style.top = `${r.bottom + 6}px`;
            popover.style.right = `${window.innerWidth - r.right}px`;
        } else {
            onClose?.(popover);
        }
    });
    document.addEventListener("click", e => {
        if (!popover.contains(e.target as Node)) {
            onClose?.(popover);
            popover.classList.remove("open");
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

function buildHeaderRight(view: View, onSetView: (v: View) => void): HTMLElement {
    const right = document.createElement("div");
    right.className = "af-header-right";

    const views: { key: View; label: string; icon: IconName }[] = [
        { key: "overview", label: "Overview", icon: "grid" },
        { key: "feed", label: "Feed", icon: "list" },
        { key: "chat", label: "Chat", icon: "chat" },
        { key: "ha", label: "Devices", icon: "home" },
        { key: "fuseki", label: "Graph", icon: "hexagon" },
        { key: "settings", label: "Settings", icon: "settings" },
    ];
    views.forEach(({ key, label, icon }) => {
        const btn = document.createElement("button");
        btn.className = `af-view-btn${key === view ? " active" : ""}`;
        btn.dataset["view"] = key;
        btn.innerHTML = `${iconMarkup(icon)}<span class="af-view-label">${label}</span>`;
        btn.addEventListener("click", () => onSetView(key));
        right.appendChild(btn);
    });

    const audioBtn = document.createElement("button");
    audioBtn.className = "af-view-btn af-view-btn-icon";
    audioBtn.title = "Audio settings";
    audioBtn.innerHTML = iconMarkup("volume");
    right.appendChild(audioBtn);
    wirePopover(audioBtn, buildAudioPopover());

    const resetBtn = document.createElement("button");
    resetBtn.className = "af-view-btn af-view-btn-icon";
    resetBtn.title = "Clear stored state";
    resetBtn.innerHTML = iconMarkup("reset");
    right.appendChild(resetBtn);
    wirePopover(resetBtn, buildResetPopover(), pop => (pop as any)._resetArmed?.());

    return right;
}

export function buildHeader(opts: HeaderOpts): HTMLElement {
    const header = document.createElement("div");
    header.className = "af-header";

    const center = document.createElement("div");
    center.className = "af-header-center";
    const health = document.createElement("span");
    health.className = "af-health";
    health.textContent = "0/0 wa healthy";
    center.appendChild(health);

    header.append(buildHeaderLeft(opts.connState), center, buildHeaderRight(opts.view, opts.onSetView));
    return header;
}

function bottomTab(key: View, icon: IconName, label: string, view: View, extra: string): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.className = `af-view-btn af-bottom-tab${extra}${key === view ? " active" : ""}`;
    btn.dataset["view"] = key;
    btn.innerHTML = `<span class="af-bottom-tab-icon">${iconMarkup(icon, 20)}</span><span class="af-bottom-tab-label">${label}</span>`;
    return btn;
}

/** Mobile bottom nav with a slide-up "More" sheet for secondary views. */
export function buildBottomNav(opts: { view: View; onSetView: (v: View) => void }): HTMLElement {
    const { view, onSetView } = opts;
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
        { key: "ha", icon: "home", label: "Devices" },
    ];
    primary.forEach(({ key, icon, label }) => {
        const btn = bottomTab(key, icon, label, view, "");
        btn.addEventListener("click", () => {
            sheet.classList.remove("open");
            onSetView(key);
        });
        nav.appendChild(btn);
    });

    const secondary: { key: View; icon: IconName; label: string }[] = [
        { key: "fuseki", icon: "hexagon", label: "Graph" },
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

    moreBtn.addEventListener("click", e => {
        e.stopPropagation();
        sheet.classList.toggle("open");
        moreBtn.classList.toggle("active", sheet.classList.contains("open"));
    });
    document.addEventListener("click", () => {
        sheet.classList.remove("open");
        moreBtn.classList.remove("active");
    });

    nav.append(moreBtn, sheet);
    return nav;
}
