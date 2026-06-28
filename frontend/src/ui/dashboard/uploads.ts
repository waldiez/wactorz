/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * File-attachment uploads for chat. Validates type/size, then resolves a file to
 * an {@link Attachment}.
 *
 * `UPLOADS_ENABLED` gates the whole attachment UI (drop zone, chip tray, paste).
 * `STUB_UPLOADS` is a dev switch: when on, a file becomes a local object-URL
 * attachment instead of being POSTed — so the compose/preview UX can be built
 * and demoed before the backend `/api/upload` endpoint exists. In production both
 * the endpoint and `UPLOADS_ENABLED` go live and `STUB_UPLOADS` stays off.
 */
import type { Attachment } from "../../types/agent";

/**
 * Whether the attachment UI (drag-drop + paste) is shown. Off by default;
 * enable per-deploy at build time with `VITE_UPLOADS_ENABLED=true` once the
 * `/api/upload` backend is live.
 */
export const UPLOADS_ENABLED = import.meta.env["VITE_UPLOADS_ENABLED"] === "true";

/** Dev-only: set `true` to keep attachments client-side (object URL) instead of
 *  POSTing them, so the compose/preview UX can be demoed with no backend. Ships
 *  as `false`; flip it locally alongside `UPLOADS_ENABLED` when demoing offline. */
const STUB_UPLOADS = false;

/** Accepted MIME prefixes and file extensions. */
export const ACCEPTED_MIME = ["image/", "audio/", "video/", "text/", "application/pdf"];
export const ACCEPTED_EXT = [
    ".pdf",
    ".txt",
    ".md",
    ".csv",
    ".ppt",
    ".pptx",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".json",
];
export const MAX_BYTES = 25 * 1024 * 1024; // 25 MB per file

export function isAccepted(file: File): boolean {
    if (ACCEPTED_MIME.some(prefix => file.type.startsWith(prefix))) {
        return true;
    }
    const name = file.name.toLowerCase();
    return ACCEPTED_EXT.some(ext => name.endsWith(ext));
}

export function humanSize(bytes: number): string {
    if (bytes < 1024) {
        return `${bytes} B`;
    }
    if (bytes < 1024 * 1024) {
        return `${(bytes / 1024).toFixed(0)} KB`;
    }
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Whether an attachment is a previewable image. */
export function isImage(att: Attachment): boolean {
    return att.mime.startsWith("image/");
}

let _localSeq = 0;

/**
 * Validate and upload `file`, resolving to its {@link Attachment}. Throws on a
 * type/size violation. With `STUB_UPLOADS` the file stays client-side (object
 * URL + local id); otherwise it is POSTed to `/api/upload` and the server's
 * id/url are used.
 */
export async function uploadFile(file: File, apiBase = ""): Promise<Attachment> {
    if (!isAccepted(file)) {
        throw new Error(`Unsupported file type: ${file.name}`);
    }
    if (file.size > MAX_BYTES) {
        throw new Error(`${file.name} is larger than ${humanSize(MAX_BYTES)}`);
    }
    if (STUB_UPLOADS) {
        return {
            id: `local-${Date.now()}-${_localSeq++}`,
            name: file.name,
            mime: file.type,
            size: file.size,
            url: URL.createObjectURL(file),
        };
    }
    const body = new FormData();
    body.append("file", file, file.name);
    const res = await fetch(`${apiBase}/api/upload`, { method: "POST", body });
    if (!res.ok) {
        throw new Error(`Upload failed (${res.status})`);
    }
    const data = (await res.json()) as { id: string; name?: string; mime?: string; size?: number };
    return {
        id: data.id,
        name: data.name ?? file.name,
        mime: data.mime ?? file.type,
        size: data.size ?? file.size,
        url: `${apiBase}/api/upload/${encodeURIComponent(data.id)}`,
    };
}
