/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The chat input bar (iobar): target <select>, auto-growing textarea wired to
 * the ChatInput controller, an attach button and a send button. Pure DOM
 * construction — all behaviour is routed back through the `IobarDeps`.
 */
import type { ChatInput } from "./chatInput";
import { toast } from "../ToastManager";
import { iconMarkup } from "./icons";
import { uploadsEnabled, uploadFile, ACCEPTED_MIME, ACCEPTED_EXT } from "./uploads";
import { emit, type Listeners } from "../../events";
import { ingressBase } from "../../io/api";
import { el, iconButton, labelled, named } from "../dom";

/** The composer's prompt: it names the recipient, or invites picking one. */
export function composerPlaceholder(target: string): string {
    return target ? `Message @${target}…` : "Message…";
}

/** Have the composer in `root`, once it is there, name `target` as the recipient. */
export function nameRecipient(root: ParentNode, target: string): void {
    const input = root.querySelector<HTMLTextAreaElement>("#af-iobar-input");
    if (input) {
        input.placeholder = composerPlaceholder(target);
    }
}

export interface IobarDeps {
    chatInput: ChatInput;
    /** Holds the bar's listeners for as long as the bar lives: it is built once. */
    listeners: Listeners;
    /** Current chat target (for the placeholder). */
    target(): string;
    /** Set the active target when the <select> changes. */
    setTarget(name: string): void;
    /** Fill the target <select> with messageable agents. */
    populateSelect(select: HTMLSelectElement): void;
    /** Send the current message. */
    send(input: HTMLTextAreaElement): void;
    /** Stop the in-flight generation (POST /chat/stop). */
    stop(): void;
}

function buildTextarea(
    deps: IobarDeps,
    select: HTMLSelectElement,
    ghost: HTMLElement,
    mentionPanel: HTMLElement,
): HTMLTextAreaElement {
    const input = named(el("textarea", "af-iobar-input"), "chat-message", "Chat message", "af-iobar-input");
    input.rows = 1;
    input.placeholder = composerPlaceholder(deps.target());

    // Auto-expand up to MAX_ROWS lines, then scroll. The cap is derived from
    // the computed line-height + padding/border so it tracks the CSS.
    const MAX_ROWS = 10;
    const autoGrow = () => {
        input.style.height = "1px";
        const cs = getComputedStyle(input);
        const line = parseFloat(cs.lineHeight) || 18;
        const extra =
            parseFloat(cs.paddingTop) +
            parseFloat(cs.paddingBottom) +
            parseFloat(cs.borderTopWidth) +
            parseFloat(cs.borderBottomWidth);
        const max = line * MAX_ROWS + extra;
        const h = Math.min(input.scrollHeight, max);
        input.style.height = h + "px";
        input.style.overflowY = input.scrollHeight > max ? "auto" : "hidden";
    };

    input.addEventListener("input", () => {
        autoGrow();
        deps.chatInput.onChange(input, select, ghost, mentionPanel);
    });
    input.addEventListener("keydown", e => deps.chatInput.onKeydown(e, input, select, ghost, mentionPanel));
    input.addEventListener("blur", () => {
        setTimeout(() => deps.chatInput.closePanel(mentionPanel), 150);
    });
    select.addEventListener("change", () => deps.setTarget(select.value));
    return input;
}

function buildSendBtn(
    deps: IobarDeps,
    input: HTMLTextAreaElement,
    mentionPanel: HTMLElement,
): HTMLButtonElement {
    const sendBtn = iconButton(
        "af-send-btn",
        "Send message",
        `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true"><path d="M1 13L13 7 1 1v4.5l8.5 1.5-8.5 1.5V13z" fill="currentColor"/></svg>`,
    );
    sendBtn.addEventListener("click", () => {
        deps.chatInput.closePanel(mentionPanel);
        deps.send(input); // recordSent() clears the ghost
    });
    return sendBtn;
}

/** Stop button: shown only while a turn is streaming; cancels generation. */
function buildStopBtn(deps: IobarDeps): HTMLButtonElement {
    const stopBtn = iconButton(
        "af-stop-btn",
        "Stop generating",
        `<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><rect x="1.5" y="1.5" width="9" height="9" rx="1.5" fill="currentColor"/></svg>`,
    );
    stopBtn.style.display = "none";
    stopBtn.addEventListener("click", () => deps.stop());
    return stopBtn;
}

/**
 * Show Stop while a turn is under way, from what the turn tracker reports
 * (`af-turn`). Send stays usable: a message sent during a turn is queued and
 * goes when it ends (`src/chat/turnTracker.ts`), so there is nothing to block.
 */
function wireTurnState(stopBtn: HTMLButtonElement, listeners: Listeners): void {
    listeners.on("af-turn", d => {
        stopBtn.style.display = d.agent === null ? "none" : "flex";
    });
}

/** The input wrapper (mention panel + ghost + textarea + hint) plus the handles
 *  (input, mentionPanel) the rest of the iobar needs to wire its buttons. */
function buildInputArea(
    deps: IobarDeps,
    select: HTMLSelectElement,
): { inputWrap: HTMLElement; input: HTMLTextAreaElement; mentionPanel: HTMLElement } {
    const inputWrap = el("div", "af-input-wrap");
    const mentionPanel = el("div", "af-mention-panel");
    const ghost = el("div", "af-input-ghost");
    ghost.setAttribute("aria-hidden", "true");
    const input = buildTextarea(deps, select, ghost, mentionPanel);
    deps.chatInput.commands.attach(input);
    const hint = el("div", "af-input-hint", "↑↓ history · Tab accept · @agent · / commands");
    // Pending-attachment chip tray (hidden while empty via CSS); filled by the
    // chat controller from `af-attachment-added` events (drop / paste).
    const tray = el("div", "af-attach-tray");
    tray.id = "af-attach-tray";
    // Always attached, gated inside: the input is built before /api/config has
    // answered, so a check here would decide with an answer nobody has yet.
    input.addEventListener("paste", e => void handlePaste(e));
    inputWrap.append(tray, mentionPanel, deps.chatInput.commands.element, ghost, input, hint);
    return { inputWrap, input, mentionPanel };
}

/** Paste handler: turn clipboard files (e.g. a pasted screenshot) into pending
 *  attachments via the same event the drop zone uses. */
async function handlePaste(e: ClipboardEvent): Promise<void> {
    if (!uploadsEnabled()) {
        return;
    }
    const files = Array.from(e.clipboardData?.files ?? []);
    if (!files.length) {
        return;
    }
    e.preventDefault();
    await attachFiles(files);
}

/** Upload files and offer each as a pending attachment, however it was chosen. */
async function attachFiles(files: File[]): Promise<void> {
    const apiBase = ingressBase();
    for (const file of files) {
        try {
            const attachment = await uploadFile(file, apiBase);
            emit("af-attachment-added", { attachment });
        } catch (err) {
            toast.show({ type: "alert-error", title: "Upload failed", message: String(err) });
        }
    }
}

/** Attach button: the same thing dropping a file does, for choosing one instead.
 *  Dropping needs a window to drop onto and the file already in view, neither of
 *  which holds on a phone or when it sits several folders deep. */
function buildAttachBtn(): { button: HTMLButtonElement; picker: HTMLInputElement } {
    const btn = iconButton("af-attach-btn", "Attach files", iconMarkup("paperclip", 16));

    const picker = el("input");
    picker.type = "file";
    picker.multiple = true;
    // ACCEPTED_MIME holds both prefixes ("image/") and exact types
    // ("application/pdf"); only the former take a wildcard, and "application/pdf*"
    // is not a token any browser accepts.
    picker.accept = [
        ...ACCEPTED_MIME.map(type => (type.endsWith("/") ? `${type}*` : type)),
        ...ACCEPTED_EXT,
    ].join(",");
    picker.hidden = true;
    labelled(picker, "Attach files");
    picker.addEventListener("change", () => {
        void attachFiles(Array.from(picker.files ?? []));
        // Cleared so choosing the same file twice in a row still counts as a
        // change; otherwise the second attempt looks like nothing happened.
        picker.value = "";
    });

    btn.addEventListener("click", () => picker.click());
    return { button: btn, picker };
}

/** Show or hide the attach button under `root`, once /api/config lands. */
export function setAttachVisible(root: ParentNode): void {
    const button = root.querySelector<HTMLElement>(".af-attach-btn");
    if (button) {
        button.hidden = !uploadsEnabled();
    }
}

/** Build the full chat input bar. */
export function buildIobar(deps: IobarDeps): HTMLElement {
    const bar = el("div", "af-iobar");

    const select = named(
        el("select", "af-target-select"),
        "chat-target",
        "Chat target agent",
        "af-target-select",
    );
    deps.populateSelect(select);

    const { inputWrap, input, mentionPanel } = buildInputArea(deps, select);
    bar.append(select, inputWrap);
    const { button, picker } = buildAttachBtn();
    // Always built, shown as the server says: this bar is built once, before
    // /api/config has answered, so a check here would decide with no answer, or
    // the previous visit's. The picker is a sibling rather than a child:
    // interactive content nested inside a button is invalid, hidden or not.
    button.hidden = !uploadsEnabled();
    bar.append(button, picker);
    const sendBtn = buildSendBtn(deps, input, mentionPanel);
    const stopBtn = buildStopBtn(deps);
    wireTurnState(stopBtn, deps.listeners);
    bar.append(sendBtn, stopBtn);
    return bar;
}
