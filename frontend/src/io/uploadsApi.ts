/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Sending a chat attachment to the server, `POST /api/upload`.
 */
import { requestJson, type Answer } from "./api";

/** How long an upload may take: a large file on a phone's connection is slow, and healthy. */
export const UPLOAD_TIMEOUT_MS = 300_000;

/** What the server says it stored. */
export interface StoredUpload {
    /** The stored file's id. */
    id: string;
    /** Its name, when the server changed it. */
    name?: string;
    /** Its type, as the server read it. */
    mime?: string;
    /** Its size in bytes. */
    size?: number;
}

/** Send `file` to the server at `apiBase`. */
export function postUpload(file: File, apiBase: string): Promise<Answer<StoredUpload>> {
    const body = new FormData();
    body.append("file", file, file.name);
    return requestJson<StoredUpload>(`${apiBase}/api/upload`, { method: "POST", body }, UPLOAD_TIMEOUT_MS);
}
