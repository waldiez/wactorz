/**
 * SPDX-License-Identifier: Apache-2.0
 */
import { apiUrl, requestJson } from "./api";

/** Read the available agent introductions by id, or null if the request failed. */
export async function readIntroductions(): Promise<Record<string, string> | null> {
    const reply = await requestJson<Record<string, string>>(apiUrl("/api/introductions"));
    return reply.ok ? reply.value : null;
}
