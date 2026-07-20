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
import { openLightbox } from "./lightbox";
import { escapeHtml } from "../escapeHtml";

/** Attachments are internally sourced (uploads → same-origin / blob: / data:),
 *  but guard the scheme anyway so a hostile url can't smuggle javascript: into
 *  a src or href. Returns "" for anything outside the allow-list. */
function safeAttachmentUrl(url: string): string {
    try {
        const proto = new URL(url, window.location.origin).protocol;
        return ["http:", "https:", "blob:", "data:"].includes(proto) ? url : "";
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

/** Image thumbnail (click → lightbox) or file chip for one attachment. */
function buildAttachmentEl(att: Attachment): HTMLElement {
    const url = att.url ? safeAttachmentUrl(att.url) : "";
    if (isImage(att) && url) {
        return buildImageThumb(url, att.name);
    }
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
        bubble.appendChild(renderMarkdown(msg.content));
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
        from.textContent = `you${via} · ${new Date(msg.timestampMs).toLocaleTimeString()}`;
    } else {
        from.textContent = surfaceLabel && msg.from === msg.surface ? surfaceLabel : msg.from;
    }

    row.append(from, buildMsgBubble(msg, isUser));
    if (!isUser) {
        const time = document.createElement("div");
        time.className = "af-chat-msg-time";
        time.textContent = new Date(msg.timestampMs).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
        });
        row.append(time);
    }
    return row;
}

/** Placeholder shown when a thread has no messages yet. */
export function buildChatEmptyState(chatTarget: string): HTMLElement {
    const empty = document.createElement("div");
    empty.className = "af-chat-empty";
    empty.innerHTML =
        chatTarget === "main-actor"
            ? `<p>Say hello to <strong>@main-actor</strong> — the system orchestrator.</p>`
            : `<p>No messages with <strong>@${escapeHtml(chatTarget)}</strong> yet.</p>
           <p style="font-size:11px;opacity:0.5">New messages will be sent directly to this agent.</p>`;
    return empty;
}
