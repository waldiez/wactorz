/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * File-attachment uploads for chat. Validates type/size, then resolves a file to
 * an {@link Attachment}.
 *
 * `uploadsEnabled()` gates the whole attachment UI (drop zone, chip tray, paste).
 * `STUB_UPLOADS` is a dev switch: when on, a file becomes a local object-URL
 * attachment instead of being POSTed — so the compose/preview UX can be built
 * and demoed without the backend `/api/upload` endpoint. It ships off.
 */
import type { Attachment } from "../../types/agent";
import { safeStorage } from "../../safeStorage";
import { uid } from "../../ids";

/** Where the seeded server capability is kept. See `config/serverConfig.ts`. */
export const UPLOADS_KEY = "wactorz-uploads-enabled";

/**
 * Whether the attachment UI (drag-drop + paste) is available.
 *
 * Answered by the server, not by how the bundle was built: the upload routes
 * are only registered when the backend has uploads on, so a build-time flag
 * could only ever guess — and guessing wrong means either a drop zone whose
 * every upload 404s, or a feature hidden from a deployment that has it.
 *
 * Read through a function rather than exported as a const because the value
 * arrives with `/api/config`, after the modules that ask have loaded.
 */
export function uploadsEnabled(): boolean {
    return safeStorage.get(UPLOADS_KEY) === "1";
}

/** Dev-only: set `true` to keep attachments client-side (object URL) instead of
 *  POSTing them, so the compose/preview UX can be demoed with no backend. Ships
 *  as `false`; flip it locally when demoing with no backend. */
const STUB_UPLOADS = false;

/** Accepted MIME-type prefixes. */
export const ACCEPTED_MIME = ["image/", "audio/", "text/", "application/pdf"];
/** Accepted file extensions (checked when the MIME prefix doesn't match). */
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
/** Maximum accepted file size (25 MB per file). */
export const MAX_BYTES = 25 * 1024 * 1024;

/** True when a file's MIME prefix or extension is in the accept-list. */
export function isAccepted(file: File): boolean {
    if (ACCEPTED_MIME.some(prefix => file.type.startsWith(prefix))) {
        return true;
    }
    const name = file.name.toLowerCase();
    return ACCEPTED_EXT.some(ext => name.endsWith(ext));
}

/** Format a byte count compactly as B / KB / MB. */
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
            id: uid("local"),
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
