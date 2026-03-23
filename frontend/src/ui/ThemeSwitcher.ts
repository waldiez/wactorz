/**
 * Theme switcher pill (top-right).
 *
 * Listens to clicks on `#btn-graph`, `#btn-galaxy`, `#btn-cards`,
 * `#btn-grave`, then dispatches a `theme-change` CustomEvent on `document`.
 *
 * Persists the last-chosen theme in localStorage ("wactorz-theme").
 * "cards-3d" maps to the same "Cards" button staying active (sub-mode).
 */

import type { ThemeChangeEvent } from "../types/agent";

export type ThemeName =
  | "graph"
  | "galaxy"
  | "cards"
  | "grave"
  | "social"
  | "fin";
const STORAGE_KEY = "wactorz-theme";

export class ThemeSwitcher {
  private buttons: Partial<
    Record<ThemeName, HTMLButtonElement | undefined | null>
  >;
  private current: ThemeName = "cards";

  constructor() {
    const get = (id: string) =>
      (document.getElementById(id) as HTMLButtonElement | undefined | null) ??
      undefined;

    this.buttons = {
      graph: get("btn-graph"),
      galaxy: get("btn-galaxy"),
      cards: get("btn-cards"),
      grave: get("btn-grave"),
      social: get("btn-social"),
      fin: get("btn-fin"),
    };

    (Object.keys(this.buttons) as ThemeName[]).forEach((name) => {
      this.buttons[name]?.addEventListener("click", () => this.switchTo(name));
    });

    // Restore saved preference; default is "cards".
    const saved = localStorage.getItem(STORAGE_KEY) as ThemeName | null;
    if (saved && saved !== this.current) {
      setTimeout(() => this.switchTo(saved), 0);
    } else {
      // Kick off the default theme
      setTimeout(() => {
        document.dispatchEvent(
          new CustomEvent<ThemeChangeEvent>("theme-change", { detail: { theme: this.current } }),
        );
      }, 0);
    }
    this.updateButtons();
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
    this.buttons.graph?.classList.toggle("active", this.current === "graph");
    this.buttons.galaxy?.classList.toggle("active", this.current === "galaxy");
    this.buttons.cards?.classList.toggle("active", this.current === "cards");
    this.buttons.grave?.classList.toggle("active", this.current === "grave");
    this.buttons.social?.classList.toggle("active", this.current === "social");
    this.buttons.fin?.classList.toggle("active", this.current === "fin");
  }
}
