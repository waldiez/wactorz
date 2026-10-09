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
import { WebSpeech } from "../../io/WebSpeech";
import {
    currentSupport,
    micOffered,
    resolveVoiceEngine,
    unavailableReason,
    voiceMode,
    type VoiceEngine,
} from "../../io/voiceInput";
import { toast } from "../ToastManager";
import { iconMarkup } from "./icons";
import { uploadsEnabled, uploadFile, ACCEPTED_MIME, ACCEPTED_EXT } from "./uploads";
import { emit, listen } from "../../events";
import { el, iconButton, named } from "../dom";

/** The composer's prompt: it names the recipient, or invites picking one. */
export function composerPlaceholder(target: string): string {
    return target ? `Message @${target}…` : "Message…";
}

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
 * How long a turn may go completely silent before the composer frees itself.
 * Every stream chunk restarts the clock, so this only fires when nothing at all
 * is arriving — a slow model still holding the line keeps its turn.
 */
export const TURN_IDLE_TIMEOUT_MS = 60_000;

/**
 * Toggle the send/stop buttons across a turn: reveal stop (and disable send) the
 * moment the user sends, restore it once the turn completes. Showing on send —
 * rather than on the first stream chunk — gives a usable stop window even when
 * the model "thinks" for a while before any tokens arrive. A turn ends with
 * `af-stream-end` (streamed reply) or `af-chat-message` (non-streamed reply,
 * e.g. slash commands), both dispatched in direct_ws and mqtt modes.
 *
 * A turn belongs to the agent it was sent to. Both terminating events also fire
 * for traffic the user had no part in — agent-to-agent chatter arrives as
 * `af-chat-message` like any other frame — so they only end the turn when they
 * come from its target. The key is the *sender*: a genuine reply is addressed
 * `to: "user"` just as a bystander's message may be, so `to` cannot distinguish
 * them.
 *
 * Neither event arrives at all if the backend dies mid-turn, which would strand
 * the composer with send disabled and stop inert. Two independent escapes: the
 * transport dropping out of `live`, and a silence timeout as the backstop for a
 * terminating frame that is simply lost on a connection that still looks fine.
 */
function wireGenerationLifecycle(sendBtn: HTMLButtonElement, stopBtn: HTMLButtonElement): void {
    let turnTarget: string | null = null;
    let silenceTimer: ReturnType<typeof setTimeout> | undefined;

    const idle = () => {
        turnTarget = null;
        clearTimeout(silenceTimer);
        sendBtn.disabled = false;
        stopBtn.style.display = "none";
    };
    const armSilenceTimer = () => {
        clearTimeout(silenceTimer);
        silenceTimer = setTimeout(idle, TURN_IDLE_TIMEOUT_MS);
    };
    const busy = (target: string | undefined) => {
        turnTarget = target ?? null;
        sendBtn.disabled = true;
        stopBtn.style.display = "flex";
        armSilenceTimer();
    };
    /** Whether `from` is the agent this turn is waiting on. */
    const endsTurn = (from: string | undefined) => turnTarget === null || from === turnTarget;

    // Page-lifetime listeners: buildIobar runs once (CardDashboard is a single
    // instance never remounted), so these are intentionally not removed. If that
    // assumption ever changes, a second call here double-fires busy/idle.
    listen("af-send-message", d => busy(d?.target));
    listen("af-stream-end", d => {
        if (endsTurn(d?.from)) {
            idle();
        }
    });
    listen("af-chat-message", d => {
        if (endsTurn(d?.msg?.from)) {
            idle();
        }
    });
    // Tokens are proof the turn is alive, but not that it is over.
    listen("af-stream-chunk", d => {
        if (turnTarget !== null && endsTurn(d?.from)) {
            armSilenceTimer();
        }
    });
    listen("af-connection-status", d => {
        if (d?.status !== "live") {
            idle();
        }
    });
}

/** Put the mic back to its resting look. */
function micIdle(btn: HTMLButtonElement): void {
    btn.classList.remove("recording");
    btn.title = "Voice input";
    btn.setAttribute("aria-label", "Voice input");
}

/** Show that the mic is live, and which engine is listening. */
function micLive(btn: HTMLButtonElement, engine: VoiceEngine): void {
    btn.classList.add("recording");
    const label = engine === "browser" ? "Stop listening" : "Stop & transcribe";
    btn.title = label;
    btn.setAttribute("aria-label", label);
}

/** Put recognized text in the composer after whatever was already typed. */
function writeTranscript(input: HTMLTextAreaElement, prefix: string, text: string): void {
    input.value = prefix ? `${prefix} ${text}` : text;
    input.dispatchEvent(new Event("input"));
}

/** What a Web Speech error means for the person, or null when it needs no word. */
export function webSpeechErrorMessage(code: string): string | null {
    switch (code) {
        case "no-speech":
        case "aborted":
            return null;
        case "not-allowed":
        case "service-not-allowed":
            return "Microphone permission denied, or the page is not on localhost or HTTPS.";
        case "network":
            return "The browser's speech service could not be reached.";
        default:
            return `Speech recognition stopped: ${code}`;
    }
}

async function startServerMic(stt: SpeechToText, btn: HTMLButtonElement): Promise<boolean> {
    try {
        await stt.start();
        micLive(btn, "server");
        return true;
    } catch {
        toast.show({ type: "alert-error", title: "Mic blocked", message: "Microphone permission denied." });
        return false;
    }
}

async function finishServerMic(
    stt: SpeechToText,
    input: HTMLTextAreaElement,
    btn: HTMLButtonElement,
): Promise<void> {
    micIdle(btn);
    try {
        const text = await stt.stopAndTranscribe();
        if (text) {
            writeTranscript(input, input.value.trim(), text);
            input.focus();
        }
    } catch (err) {
        toast.show({
            type: "alert-error",
            title: "Transcription failed",
            message: err instanceof Error ? err.message : String(err),
        });
    }
}

/** Listen through the browser's recogniser, writing hypotheses as they arrive. */
function startBrowserMic(
    speech: WebSpeech,
    input: HTMLTextAreaElement,
    btn: HTMLButtonElement,
    onEnd: () => void,
): void {
    // Hypotheses are revised as speech continues, so each one replaces the last
    // rather than appending; only text typed before listening is kept in front.
    const prefix = input.value.trim();
    micLive(btn, "browser");
    speech.start(
        text => writeTranscript(input, prefix, text.trim()),
        () => {
            micIdle(btn);
            input.focus();
            onEnd();
        },
        code => {
            const message = webSpeechErrorMessage(code);
            if (message) {
                toast.show({ type: "alert-error", title: "Voice input", message });
            }
        },
    );
}

/**
 * Mic button. The engine is decided at each click from the voice-input setting
 * and what the browser and server can do, since `/api/config` may answer after
 * the bar is built. Browser: listen until a pause or a second click. Server:
 * record until the second click, then transcribe.
 */
function buildMicBtn(stt: SpeechToText, input: HTMLTextAreaElement): HTMLButtonElement {
    const btn = iconButton("af-mic-btn", "Voice input", iconMarkup("mic", 16));
    const speech = new WebSpeech();
    let active: VoiceEngine | null = null;

    btn.addEventListener("click", () => {
        if (active === "browser") {
            speech.stop();
            return;
        }
        if (active === "server") {
            active = null;
            void finishServerMic(stt, input, btn);
            return;
        }
        const mode = voiceMode();
        const support = currentSupport();
        const engine = resolveVoiceEngine(mode, support);
        if (!engine) {
            toast.show({
                type: "alert-warning",
                title: "Voice input",
                message: unavailableReason(mode, support),
            });
            return;
        }
        active = engine;
        if (engine === "browser") {
            startBrowserMic(speech, input, btn, () => {
                active = null;
            });
            return;
        }
        void startServerMic(stt, btn).then(started => {
            if (!started) {
                active = null;
            }
        });
    });

    // Shown or hidden as the setting changes; page-lifetime, like the bar.
    btn.hidden = !micOffered(voiceMode(), currentSupport());
    listen("af-voice-mode", d => {
        btn.hidden = !micOffered(d.mode, currentSupport());
    });
    return btn;
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
    const hint = el("div", "af-input-hint", "↑↓ history · Tab accept · @agent");
    // Pending-attachment chip tray (hidden while empty via CSS); filled by the
    // chat controller from `af-attachment-added` events (drop / paste).
    const tray = el("div", "af-attach-tray");
    tray.id = "af-attach-tray";
    // Always attached, gated inside: the input is built before /api/config has
    // answered, so a check here would decide with an answer nobody has yet.
    input.addEventListener("paste", e => void handlePaste(e));
    inputWrap.append(tray, mentionPanel, ghost, input, hint);
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
    picker.addEventListener("change", () => {
        void attachFiles(Array.from(picker.files ?? []));
        // Cleared so choosing the same file twice in a row still counts as a
        // change; otherwise the second attempt looks like nothing happened.
        picker.value = "";
    });

    btn.addEventListener("click", () => picker.click());
    return { button: btn, picker };
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
    if (uploadsEnabled()) {
        const { button, picker } = buildAttachBtn();
        // The picker is a sibling rather than a child: interactive content
        // nested inside a button is invalid, hidden or not.
        bar.append(button, picker);
    }
    const support = currentSupport();
    if (support.browser || support.recorder) {
        bar.appendChild(buildMicBtn(deps.stt, input));
    }
    const sendBtn = buildSendBtn(deps, input, mentionPanel);
    const stopBtn = buildStopBtn(deps);
    wireGenerationLifecycle(sendBtn, stopBtn);
    bar.append(sendBtn, stopBtn);
    return bar;
}
