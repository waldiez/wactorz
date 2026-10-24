/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";

import { emit, listen } from "../events";
import {
    DEFAULT_ROUTE,
    VIEW_KEY,
    formatRoute,
    initialRoute,
    parseRoute,
    startRouter,
    type Route,
} from "../router";

describe("parseRoute", () => {
    it.each<[string, Route | null]>([
        ["#/feed", { view: "feed", path: [] }],
        ["#/chat", { view: "chat", path: [] }],
        ["#/chat/weather", { view: "chat", path: ["weather"] }],
        ["#/chat/my%20agent%2F2", { view: "chat", path: ["my agent/2"] }],
        ["#/models/hvac/versions/7", { view: "models", path: ["hvac", "versions", "7"] }],
        ["#/models/hvac/", { view: "models", path: ["hvac"] }],
        ["#feed", { view: "feed", path: [] }],
        ["", null],
        ["#", null],
        ["#/", null],
        ["#/Feed", null],
        ["#/<script>", null],
    ])("reads %j", (hash, route) => {
        expect(parseRoute(hash)).toEqual(route);
    });

    it("keeps the levels before one that is badly escaped", () => {
        expect(parseRoute("#/models/hvac/%E0%A4%A/7")).toEqual({ view: "models", path: ["hvac"] });
    });
});

describe("formatRoute", () => {
    it.each<Route>([
        { view: "feed", path: [] },
        { view: "chat", path: ["my agent/2"] },
        { view: "models", path: ["hvac", "versions", "7"] },
    ])("writes %j so that it reads back the same", route => {
        expect(parseRoute(formatRoute(route))).toEqual(route);
    });

    it("keeps a level with a slash in it one level", () => {
        expect(formatRoute({ view: "chat", path: ["a/b"] })).toBe("#/chat/a%2Fb");
    });
});

describe("initialRoute", () => {
    beforeEach(() => {
        history.replaceState(null, "", "#");
        localStorage.clear();
    });

    it("opens what the address names", () => {
        localStorage.setItem(VIEW_KEY, "settings");
        history.replaceState(null, "", "#/chat/weather");

        expect(initialRoute()).toEqual({ view: "chat", path: ["weather"] });
    });

    it("opens the view last open in this browser when the address names none", () => {
        localStorage.setItem(VIEW_KEY, "feed");

        expect(initialRoute()).toEqual({ view: "feed", path: [] });
    });

    it("opens the overview on a first visit, or when what was remembered is not a view", () => {
        expect(initialRoute()).toEqual(DEFAULT_ROUTE);
        localStorage.setItem(VIEW_KEY, "<nonsense>");
        expect(initialRoute()).toEqual(DEFAULT_ROUTE);
    });
});

describe("startRouter", () => {
    let stop: () => void;

    beforeEach(() => {
        history.replaceState(null, "", "#");
        localStorage.clear();
        stop = startRouter();
    });

    afterEach(() => {
        stop();
    });

    it("puts the first view in place of the entry the page opened with, adding nothing to Back", () => {
        const before = history.length;

        emit("af-view-changed", { view: "feed", path: [] });

        expect(location.hash).toBe("#/feed");
        expect(history.length).toBe(before);
    });

    it("adds a history entry for every view after the first, so Back steps through them", () => {
        emit("af-view-changed", { view: "feed", path: [] });
        const before = history.length;

        emit("af-view-changed", { view: "chat", path: ["weather"] });

        expect(location.hash).toBe("#/chat/weather");
        expect(history.length).toBe(before + 1);
    });

    it("adds nothing when the view followed the address", () => {
        emit("af-view-changed", { view: "feed", path: [] });
        const before = history.length;

        emit("af-view-changed", { view: "feed", path: [] });

        expect(history.length).toBe(before);
    });

    it("remembers the view for an address with no hash", () => {
        emit("af-view-changed", { view: "settings", path: [] });

        expect(localStorage.getItem(VIEW_KEY)).toBe("settings");
    });

    it("opens what an address names when it changes, and nothing for one that names none", () => {
        const routes: Route[] = [];
        const heard = listen("af-route", r => routes.push(r));

        history.replaceState(null, "", "#/chat/main");
        window.dispatchEvent(new HashChangeEvent("hashchange"));
        history.replaceState(null, "", "#/");
        window.dispatchEvent(new HashChangeEvent("hashchange"));
        document.removeEventListener("af-route", heard);

        expect(routes).toEqual([{ view: "chat", path: ["main"] }]);
    });

    it("follows Back whichever event announces it, and handles an address once", () => {
        const routes: Route[] = [];
        const heard = listen("af-route", r => routes.push(r));

        history.replaceState(null, "", "#/settings");
        window.dispatchEvent(new PopStateEvent("popstate"));
        window.dispatchEvent(new HashChangeEvent("hashchange"));
        document.removeEventListener("af-route", heard);

        expect(routes).toEqual([{ view: "settings", path: [] }]);
    });

    it("does not hand the dashboard back the address it just wrote", () => {
        const routes: Route[] = [];
        const heard = listen("af-route", r => routes.push(r));

        emit("af-view-changed", { view: "feed", path: [] });
        window.dispatchEvent(new HashChangeEvent("hashchange"));
        document.removeEventListener("af-route", heard);

        expect(routes).toEqual([]);
    });

    it("stops following either way once stopped", () => {
        stop();
        const routes: Route[] = [];
        const heard = listen("af-route", r => routes.push(r));

        emit("af-view-changed", { view: "feed", path: [] });
        window.dispatchEvent(new HashChangeEvent("hashchange"));
        document.removeEventListener("af-route", heard);

        expect(location.hash).not.toBe("#/feed");
        expect(routes).toEqual([]);
    });
});
