/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The open view in the address, so a refresh returns to it, a link opens it,
 * and Back steps through the views visited.
 *
 * In the hash (`#/feed`, `#/chat/weather`) rather than the path: the browser
 * never sends it to the server, so it needs no route there, survives any
 * prefix the dashboard is served under (Home Assistant's ingress, a proxy),
 * and the sign-in redirect is untouched.
 *
 * Where the address has no hash — Home Assistant rebuilding its frame on a
 * refresh, the redirect after signing in — the view last open in this browser
 * is used instead.
 *
 * This module talks to the dashboard only through the event bus: it emits
 * `af-route` when the address changes, and writes the address when it hears
 * `af-view-changed`.
 */
import { emit, listen } from "./events";
import { safeStorage } from "./safeStorage";

/**
 * Where the dashboard is: a view, and where within it.
 *
 * The router knows no view: `path` is the view's own to read, one level per
 * segment, as many as it has. The chat reads `path[0]` as the agent whose
 * conversation is open (`#/chat/weather`); a view with more levels reads more
 * (`#/models/hvac/versions/7`).
 */
export interface Route {
    /** The view's key: a built-in one, or an extension's. */
    view: string;
    /** Where within the view, outermost first; empty for the view itself. */
    path: string[];
}

/** The browser's memory of the last view, for an address with no hash. */
export const VIEW_KEY = "wactorz-view";

/** The view a first visit opens. */
export const DEFAULT_ROUTE: Route = { view: "overview", path: [] };

/** A view key: what a built-in or an extension's view can be called. */
const VIEW_NAME = /^[a-z][a-z0-9-]*$/;

/** Read a route from an address's hash; null when it names no view. */
export function parseRoute(hash: string): Route | null {
    const [view, ...rest] = hash.replace(/^#\/?/, "").split("/");
    if (!view || !VIEW_NAME.test(view)) {
        return null;
    }
    const path: string[] = [];
    for (const segment of rest) {
        if (!segment) {
            break;
        }
        try {
            path.push(decodeURIComponent(segment));
        } catch {
            // A malformed escape in a hand-typed address: what came before it still holds.
            break;
        }
    }
    return { view, path };
}

/** Write a route as a hash, each level escaped, so a name with a `/` in it stays one level. */
export function formatRoute(route: Route): string {
    return `#/${[route.view, ...route.path.map(encodeURIComponent)].join("/")}`;
}

/** Where to open: the address's route, else the last view open here, else the overview. */
export function initialRoute(): Route {
    return parseRoute(location.hash) ?? parseRoute(`#/${safeStorage.get(VIEW_KEY) ?? ""}`) ?? DEFAULT_ROUTE;
}

/**
 * Keep the address and the open view in step, until the returned function is called.
 *
 * The first view the dashboard reports replaces the current history entry, so
 * opening the page adds nothing to Back; every change after it adds one.
 *
 * Back and Forward fire `popstate`, and a hash change `hashchange`; browsers
 * fire one, or both, depending on how the entry was made. Both are heard, and
 * an address already handled is not handled twice.
 */
export function startRouter(): () => void {
    let landed = false;
    let handled = location.hash;
    const onAddress = (): void => {
        if (location.hash === handled) {
            return;
        }
        handled = location.hash;
        const route = parseRoute(location.hash);
        if (route) {
            emit("af-route", route);
        }
    };
    const onView = listen("af-view-changed", route => {
        safeStorage.set(VIEW_KEY, route.view);
        const hash = formatRoute(route);
        // Unchanged when the view followed the address (Back, a typed link).
        if (hash !== location.hash) {
            if (landed) {
                history.pushState(null, "", hash);
            } else {
                history.replaceState(null, "", hash);
            }
        }
        handled = hash;
        landed = true;
    });
    window.addEventListener("hashchange", onAddress);
    window.addEventListener("popstate", onAddress);
    return () => {
        window.removeEventListener("hashchange", onAddress);
        window.removeEventListener("popstate", onAddress);
        document.removeEventListener("af-view-changed", onView);
    };
}
