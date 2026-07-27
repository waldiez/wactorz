/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { toast } from "../ToastManager";

/**
 * POST to `url` and tell the user if it didn't take.
 *
 * `fetch` only rejects on a transport failure, so a bare `await fetch(...)`
 * treats a 4xx/5xx as success. For state-changing calls that is the difference
 * between "saved" and "silently discarded", and these all re-render from the
 * server afterwards — which redisplays the unchanged value and looks like it
 * worked. `what` completes "Couldn't …".
 *
 * @returns whether the request succeeded.
 */
export async function postOrWarn(url: string, init: RequestInit, what: string): Promise<boolean> {
    try {
        const res = await fetch(url, init);
        if (!res.ok) {
            throw new Error(`HTTP ${res.status}`);
        }
        return true;
    } catch (err) {
        toast.show({
            type: "alert-error",
            title: `Couldn't ${what}`,
            message: `The request did not get through (${String(err)}).`,
        });
        return false;
    }
}
