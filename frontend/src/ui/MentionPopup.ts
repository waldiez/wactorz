/**
 * @mention autocomplete popup for the IO bar.
 *
 * Triggered when the user types `@` in the textarea.
 * Lists agents whose names start with the typed partial name.
 * Arrow keys + Enter / Tab select; Esc dismisses.
 * On selection, replaces `@partial` in the input with `@agent-name `.
 */

import type { AgentInfo } from "../types/agent";

export class MentionPopup {
  private popup: HTMLUListElement;
  private input: HTMLTextAreaElement;
  private getAgents: () => AgentInfo[];
  private focusedIdx = -1;

  constructor(input: HTMLTextAreaElement, getAgents: () => AgentInfo[]) {
    this.input = input;
    this.getAgents = getAgents;

    this.popup = document.createElement("ul");
    this.popup.id = "mention-popup";
    document.body.appendChild(this.popup);

    this.bindEvents();
  }

  private bindEvents(): void {
    this.input.addEventListener("input", () => this.onInput());
    this.input.addEventListener("keydown", (e) => this.onKeyDown(e), true);
    document.addEventListener("click", (e) => {
      if (!this.popup.contains(e.target as Node) && e.target !== this.input) {
        this.hide();
      }
    });
  }

  private onInput(): void {
    const { partial } = this.getMentionContext();
    if (partial === null) {
      this.hide();
      return;
    }
    this.show(partial);
  }

  private onKeyDown(e: KeyboardEvent): void {
    if (!this.isVisible()) return;

    const items = this.popup.querySelectorAll<HTMLLIElement>("li");
    if (e.key === "ArrowDown") {
      e.preventDefault();
      e.stopPropagation();
      this.focusedIdx = Math.min(this.focusedIdx + 1, items.length - 1);
      this.updateFocus(items);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      e.stopPropagation();
      this.focusedIdx = Math.max(this.focusedIdx - 1, 0);
      this.updateFocus(items);
    } else if (e.key === "Enter" || e.key === "Tab") {
      if (this.focusedIdx >= 0 && this.focusedIdx < items.length) {
        e.preventDefault();
        e.stopPropagation();
        const name = items[this.focusedIdx]?.dataset["name"];
        if (name) this.select(name);
      }
    } else if (e.key === "Escape") {
      this.hide();
    }
  }

  private show(partial: string): void {
    const agents = this.getAgents().filter((a) =>
      a.name.toLowerCase().startsWith(partial.toLowerCase()),
    );

    if (agents.length === 0) {
      this.hide();
      return;
    }

    this.focusedIdx = 0;
    this.popup.innerHTML = "";
    for (const agent of agents) {
      const li = document.createElement("li");
      li.textContent = `@${agent.name}`;
      li.dataset["name"] = agent.name;
      li.addEventListener("mousedown", (e) => {
        e.preventDefault(); // keep focus on input
        this.select(agent.name);
      });
      this.popup.appendChild(li);
    }

    // Position above the IO bar
    const inputRect = this.input.getBoundingClientRect();
    this.popup.style.left = `${inputRect.left}px`;
    this.popup.style.bottom = `${window.innerHeight - inputRect.top + 4}px`;
    this.popup.style.width = `${Math.max(inputRect.width * 0.6, 200)}px`;
    this.popup.style.display = "block";

    this.updateFocus(this.popup.querySelectorAll<HTMLLIElement>("li"));
  }

  private hide(): void {
    this.popup.style.display = "none";
    this.focusedIdx = -1;
  }

  private isVisible(): boolean {
    return this.popup.style.display === "block";
  }

  private updateFocus(items: NodeListOf<HTMLLIElement>): void {
    items.forEach((li, i) => {
      li.classList.toggle("focused", i === this.focusedIdx);
    });
  }

  private select(name: string): void {
    const { atPos } = this.getMentionContext();
    if (atPos === null) {
      this.hide();
      return;
    }
    const before = this.input.value.slice(0, atPos);
    const after = this.input.value.slice(this.input.selectionStart);
    this.input.value = `${before}@${name} ${after}`;
    // Place cursor after the inserted mention
    const cursor = before.length + name.length + 2;
    this.input.setSelectionRange(cursor, cursor);
    this.input.dispatchEvent(new Event("input")); // trigger autoGrow
    this.hide();
  }

  /**
   * Returns the position of the triggering `@` and the partial name typed
   * after it, or `{ atPos: null, partial: null }` if not in a mention.
   */
  private getMentionContext(): { atPos: number | null; partial: string | null } {
    const val = this.input.value;
    const cursor = this.input.selectionStart ?? val.length;
    const before = val.slice(0, cursor);
    const match = before.match(/@(\w*)$/);
    if (!match) return { atPos: null, partial: null };
    const atPos = before.lastIndexOf("@");
    return { atPos, partial: match[1] ?? "" };
  }
}
