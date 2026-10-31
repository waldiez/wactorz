/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { requestJson, type Refusal } from "../../io/api";
import { toast } from "../ToastManager";

/**
 * Tell the user a change did not come back as done. An error under `failed`, or,
 * when the timeout ran out, a warning under `unconfirmed`: the server may have
 * made the change anyway, and calling it failed invites doing it twice.
 */
export function sayRefusal(got: Refusal, failed: string, unconfirmed: string): void {
    toast.show({
        type: got.timedOut ? "alert-warning" : "alert-error",
        title: got.timedOut ? unconfirmed : failed,
        message: got.error,
    });
}

/**
 * POST to `url` and tell the user if it didn't take. A refusal needs saying
 * here: these calls all re-render from the server afterwards, which redisplays
 * the unchanged value and looks like it worked. `what` completes "Couldn't …".
 *
 * @returns whether the server said it took.
 */
export async function postOrWarn(url: string, init: RequestInit, what: string): Promise<boolean> {
    const got = await requestJson<unknown>(url, init);
    if (!got.ok) {
        sayRefusal(got, `Couldn't ${what}`, `Unconfirmed: ${what}`);
    }
    return got.ok;
}
