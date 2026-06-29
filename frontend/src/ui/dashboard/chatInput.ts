/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Chat input behaviour for the iobar textarea: history (↑/↓), ghost-text
 * autosuggestion from history, and the `@mention` panel. Owns all of that state
 * so CardDashboard doesn't have to; it reaches back to the host only for the
 * agent list, the active target, and the send action.
 */

export interface ChatInputHost {
    /** All known agent names (for `@mention` completion). */
    agentNames(): string[];
    /** Set the active chat target (when a mention is accepted). */
    setTarget(name: string): void;
    /** Send the current message. */
    send(input: HTMLTextAreaElement, select: HTMLSelectElement): void;
}

export class ChatInput {
    private history: string[] = [];
    private histIdx = -1;
    private draft = "";
    private suggestion = "";
    private mentionOpen = false;
    private mentionIdx = -1;
    private mentionMatches: string[] = [];

    constructor(private host: ChatInputHost) {}

    /** Handle a keydown on the textarea (mention nav, send, suggestion, history). */
    onKeydown(
        e: KeyboardEvent,
        input: HTMLTextAreaElement,
        select: HTMLSelectElement,
        ghost: HTMLElement,
        mentionPanel: HTMLElement,
    ): void {
        if (this.mentionOpen && this._handleMentionKeydown(e, input, select, ghost, mentionPanel)) {
            return;
        }
        if (this._trySendOnEnter(e, input, select, ghost, mentionPanel)) {
            return;
        }
        // Tab or → at end-of-line accepts the ghost suggestion.
        const atEnd = e.key === "ArrowRight" && input.selectionStart === input.value.length;
        if ((e.key === "Tab" || atEnd) && this.suggestion) {
            e.preventDefault();
            this._acceptSuggestion(input, ghost);
            return;
        }
        if (this._tryHistoryNav(e, input, select, ghost, mentionPanel)) {
            return;
        }
        if (e.key === "Escape") {
            this._clearGhost(input, ghost);
            return;
        }
        if (!["ArrowUp", "ArrowDown", "Tab"].includes(e.key)) {
            this.histIdx = -1;
        }
    }

    /** Handle input changes: open the `@mention` panel or update the ghost suggestion. */
    onChange(
        input: HTMLTextAreaElement,
        select: HTMLSelectElement,
        ghost: HTMLElement,
        mentionPanel: HTMLElement,
    ): void {
        const val = input.value;
        // @mention detection: `@` anywhere at the end of the current word.
        const mentionMatch = /@(\w*)$/.exec(val);
        if (mentionMatch) {
            this._openMentionPanel(mentionMatch[1] ?? "", mentionPanel, select, input, ghost);
            this._clearGhost(input, ghost);
            return;
        }
        this.closePanel(mentionPanel);
        this._updateGhost(input, ghost);
    }

    /** Record a just-sent message into history and clear the ghost/suggestion. */
    recordSent(content: string, input: HTMLTextAreaElement): void {
        this.history.unshift(content);
        if (this.history.length > 50) {
            this.history.pop();
        }
        this.histIdx = -1;
        this.draft = "";
        this.suggestion = "";
        input.classList.remove("has-suggestion");
        // Locate the ghost by class within the input's wrapper rather than by
        // sibling order, so a markup reorder can't silently break clearing.
        const ghost = input.parentElement?.querySelector<HTMLElement>(".af-input-ghost");
        if (ghost) {
            ghost.textContent = "";
        }
    }

    /** Close the `@mention` panel and reset its selection state. */
    closePanel(panel: HTMLElement): void {
        this.mentionOpen = false;
        this.mentionIdx = -1;
        this.mentionMatches = [];
        panel.classList.remove("open");
    }

    private _handleMentionKeydown(
        e: KeyboardEvent,
        input: HTMLTextAreaElement,
        select: HTMLSelectElement,
        ghost: HTMLElement,
        mentionPanel: HTMLElement,
    ): boolean {
        if (e.key === "Escape") {
            e.preventDefault();
            this.closePanel(mentionPanel);
            return true;
        }
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
            e.preventDefault();
            const dir = e.key === "ArrowRight" ? 1 : -1;
            this.mentionIdx = Math.max(-1, Math.min(this.mentionMatches.length - 1, this.mentionIdx + dir));
            this._renderMentionChips(mentionPanel, select, input, ghost);
            return true;
        }
        if (e.key === "Tab" || e.key === "Enter") {
            e.preventDefault();
            const idx = this.mentionIdx >= 0 ? this.mentionIdx : 0;
            this._acceptMention(this.mentionMatches[idx] ?? "", input, select, mentionPanel, ghost);
            return true;
        }
        return false;
    }

    /** Enter (without Shift) sends the message. Returns true if handled. */
    private _trySendOnEnter(
        e: KeyboardEvent,
        input: HTMLTextAreaElement,
        select: HTMLSelectElement,
        ghost: HTMLElement,
        mentionPanel: HTMLElement,
    ): boolean {
        if (e.key !== "Enter" || e.shiftKey) {
            return false;
        }
        e.preventDefault();
        this.closePanel(mentionPanel);
        this.host.send(input, select);
        input.style.height = "auto";
        this._clearGhost(input, ghost);
        return true;
    }

    /** ↑/↓ step through input history. Returns true if handled. */
    private _tryHistoryNav(
        e: KeyboardEvent,
        input: HTMLTextAreaElement,
        select: HTMLSelectElement,
        ghost: HTMLElement,
        mentionPanel: HTMLElement,
    ): boolean {
        if ((e.key !== "ArrowUp" && e.key !== "ArrowDown") || e.shiftKey) {
            return false;
        }
        e.preventDefault();
        if (e.key === "ArrowUp") {
            this._historyUp(input);
        } else {
            this._historyDown(input);
        }
        this.onChange(input, select, ghost, mentionPanel);
        return true;
    }

    private _historyUp(input: HTMLTextAreaElement): void {
        if (this.history.length === 0) {
            return;
        }
        if (this.histIdx === -1) {
            this.draft = input.value;
        }
        this.histIdx = Math.min(this.histIdx + 1, this.history.length - 1);
        input.value = this.history[this.histIdx] ?? "";
        input.setSelectionRange(input.value.length, input.value.length);
    }

    private _historyDown(input: HTMLTextAreaElement): void {
        if (this.histIdx === -1) {
            return;
        }
        this.histIdx--;
        input.value = this.histIdx === -1 ? this.draft : (this.history[this.histIdx] ?? "");
        input.setSelectionRange(input.value.length, input.value.length);
    }

    private _updateGhost(input: HTMLTextAreaElement, ghost: HTMLElement): void {
        const val = input.value;
        if (!val.trim()) {
            this._clearGhost(input, ghost);
            return;
        }
        const lower = val.toLowerCase();
        const match = this.history.find(h => h.toLowerCase().startsWith(lower) && h !== val);
        if (match) {
            this.suggestion = match;
            const typed = document.createElement("span");
            typed.style.color = "transparent";
            typed.textContent = val;
            const tail = document.createElement("span");
            tail.textContent = match.slice(val.length);
            ghost.textContent = "";
            ghost.append(typed, tail);
            input.classList.add("has-suggestion");
        } else {
            this._clearGhost(input, ghost);
        }
    }

    private _clearGhost(input: HTMLTextAreaElement, ghost: HTMLElement): void {
        this.suggestion = "";
        ghost.textContent = "";
        input.classList.remove("has-suggestion");
    }

    private _acceptSuggestion(input: HTMLTextAreaElement, ghost: HTMLElement): void {
        if (!this.suggestion) {
            return;
        }
        input.value = this.suggestion;
        input.setSelectionRange(input.value.length, input.value.length);
        this._clearGhost(input, ghost);
        input.dispatchEvent(new Event("input")); // trigger autoGrow after filling
    }

    private _openMentionPanel(
        query: string,
        panel: HTMLElement,
        select: HTMLSelectElement,
        input: HTMLTextAreaElement,
        ghost: HTMLElement,
    ): void {
        const all = this.host.agentNames();
        this.mentionMatches = query ? all.filter(n => n.toLowerCase().startsWith(query.toLowerCase())) : all;
        if (this.mentionMatches.length === 0) {
            this.closePanel(panel);
            return;
        }
        this.mentionIdx = 0;
        this.mentionOpen = true;
        this._renderMentionChips(panel, select, input, ghost);
        panel.classList.add("open");
    }

    private _renderMentionChips(
        panel: HTMLElement,
        select: HTMLSelectElement,
        input: HTMLTextAreaElement,
        ghost: HTMLElement,
    ): void {
        panel.textContent = "";
        this.mentionMatches.forEach((name, i) => {
            const chip = document.createElement("button");
            chip.className = "af-mention-chip" + (i === this.mentionIdx ? " active" : "");
            chip.textContent = name;
            chip.addEventListener("mousedown", e => {
                e.preventDefault();
                this._acceptMention(name, input, select, panel, ghost);
            });
            panel.appendChild(chip);
        });
    }

    private _acceptMention(
        name: string,
        input: HTMLTextAreaElement,
        select: HTMLSelectElement,
        panel: HTMLElement,
        ghost: HTMLElement,
    ): void {
        if (!name) {
            return;
        }
        // Replace the trailing @query with nothing (target is set via select).
        input.value = input.value.replace(/@\w*$/, "").trimEnd();
        const opt = [...select.options].find(o => o.value === name || o.text === name);
        if (opt) {
            select.value = opt.value;
            this.host.setTarget(opt.value);
        }
        input.placeholder = `Message @${name}…`;
        this.closePanel(panel);
        if (ghost) {
            this._clearGhost(input, ghost);
        }
        input.focus();
        input.dispatchEvent(new Event("input"));
    }
}
