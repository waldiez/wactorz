/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The chat input bar (iobar): target <select>, auto-growing textarea wired to
 * the ChatInput controller, a voice (STT) mic button and a send button. Pure
 * DOM construction — all behaviour is routed back through the `IobarDeps`.
 */
import type { ChatInput } from "./chatInput";
import type { SpeechToText } from "../../io/SpeechToText";
import { SpeechToText as Stt, STT_ENABLED } from "../../io/SpeechToText";
import { toast } from "../ToastManager";
import { iconMarkup } from "./icons";
import { UPLOADS_ENABLED, uploadFile } from "./uploads";
import { emit, listen } from "../../events";

export interface IobarDeps {
    chatInput: ChatInput;
    stt: SpeechToText;
    /** Current chat target (for the placeholder). */
    target(): string;
    /** Set the active target when the <select> changes. */
    setTarget(name: string): void;
    /** Fill the target <select> with messageable agents. */
    populateSelect(select: HTMLSelectElement): void;
    /** Send the current message. */
    send(input: HTMLTextAreaElement, select: HTMLSelectElement): void;
    /** Stop the in-flight generation (POST /chat/stop). */
    stop(): void;
}

function buildTextarea(
    deps: IobarDeps,
    select: HTMLSelectElement,
    ghost: HTMLElement,
    mentionPanel: HTMLElement,
): HTMLTextAreaElement {
    const input = document.createElement("textarea");
    input.className = "af-iobar-input";
    input.id = "af-iobar-input";
    input.rows = 1;
    input.placeholder = `Message @${deps.target()}…`;

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
    select.addEventListener("change", () => {
        deps.setTarget(select.value);
        input.placeholder = `Message @${select.value}…`;
    });
    return input;
}

function buildSendBtn(
    deps: IobarDeps,
    input: HTMLTextAreaElement,
    select: HTMLSelectElement,
    mentionPanel: HTMLElement,
): HTMLButtonElement {
    const sendBtn = document.createElement("button");
    sendBtn.className = "af-send-btn";
    sendBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M1 13L13 7 1 1v4.5l8.5 1.5-8.5 1.5V13z" fill="currentColor"/></svg>`;
    sendBtn.addEventListener("click", () => {
        deps.chatInput.closePanel(mentionPanel);
        deps.send(input, select); // recordSent() clears the ghost
    });
    return sendBtn;
}

/** Stop button: shown only while a turn is streaming; cancels generation. */
function buildStopBtn(deps: IobarDeps): HTMLButtonElement {
    const stopBtn = document.createElement("button");
    stopBtn.className = "af-stop-btn";
    stopBtn.title = "Stop generating";
    stopBtn.style.display = "none";
    stopBtn.innerHTML = `<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><rect x="1.5" y="1.5" width="9" height="9" rx="1.5" fill="currentColor"/></svg>`;
    stopBtn.addEventListener("click", () => deps.stop());
    return stopBtn;
}

/**
 * Toggle the send/stop buttons across a turn: reveal stop (and disable send) the
 * moment the user sends, restore it once the turn completes. Showing on send —
 * rather than on the first stream chunk — gives a usable stop window even when
 * the model "thinks" for a while before any tokens arrive. A turn ends with
 * `af-stream-end` (streamed reply) or `af-chat-message` (non-streamed reply,
 * e.g. slash commands), both dispatched in direct_ws and mqtt modes.
 */
function wireGenerationLifecycle(sendBtn: HTMLButtonElement, stopBtn: HTMLButtonElement): void {
    const busy = () => {
        sendBtn.disabled = true;
        stopBtn.style.display = "flex";
    };
    const idle = () => {
        sendBtn.disabled = false;
        stopBtn.style.display = "none";
    };
    listen("af-send-message", busy);
    listen("af-stream-end", idle);
    listen("af-chat-message", idle);
}

async function startMic(stt: SpeechToText, btn: HTMLButtonElement): Promise<void> {
    try {
        await stt.start();
        btn.classList.add("recording");
        btn.title = "Stop & transcribe";
    } catch {
        toast.show({ type: "alert-error", title: "Mic blocked", message: "Microphone permission denied." });
    }
}

async function finishMic(
    stt: SpeechToText,
    input: HTMLTextAreaElement,
    btn: HTMLButtonElement,
): Promise<void> {
    btn.classList.remove("recording");
    btn.title = "Voice input";
    try {
        const text = await stt.stopAndTranscribe();
        if (text) {
            input.value = input.value ? `${input.value} ${text}` : text;
            input.dispatchEvent(new Event("input"));
            input.focus();
        }
    } catch (err) {
        toast.show({ type: "alert-error", title: "Transcription failed", message: String(err) });
    }
}

async function toggleMic(
    stt: SpeechToText,
    input: HTMLTextAreaElement,
    btn: HTMLButtonElement,
): Promise<void> {
    if (stt.recording) {
        await finishMic(stt, input, btn);
    } else {
        await startMic(stt, btn);
    }
}

/** Whether the voice mic button should be shown: backend enabled and the
 *  browser can actually capture audio. Otherwise it's simply not rendered. */
function micAvailable(): boolean {
    return STT_ENABLED && Stt.isSupported();
}

/** Mic button: click to record, click again to transcribe into the input. */
function buildMicBtn(stt: SpeechToText, input: HTMLTextAreaElement): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.className = "af-mic-btn";
    btn.title = "Voice input";
    btn.innerHTML = iconMarkup("mic", 16);
    btn.addEventListener("click", () => void toggleMic(stt, input, btn));
    return btn;
}

/** The input wrapper (mention panel + ghost + textarea + hint) plus the handles
 *  (input, mentionPanel) the rest of the iobar needs to wire its buttons. */
function buildInputArea(
    deps: IobarDeps,
    select: HTMLSelectElement,
): { inputWrap: HTMLElement; input: HTMLTextAreaElement; mentionPanel: HTMLElement } {
    const inputWrap = document.createElement("div");
    inputWrap.className = "af-input-wrap";
    const mentionPanel = document.createElement("div");
    mentionPanel.className = "af-mention-panel";
    const ghost = document.createElement("div");
    ghost.className = "af-input-ghost";
    ghost.setAttribute("aria-hidden", "true");
    const input = buildTextarea(deps, select, ghost, mentionPanel);
    const hint = document.createElement("div");
    hint.className = "af-input-hint";
    hint.textContent = "↑↓ history · Tab accept · @agent";
    // Pending-attachment chip tray (hidden while empty via CSS); filled by the
    // chat controller from `af-attachment-added` events (drop / paste).
    const tray = document.createElement("div");
    tray.className = "af-attach-tray";
    tray.id = "af-attach-tray";
    if (UPLOADS_ENABLED) {
        input.addEventListener("paste", e => void handlePaste(e));
    }
    inputWrap.append(tray, mentionPanel, ghost, input, hint);
    return { inputWrap, input, mentionPanel };
}

/** Paste handler: turn clipboard files (e.g. a pasted screenshot) into pending
 *  attachments via the same event the drop zone uses. */
async function handlePaste(e: ClipboardEvent): Promise<void> {
    const files = Array.from(e.clipboardData?.files ?? []);
    if (!files.length) {
        return;
    }
    e.preventDefault();
    const apiBase: string = window.__WACTORZ_INGRESS_PATH ?? "";
    for (const file of files) {
        try {
            const attachment = await uploadFile(file, apiBase);
            emit("af-attachment-added", { attachment });
        } catch (err) {
            toast.show({ type: "alert-error", title: "Upload failed", message: String(err) });
        }
    }
}

/** Build the full chat input bar. */
export function buildIobar(deps: IobarDeps): HTMLElement {
    const bar = document.createElement("div");
    bar.className = "af-iobar";

    const select = document.createElement("select");
    select.className = "af-target-select";
    select.id = "af-target-select";
    select.name = "chat-target";
    select.setAttribute("aria-label", "Chat target agent");
    deps.populateSelect(select);

    const { inputWrap, input, mentionPanel } = buildInputArea(deps, select);
    bar.append(select, inputWrap);
    if (micAvailable()) {
        bar.appendChild(buildMicBtn(deps.stt, input));
    }
    const sendBtn = buildSendBtn(deps, input, select, mentionPanel);
    const stopBtn = buildStopBtn(deps);
    wireGenerationLifecycle(sendBtn, stopBtn);
    bar.append(sendBtn, stopBtn);
    return bar;
}
