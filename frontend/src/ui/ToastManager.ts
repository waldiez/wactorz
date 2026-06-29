/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * ToastManager — rich in-app notification toasts.
 *
 * Always shown inside the app window (unlike OS notifications which only fire
 * in the background).  Spring-animated, glassmorphic, warm-palette cards that
 * stack in the bottom-right corner and auto-dismiss after a configurable delay.
 */

import { escapeHtml } from "./escapeHtml";

export type ToastType = "chat" | "spawn" | "alert-error" | "alert-warning" | "welcome" | "system";

export interface ToastAction {
    label: string;
    primary?: boolean;
    onClick: () => void;
}

export interface ToastOptions {
    type?: ToastType;
    title: string;
    message: string;
    durationMs?: number;
    actions?: ToastAction[];
}

interface ThemeEntry {
    strip: string;
    avatar: string;
    badge: string;
    badgeBg: string;
    progress: string;
    label: string;
}

const THEME: Record<ToastType, ThemeEntry> = {
    chat: {
        strip: "linear-gradient(90deg,#f59e0b,#f97316)",
        avatar: "linear-gradient(135deg,#f59e0b,#ea580c)",
        badge: "#fbbf24",
        badgeBg: "rgba(245,158,11,0.15)",
        progress: "#f97316",
        label: "reply",
    },
    spawn: {
        strip: "linear-gradient(90deg,#10b981,#06b6d4)",
        avatar: "linear-gradient(135deg,#10b981,#0891b2)",
        badge: "#34d399",
        badgeBg: "rgba(16,185,129,0.15)",
        progress: "#10b981",
        label: "spawned",
    },
    "alert-error": {
        strip: "linear-gradient(90deg,#ef4444,#f43f5e)",
        avatar: "linear-gradient(135deg,#ef4444,#e11d48)",
        badge: "#f87171",
        badgeBg: "rgba(239,68,68,0.15)",
        progress: "#ef4444",
        label: "error",
    },
    "alert-warning": {
        strip: "linear-gradient(90deg,#f59e0b,#eab308)",
        avatar: "linear-gradient(135deg,#f59e0b,#ca8a04)",
        badge: "#fbbf24",
        badgeBg: "rgba(245,158,11,0.15)",
        progress: "#f59e0b",
        label: "warning",
    },
    welcome: {
        strip: "linear-gradient(90deg,#8b5cf6,#6366f1,#3b82f6)",
        avatar: "linear-gradient(135deg,#8b5cf6,#4f46e5)",
        badge: "#a78bfa",
        badgeBg: "rgba(139,92,246,0.15)",
        progress: "#8b5cf6",
        label: "welcome",
    },
    system: {
        strip: "linear-gradient(90deg,#6366f1,#8b5cf6)",
        avatar: "linear-gradient(135deg,#6366f1,#7c3aed)",
        badge: "#818cf8",
        badgeBg: "rgba(99,102,241,0.15)",
        progress: "#6366f1",
        label: "system",
    },
};

const CSS = `
.wz-toasts {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 99999;
  display: flex;
  flex-direction: column-reverse;
  gap: 10px;
  pointer-events: none;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
}

.wz-toast {
  pointer-events: all;
  width: 330px;
  background: rgba(12, 12, 20, 0.95);
  backdrop-filter: blur(28px) saturate(200%);
  -webkit-backdrop-filter: blur(28px) saturate(200%);
  border-radius: 18px;
  border: 1px solid rgba(255,255,255,0.07);
  box-shadow:
    0 24px 64px rgba(0,0,0,0.65),
    0 0 0 1px rgba(255,255,255,0.03),
    inset 0 1px 0 rgba(255,255,255,0.07);
  overflow: hidden;
  cursor: pointer;
  transform: translateX(calc(100% + 40px));
  opacity: 0;
  transition:
    transform 0.45s cubic-bezier(0.34,1.56,0.64,1),
    opacity  0.3s  ease;
  will-change: transform, opacity;
  user-select: none;
}

.wz-toast:hover { filter: brightness(1.07); }
.wz-toast:active { transform: scale(0.98) !important; }

.wz-toast--in {
  transform: translateX(0);
  opacity: 1;
}
.wz-toast--out {
  transform: translateX(calc(100% + 40px));
  opacity: 0;
  transition:
    transform 0.3s cubic-bezier(0.4,0,1,1),
    opacity   0.25s ease;
}

.wz-toast__strip {
  height: 3px;
  width: 100%;
}

.wz-toast__body {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 14px 16px 10px;
}

.wz-toast__avatar {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  font-weight: 800;
  color: rgba(255,255,255,0.95);
  flex-shrink: 0;
  text-shadow: 0 1px 4px rgba(0,0,0,0.4);
  box-shadow: 0 4px 12px rgba(0,0,0,0.35);
  letter-spacing: 0.3px;
}

.wz-toast__content {
  flex: 1;
  min-width: 0;
  padding-top: 1px;
}

.wz-toast__header {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 5px;
}

.wz-toast__name {
  font-size: 13px;
  font-weight: 700;
  color: rgba(255,255,255,0.92);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 150px;
  letter-spacing: 0.15px;
}

.wz-toast__badge {
  font-size: 9.5px;
  padding: 2px 7px;
  border-radius: 100px;
  font-weight: 700;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  flex-shrink: 0;
}

.wz-toast__time {
  font-size: 10.5px;
  color: rgba(255,255,255,0.28);
  flex-shrink: 0;
  margin-left: auto;
}

.wz-toast__message {
  font-size: 12.5px;
  color: rgba(255,255,255,0.52);
  line-height: 1.5;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  word-break: break-word;
}

.wz-toast__actions {
  padding: 0 16px 13px;
  display: flex;
  gap: 8px;
}

.wz-toast__btn {
  font-size: 11.5px;
  font-weight: 600;
  padding: 5px 13px;
  border-radius: 100px;
  border: 1px solid rgba(255,255,255,0.1);
  background: rgba(255,255,255,0.06);
  color: rgba(255,255,255,0.65);
  cursor: pointer;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
  font-family: inherit;
  letter-spacing: 0.2px;
}
.wz-toast__btn:hover {
  background: rgba(255,255,255,0.12);
  color: rgba(255,255,255,0.9);
  border-color: rgba(255,255,255,0.18);
}

.wz-toast__progress {
  height: 2.5px;
  background: rgba(255,255,255,0.04);
  margin-top: 4px;
}
.wz-toast__progress-bar {
  height: 100%;
  width: 100%;
  transform-origin: left;
  border-radius: 0 2px 2px 0;
}
`;

/** Two-letter avatar initials: first letters of the first two words, else the first two chars. */
export function initials(name: string): string {
    const parts = name.trim().split(/[\s\-_]+/);
    if (parts.length >= 2) {
        const a = parts[0] ? (parts[0][0] ?? "") : "";
        const b = parts[1] ? (parts[1][0] ?? "") : "";
        return (a + b).toUpperCase();
    }
    return name.slice(0, 2).toUpperCase();
}

function timeLabel(): string {
    const d = new Date();
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export class ToastManager {
    private container!: HTMLElement;
    private active: HTMLElement[] = [];
    private readonly MAX = 4;

    constructor() {
        if (typeof document === "undefined") {
            return;
        }
        this.injectCSS();
        this.createContainer();
    }

    private injectCSS(): void {
        if (document.getElementById("wz-toast-css")) {
            return;
        }
        const style = document.createElement("style");
        style.id = "wz-toast-css";
        style.textContent = CSS;
        document.head.appendChild(style);
    }

    private createContainer(): void {
        this.container = document.createElement("div");
        this.container.className = "wz-toasts";
        // Announce new toasts to screen readers as they're added (not the whole
        // stack), so audible-only cues (TTS replies, alerts) have a text equivalent.
        this.container.setAttribute("aria-live", "polite");
        this.container.setAttribute("aria-atomic", "false");
        document.body.appendChild(this.container);
    }

    /** Display a toast (evicting the oldest at capacity); auto-dismisses after `durationMs`. */
    show(opts: ToastOptions): void {
        const type: ToastType = opts.type ?? "system";
        const theme = THEME[type];
        const duration = opts.durationMs ?? (opts.actions?.length ? 8000 : 5000);

        // Evict oldest if at cap
        if (this.active.length >= this.MAX) {
            this.dismiss(this.active[0]!);
        }

        const el = document.createElement("div");
        el.className = "wz-toast";

        el.innerHTML = `
      <div class="wz-toast__strip" style="background:${theme.strip}"></div>
      <div class="wz-toast__body">
        <div class="wz-toast__avatar" style="background:${theme.avatar}">${escapeHtml(initials(opts.title))}</div>
        <div class="wz-toast__content">
          <div class="wz-toast__header">
            <span class="wz-toast__name">${escapeHtml(opts.title)}</span>
            <span class="wz-toast__badge" style="color:${theme.badge};background:${theme.badgeBg}">${theme.label}</span>
            <span class="wz-toast__time">${timeLabel()}</span>
          </div>
          <div class="wz-toast__message">${escapeHtml(opts.message)}</div>
        </div>
      </div>
      ${opts.actions?.length ? `<div class="wz-toast__actions"></div>` : ""}
      <div class="wz-toast__progress">
        <div class="wz-toast__progress-bar" style="background:${theme.progress}"></div>
      </div>
    `;

        // Action buttons
        if (opts.actions?.length) {
            this._renderActions(el, opts.actions, theme);
        }

        // Click anywhere to dismiss
        el.addEventListener("click", () => this.dismiss(el));

        this.container.appendChild(el);
        this.active.push(el);

        this._animateAndSchedule(el, duration);
    }

    /** Build and wire the action buttons into the toast's actions container. */
    private _renderActions(el: HTMLElement, actions: ToastAction[], theme: ThemeEntry): void {
        const actionsEl = el.querySelector(".wz-toast__actions")!;
        for (const action of actions) {
            const btn = document.createElement("button");
            btn.className = "wz-toast__btn" + (action.primary ? " wz-toast__btn--primary" : "");
            btn.style.cssText = action.primary
                ? `border-color:${theme.badge}55;background:${theme.badgeBg};color:${theme.badge}`
                : "";
            btn.textContent = action.label;
            btn.addEventListener("click", e => {
                e.stopPropagation();
                action.onClick();
                this.dismiss(el);
            });
            actionsEl.appendChild(btn);
        }
    }

    /** Trigger the enter animation, run the progress countdown and schedule auto-dismiss. */
    private _animateAndSchedule(el: HTMLElement, duration: number): void {
        // Animate in (next frame so transition fires)
        requestAnimationFrame(() => {
            requestAnimationFrame(() => el.classList.add("wz-toast--in"));
        });

        // Progress bar countdown
        const bar = el.querySelector<HTMLElement>(".wz-toast__progress-bar")!;
        bar.style.transition = `transform ${duration}ms linear`;
        requestAnimationFrame(() => {
            requestAnimationFrame(() => {
                bar.style.transform = "scaleX(0)";
            });
        });

        // Auto-dismiss
        const timer = window.setTimeout(() => this.dismiss(el), duration);
        el.dataset["timer"] = String(timer);
    }

    private dismiss(el: HTMLElement): void {
        if (!el.isConnected) {
            return;
        }
        clearTimeout(Number(el.dataset["timer"]));
        el.classList.remove("wz-toast--in");
        el.classList.add("wz-toast--out");
        this.active = this.active.filter(t => t !== el);
        el.addEventListener("transitionend", () => el.remove(), { once: true });
        // Safety net if transitionend never fires
        window.setTimeout(() => el.isConnected && el.remove(), 600);
    }
}

/** Shared ToastManager singleton. */
export const toast = new ToastManager();
