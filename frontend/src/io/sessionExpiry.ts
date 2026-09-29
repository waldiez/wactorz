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

/**
 * How many consecutive 503s stop looking like bad luck.
 *
 * Concurrent polls fail together, so three can arrive inside a second — which is
 * why a count alone decides nothing, and {@link REVOKED_AFTER_MS} is what the
 * conclusion actually rests on.
 */
const REVOKED_AFTER_TRIES = 3;

/**
 * How long a run of 503s must last before the token is presumed gone.
 *
 * Everything else that answers 503 recovers: the Supervisor serves a restart in
 * well under a minute, and the app answers "registry not available" only until
 * its registry exists — which, since the UI now binds before the agents start,
 * is a window every restart passes through. A revoked token never recovers, so
 * duration is the only honest discriminator. The margin over an observed boot is
 * deliberately generous: showing this overlay on a healthy install is worse than
 * showing it a minute late on a dead one.
 */
const REVOKED_AFTER_MS = 60_000;

/** The ingress prefix this page was served under, empty when it was not. */
function ingressPath(target: Window): string {
    // Injected on every page, ingress or not, so an empty value is the standalone
    // case rather than a missing one. Truthiness, not `!== undefined`: the latter
    // reads as "under ingress" on a deployment that has no sidebar to send anyone
    // back to.
    return target.__WACTORZ_INGRESS_PATH ?? "";
}

/**
 * Whether a 503 under ingress has gone on long enough to mean the token is gone.
 *
 * A 503 alone says nothing. The Supervisor forwards the app's own status
 * verbatim, so under a live token the app's own 503s -- a command that could not
 * be delivered while the broker is down, a registry that does not exist yet --
 * are the same status as the Supervisor's "I do not know this token". What
 * separates them is that only one of them ever stops.
 *
 * Kept apart from `isSessionGone` because the answers differ. A 401 is fixed by
 * signing in again, and the sign-in page sits under the same working prefix. A
 * revoked token has no working prefix, so sending someone to `/login` under it
 * lands on another 503.
 */
function sessionRevokedTracker(target: Window): (response: Response) => boolean {
    let failures = 0;
    let since = 0;

    return (response: Response): boolean => {
        if (!ingressPath(target)) {
            return false;
        }
        if (response.status !== 503) {
            // Any answer at all proves the prefix still reaches something, so a
            // run of 503s around it was the app being unwell, not the token.
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
 * Replace `fetch` with one that redirects to sign-in on a 401, and says so when
 * an ingress session has been revoked.
 *
 * `onRevoked` is called once, for a caller that holds things this module does
 * not -- the socket, chiefly, which would otherwise go on reconnecting to a
 * prefix that cannot answer.
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
