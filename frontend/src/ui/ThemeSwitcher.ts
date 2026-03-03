/**
 * Theme switcher pill (top-right).
 *
 * Listens to clicks on `#btn-graph`, `#btn-galaxy`, `#btn-cards`,
 * `#btn-grave`, then dispatches a `theme-change` CustomEvent on `document`.
 *
 * Persists the last-chosen theme in localStorage ("agentflow-theme").
 * "cards-3d" maps to the same "Cards" button staying active (sub-mode).
 */

import type { ThemeChangeEvent } from "../types/agent";

type ThemeName = "graph" | "galaxy" | "cards" | "cards-3d" | "grave" | "social" | "fin" | "ops";
const STORAGE_KEY = "agentflow-theme";

export class ThemeSwitcher {
  private buttons: Record<"graph" | "galaxy" | "cards" | "grave" | "social" | "fin" | "ops", HTMLButtonElement>;
  private current: ThemeName = "social";

  constructor() {
    this.buttons = {
      graph:  document.getElementById("btn-graph")  as HTMLButtonElement,
      galaxy: document.getElementById("btn-galaxy") as HTMLButtonElement,
      cards:  document.getElementById("btn-cards")  as HTMLButtonElement,
      grave:  document.getElementById("btn-grave")  as HTMLButtonElement,
      social: document.getElementById("btn-social") as HTMLButtonElement,
      fin:    document.getElementById("btn-fin")    as HTMLButtonElement,
      ops:    document.getElementById("btn-ops")    as HTMLButtonElement,
    };

    (Object.keys(this.buttons) as ("graph" | "galaxy" | "cards" | "grave" | "social" | "fin" | "ops")[]).forEach((name) => {
      this.buttons[name].addEventListener("click", () => this.switchTo(name));
    });

    // Mark the default (social) button active immediately
    this.updateButtons();

    // Mobile always stays on Social. Desktop restores saved preference.
    const isMobile = window.innerWidth < 640;
    const saved = localStorage.getItem(STORAGE_KEY) as ThemeName | null;
    if (!isMobile && saved && saved !== "social") {
      setTimeout(() => this.switchTo(saved), 0);
    }
  }

  /** Switch to a theme and persist. */
  switchTo(theme: ThemeName): void {
    if (this.current === theme) return;
    this.current = theme;
    localStorage.setItem(STORAGE_KEY, theme);
    this.updateButtons();
    document.dispatchEvent(
      new CustomEvent<ThemeChangeEvent>("theme-change", { detail: { theme } }),
    );
  }

  /**
   * Sync internal state when another component (e.g. CardDashboard) changes
   * the theme without going through this switcher — updates buttons + storage
   * without dispatching a new event.
   */
  syncState(theme: ThemeName): void {
    if (this.current === theme) return;
    this.current = theme;
    localStorage.setItem(STORAGE_KEY, theme);
    this.updateButtons();
  }

  private updateButtons(): void {
    this.buttons.graph.classList.toggle("active",  this.current === "graph");
    this.buttons.galaxy.classList.toggle("active", this.current === "galaxy");
    // Both html-cards and babylon-cards highlight the Cards button
    this.buttons.cards.classList.toggle("active",
      this.current === "cards" || this.current === "cards-3d");
    this.buttons.grave.classList.toggle("active",  this.current === "grave");
    this.buttons.social.classList.toggle("active", this.current === "social");
    this.buttons.fin.classList.toggle("active",    this.current === "fin");
    this.buttons.ops.classList.toggle("active",    this.current === "ops");

    // ARIA: update aria-selected for all tab buttons
    (Object.keys(this.buttons) as ("graph" | "galaxy" | "cards" | "grave" | "social" | "fin" | "ops")[]).forEach((name) => {
      const active = name === "cards"
        ? (this.current === "cards" || this.current === "cards-3d")
        : this.current === name;
      this.buttons[name].setAttribute("aria-selected", String(active));
    });
  }
}
