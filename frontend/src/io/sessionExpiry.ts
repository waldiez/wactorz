/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Send the page to sign in again when its session has gone.
 *
 * A session lasts a long time but not forever, and the server restarting ends
 * every one of them. When that happens mid-visit the dashboard is still on
 * screen and still polling — so without this it simply stops changing: cards
 * freeze at their last values, the feed stops, and nothing says why. A page
 * that looks alive and is not is worse than one that plainly asks you to sign
 * in.
 *
 * Installed once over `fetch` rather than added to each caller. There are a
 * dozen call sites across the views and more arrive with each feature, so the
 * per-caller version protects whichever ones its author remembered — the same
 * reasoning that puts the server's own key check in middleware instead of on
 * each route.
 */
import { log } from "./logger";
import { showDeadSession } from "../ui/deadSession";

/** Where the server's sign-in page lives, under any ingress prefix. */
function loginUrl(target: Window): string {
    // `target`, not the global: the two are the same in the running app, and
    // reading one while redirecting the other is the kind of difference that
    // only shows up as a test that cannot be written.
    const base = window.__WACTORZ_INGRESS_PATH ?? "";
    const here = target.location.pathname + target.location.search;
    return `${base}/login?next=${encodeURIComponent(here)}`;
}

/**
 * Whether a response means "your session is gone" rather than "you may not do
 * that".
 *
 * Only 401. A 403 is the origin check refusing a request, and answering that
 * with a sign-in page would send someone to log in over something logging in
 * cannot fix.
 */
function isSessionGone(response: Response): boolean {
    return response.status === 401;
}

const REVOKED_AFTER_TRIES = 3;
const REVOKED_AFTER_MS = 60_000;

/** Return whether this page was served through a Home Assistant ingress URL. */
function ingressPath(target: Window): string {
    return target.__WACTORZ_INGRESS_PATH ?? "";
}

/** Track a sustained ingress failure without mistaking an add-on restart for one. */
function sessionRevokedTracker(target: Window): (response: Response) => boolean {
    let failures = 0;
    let since = 0;
    return (response: Response): boolean => {
        if (!ingressPath(target)) {
            return false;
        }
        if (response.status !== 503) {
            failures = 0;
            return false;
        }
        failures += 1;
        if (failures === 1) {
            since = Date.now();
        }
        return failures >= REVOKED_AFTER_TRIES && Date.now() - since >= REVOKED_AFTER_MS;
    };
}

/**
 * Replace `fetch` with one that redirects to sign-in on a 401.
 *
 * Returns a function that puts the original back, for tests and for a teardown
 * that wants the global left as it found it.
 */
export function installSessionExpiry(target: Window = window, onRevoked: () => void = () => {}): () => void {
    // Kept unbound so releasing puts back the very function that was there —
    // a bound copy behaves the same and is not the same object, which makes
    // "restore" quietly untrue for anyone comparing. The lint rule guards
    // against losing `this` by accident; here it is supplied explicitly on
    // every call below, which is the case the rule cannot see.
    // eslint-disable-next-line @typescript-eslint/unbound-method
    const original = target.fetch;
    // Concurrent polls all fail together when a session ends, so without this
    // the first navigation is cancelled by the second and the address bar can
    // end up somewhere between the two.
    let redirecting = false;
    const revoked = sessionRevokedTracker(target);

    target.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const response = await original.call(target, input, init);
        if (isSessionGone(response) && !redirecting) {
            redirecting = true;
            log.warn("[auth] session expired — returning to sign-in");
            target.location.assign(loginUrl(target));
        } else if (revoked(response) && showDeadSession()) {
            log.warn("[auth] ingress session revoked — this page cannot recover");
            onRevoked();
        }
        return response;
    };

    return () => {
        target.fetch = original;
    };
}
