/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Pure DOM builders for the dashboard's in-card chat thread: one message row
 * and the empty-state placeholder. Kept side-effect-free (they return detached
 * elements) so the dashboard just appends them where needed.
 */
import type { ChatMessage, Attachment } from "../../types/agent";
import { renderMarkdown } from "../markdown";
import { isImage, humanSize } from "./uploads";
import { iconMarkup } from "./icons";
import { MAIN_AGENT } from "../../agents/naming";
import { openLightbox } from "./lightbox";
import { escapeHtml } from "../escapeHtml";
import { timeLabel } from "../../time";

/** Where a url is about to be used. The allow-list differs by destination, and
 *  a single list for both is what let `data:` reach an anchor href. */
type UrlContext = "media" | "link";

/** Attachments are internally sourced (uploads → same-origin / blob: / data:),
 *  but guard the scheme anyway so a hostile url can't smuggle javascript: into
 *  a src or href. Returns "" for anything outside the allow-list.
 *
 *  `data:` is allowed for `media` and never for `link`: an <img src> renders,
 *  it does not navigate, whereas following a `data:text/html` href runs script
 *  in this origin. Browsers block top-level `data:` navigation today, which is
 *  the only reason this was latent — it is not a guarantee to build on, and
 *  uploads will start putting server-supplied urls here. */
function safeAttachmentUrl(url: string, context: UrlContext): string {
    try {
        const proto = new URL(url, window.location.origin).protocol;
        const allowed =
            context === "media" ? ["http:", "https:", "blob:", "data:"] : ["http:", "https:", "blob:"];
        return allowed.includes(proto) ? url : "";
    } catch {
        return "";
    }
}

/** Clickable thumbnail that opens the lightbox; keyboard-operable (it's a button
 *  in spirit, kept as an <img> for layout). */
function buildImageThumb(url: string, name: string): HTMLImageElement {
    const img = document.createElement("img");
    img.className = "af-chat-attach-thumb";
    img.src = url;
    img.alt = name;
    img.loading = "lazy";
    // Expose it as a button rather than a bare clickable image no AT/keyboard user can reach.
    img.setAttribute("role", "button");
    img.tabIndex = 0;
    img.setAttribute("aria-label", `Open preview: ${name}`);
    const open = (): void => openLightbox(url, name);
    img.addEventListener("click", open);
    img.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            open();
        }
    });
    return img;
}

/** Let an inline image be opened full size, by pointer or keyboard. */
function makeZoomable(img: HTMLImageElement): void {
    const name = img.alt || "image";
    img.setAttribute("role", "button");
    img.tabIndex = 0;
    img.setAttribute("aria-label", `Open preview: ${name}`);
    const open = (): void => openLightbox(img.src, name);
    img.addEventListener("click", open);
    img.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            open();
        }
    });
}

/** Render agent markdown with its inline images wired to the lightbox.
 *
 * The markdown renderer emits inert images on purpose — the lightbox belongs to
 * this layer, and importing it there would pull a dashboard dependency into a
 * generic renderer. That only works if every caller comes through here: a
 * screenshot arriving live is rendered by the streaming path and one by the
 * history path, and for a while only the second one attached the behaviour, so
 * a new image was dead until the page was reloaded.
 */
export function renderAgentMarkdown(text: string): DocumentFragment {
    const rendered = renderMarkdown(text);
    rendered.querySelectorAll("img.af-chat-md-img").forEach(el => {
        makeZoomable(el as HTMLImageElement);
    });
    return rendered;
}

/** Image thumbnail (click → lightbox) or file chip for one attachment. */
function buildAttachmentEl(att: Attachment): HTMLElement {
    if (isImage(att)) {
        const src = att.url ? safeAttachmentUrl(att.url, "media") : "";
        if (src) {
            return buildImageThumb(src, att.name);
        }
    }
    // Not an image (or not renderable): a chip, linked only if the url is safe
    // to navigate to. Anything else degrades to an unlinked <span>.
    const url = att.url ? safeAttachmentUrl(att.url, "link") : "";
    const el = url ? document.createElement("a") : document.createElement("span");
    el.className = "af-chat-attach-file";
    el.innerHTML = `${iconMarkup("file", 13)}<span>${escapeHtml(att.name)} · ${humanSize(att.size)}</span>`;
    if (url && el instanceof HTMLAnchorElement) {
        el.href = url;
        el.target = "_blank";
        el.rel = "noopener";
    }
    return el;
}

/** A row of attachment previews appended below a message's text. */
function buildAttachments(attachments: Attachment[]): HTMLElement {
    const wrap = document.createElement("div");
    wrap.className = "af-chat-attachments";
    attachments.forEach(att => wrap.appendChild(buildAttachmentEl(att)));
    return wrap;
}

/** The bubble body: agent replies render Markdown; user messages stay plain so
 *  stray *asterisks* / backticks they type are never reformatted. */
function buildMsgBubble(msg: ChatMessage, isUser: boolean): HTMLElement {
    const bubble = document.createElement("div");
    bubble.className = "af-chat-msg-bubble";
    if (isUser) {
        bubble.textContent = msg.content;
    } else if (msg.content) {
        bubble.appendChild(renderAgentMarkdown(msg.content));
    }
    if (msg.attachments?.length) {
        bubble.appendChild(buildAttachments(msg.attachments));
    }
    return bubble;
}

/** Build a detached chat message row (user vs agent styling, optional time). */
export function buildChatMessageEl(msg: ChatMessage): HTMLElement {
    const isUser = msg.from === "user";
    const surfaceLabel = msg.source === "voice" ? msg.surfaceLabel?.trim() || msg.surface?.trim() || "" : "";

    const row = document.createElement("div");
    row.className = `af-chat-msg af-chat-msg-${isUser ? "user" : "agent"}`;

    const from = document.createElement("div");
    from.className = "af-chat-msg-from";
    if (isUser) {
        const via = surfaceLabel ? ` · via ${surfaceLabel}` : "";
        from.textContent = `you${via} · ${timeLabel(msg.timestampMs)}`;
    } else {
        from.textContent = surfaceLabel && msg.from === msg.surface ? surfaceLabel : msg.from;
    }

    row.append(from, buildMsgBubble(msg, isUser));
    if (!isUser) {
        const time = document.createElement("div");
        time.className = "af-chat-msg-time";
        time.textContent = timeLabel(msg.timestampMs);
        row.append(time);
    }
    return row;
}

/** Placeholder shown when a thread has no messages yet. */
export function buildChatEmptyState(chatTarget: string): HTMLElement {
    const empty = document.createElement("div");
    empty.className = "af-chat-empty";
    empty.innerHTML =
        chatTarget === MAIN_AGENT
            ? `<p>Say hello to <strong>@main</strong> — the system orchestrator.</p>`
            : `<p>No messages with <strong>@${escapeHtml(chatTarget)}</strong> yet.</p>
           <p style="font-size:11px;opacity:0.5">New messages will be sent directly to this agent.</p>`;
    return empty;
}
