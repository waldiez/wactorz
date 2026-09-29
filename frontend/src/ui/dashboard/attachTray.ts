/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Renders the pending-attachment chips into the iobar tray (`#af-attach-tray`).
 * Images show as compact `Image #N` tokens; other files show their name. The
 * `×` button routes back through `onRemove`.
 */
import type { Attachment } from "../../types/agent";
import { isImage } from "./uploads";
import { button, el } from "../dom";

/** Render the pending-attachment chips into the iobar tray; `×` routes back through `onRemove`. */
export function renderAttachTray(
    root: HTMLElement,
    attachments: Attachment[],
    onRemove: (att: Attachment) => void,
): void {
    const tray = root.querySelector<HTMLElement>("#af-attach-tray");
    if (!tray) {
        return;
    }
    tray.innerHTML = "";
    attachments.forEach((att, i) => {
        const chip = el("span", "af-attach-chip");
        const label = el("span", "af-attach-chip-label", isImage(att) ? `Image #${i + 1}` : att.name);
        const remove = button("af-attach-chip-x", "×");
        remove.title = "Remove attachment";
        remove.addEventListener("click", () => onRemove(att));
        chip.append(label, remove);
        tray.appendChild(chip);
    });
}

/** Release a dev-stub blob URL, if that is what this attachment holds. */
function revoke(att: Attachment): void {
    if (att.url?.startsWith("blob:")) {
        URL.revokeObjectURL(att.url);
    }
}

/** Drop one attachment from `pending`, returning the remainder. */
export function withoutAttachment(pending: Attachment[], att: Attachment): Attachment[] {
    revoke(att);
    return pending.filter(a => a !== att);
}

/** Discard every pending attachment (send / wipe), releasing their blob URLs. */
export function dropAllAttachments(pending: Attachment[]): Attachment[] {
    pending.forEach(revoke);
    return [];
}
