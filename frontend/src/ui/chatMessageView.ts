/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Builds the DOM for a single chat message bubble.
 *
 * Split out of ChatPanel so message presentation (avatars, copy button,
 * markdown body) is a self-contained, side-effect-free concern: each builder
 * returns a detached element the caller appends where it wants.
 */
import type { ChatMessage } from "../types/agent";
import { renderMarkdown } from "./markdown";

/** DiceBear robot URL — instant, no API key needed. */
export function dicebearFor(name: string): string {
    return (
        `https://api.dicebear.com/9.x/bottts-neutral/svg` +
        `?seed=${encodeURIComponent(name)}&backgroundColor=0d1117,111827&radius=50`
    );
}

const COPY_ICON_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;

/** A copy-to-clipboard button that flashes a checkmark on success. */
export function buildCopyBtn(text: string): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.className = "af-chat-copy-btn";
    btn.title = "Copy message";
    btn.innerHTML = COPY_ICON_SVG;
    btn.addEventListener("click", () => {
        navigator.clipboard
            .writeText(text)
            .then(() => {
                btn.textContent = "✓";
                btn.style.color = "#34d399";
                setTimeout(() => {
                    btn.innerHTML = COPY_ICON_SVG;
                    btn.style.color = "";
                }, 2000);
            })
            .catch(() => {});
    });
    return btn;
}

function timeLabel(ms: number): string {
    return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** A user or system message (right-aligned, no avatar). */
function buildSelfMessageEl(msg: ChatMessage): HTMLElement {
    const isSystem = msg.from === "system";
    const el = document.createElement("div");
    el.className = isSystem ? "af-chat-msg af-chat-msg-system" : "af-chat-msg af-chat-msg-user";

    const from = document.createElement("div");
    from.className = "af-chat-msg-from";
    from.textContent = isSystem ? "system" : `you · ${timeLabel(msg.timestampMs)}`;

    const bubble = document.createElement("div");
    bubble.className = "af-chat-msg-bubble";
    bubble.appendChild(renderMarkdown(msg.content));

    el.appendChild(from);
    el.appendChild(bubble);
    return el;
}

/** Avatar + sender-name header (no timestamp) — shared with the live stream row. */
export function buildAvatarHeader(name: string): HTMLElement {
    const header = document.createElement("div");
    header.className = "af-chat-msg-header";

    const avatar = document.createElement("img");
    avatar.className = "af-chat-msg-avatar";
    avatar.src = dicebearFor(name);
    avatar.alt = name;
    avatar.loading = "lazy";

    const from = document.createElement("span");
    from.className = "af-chat-msg-from";
    from.textContent = name;

    header.appendChild(avatar);
    header.appendChild(from);
    return header;
}

/** Avatar + sender name + timestamp row for a finalised agent message. */
function buildAgentHeader(msg: ChatMessage): HTMLElement {
    const header = buildAvatarHeader(msg.from);
    const time = document.createElement("span");
    time.className = "af-chat-msg-time";
    time.textContent = timeLabel(msg.timestampMs);
    header.appendChild(time);
    return header;
}

/** An agent message with avatar, name/time header and a copy button. */
function buildAgentMessageEl(msg: ChatMessage): HTMLElement {
    const wrapper = document.createElement("div");
    wrapper.className = "af-chat-msg af-chat-msg-agent";

    const body = document.createElement("div");
    body.className = "af-chat-msg-body";

    const bubble = document.createElement("div");
    bubble.className = "af-chat-msg-bubble";
    bubble.appendChild(renderMarkdown(msg.content));

    body.appendChild(bubble);
    body.appendChild(buildCopyBtn(msg.content));

    wrapper.appendChild(buildAgentHeader(msg));
    wrapper.appendChild(body);
    return wrapper;
}

/** Build the detached DOM element for a chat message. */
export function buildMessageEl(msg: ChatMessage): HTMLElement {
    const isSelf = msg.from === "user" || msg.from === "system";
    return isSelf ? buildSelfMessageEl(msg) : buildAgentMessageEl(msg);
}
