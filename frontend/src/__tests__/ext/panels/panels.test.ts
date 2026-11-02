/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { loadServerConfig } from "../../../config/serverConfig";
import { emit, listen } from "../../../events";
import { PANELS_KEY, parsePanels, register, toastOptions } from "../../../ext/panels";
import { PanelView, topicMatches, type PanelServices } from "../../../ext/panels/PanelView";
import { PANEL_API_VERSION, type PanelHost, type PanelModule } from "../../../ext/panels/types";
import { safeStorage } from "../../../safeStorage";

const ENTRY = { name: "mlops", title: "MLOps", icon: "grid" };

type Frame = (topic: string, payload: unknown) => void;

/** Lets a panel's load and mount finish. */
const settled = (): Promise<void> => new Promise(resolve => setTimeout(resolve));

function services(panel: Partial<PanelModule>): PanelServices & { frame: Frame; toasts: unknown[] } {
    const handlers = new Set<Frame>();
    const toasts: unknown[] = [];
    return {
        frames: handler => {
            handlers.add(handler);
            return () => handlers.delete(handler);
        },
        toast: notice => toasts.push(notice),
        load: () => Promise.resolve(panel),
        frame: (topic, payload) => handlers.forEach(handler => handler(topic, payload)),
        toasts,
    };
}

/** A panel that keeps its host, and says how often it was ended. */
function mounted(): { panel: PanelModule; host: () => PanelHost; ended: ReturnType<typeof vi.fn> } {
    let held: PanelHost | undefined;
    const ended = vi.fn();
    return {
        ended,
        host: () => held as PanelHost,
        panel: {
            apiVersion: PANEL_API_VERSION,
            mount(root, host) {
                held = host;
                root.textContent = "the panel's own";
                return ended;
            },
        },
    };
}

describe("a panel's view", () => {
    const stops: (() => void)[] = [];

    function view(using: PanelServices): PanelView {
        const panel = new PanelView(ENTRY, using);
        stops.push(panel.start());
        return panel;
    }

    afterEach(() => stops.splice(0).forEach(stop => stop()));

    it("loads nothing until the tab opens", () => {
        const using = services(mounted().panel);
        const load = vi.spyOn(using, "load");

        view(using);

        expect(load).not.toHaveBeenCalled();
    });

    it("fills the view when it opens, asking for its script under /api", async () => {
        const using = services(mounted().panel);
        const load = vi.spyOn(using, "load");
        const panel = view(using);

        const root = panel.build();
        expect(root.textContent).toBe("Loading…");
        await settled();

        expect(root.textContent).toBe("the panel's own");
        expect(load.mock.calls[0]?.[0]).toMatch(/\/api\/ext\/mlops\/panel\.js$/);
    });

    it("mounts once however often the dashboard draws the view", async () => {
        const mount = vi.fn();
        const panel = view(services({ apiVersion: PANEL_API_VERSION, mount }));

        const first = panel.build();
        await settled();

        expect(panel.build()).toBe(first);
        await settled();
        expect(mount).toHaveBeenCalledTimes(1);
    });

    it("ends the panel when another view opens, and mounts it again on return", async () => {
        const { panel, ended } = mounted();
        const open = view(services(panel));
        const root = open.build();
        await settled();

        emit("af-view-changed", { view: "feed", path: [] });
        expect(ended).toHaveBeenCalledTimes(1);
        expect(root.childElementCount).toBe(0);

        open.build();
        await settled();
        expect(root.textContent).toBe("the panel's own");
    });

    it("ends a panel that finishes mounting after the view was left", async () => {
        const ended = vi.fn();
        let finish: (stop: () => void) => void = () => undefined;
        const open = view(
            services({
                apiVersion: PANEL_API_VERSION,
                mount: () => new Promise(resolve => (finish = resolve)),
            }),
        );
        open.build();
        await settled();

        emit("af-view-changed", { view: "feed", path: [] });
        finish(ended);
        await settled();

        expect(ended).toHaveBeenCalledTimes(1);
    });

    it("drops what a panel still mounting starts after its view closed", async () => {
        let host: PanelHost | undefined;
        let go: () => void = () => undefined;
        const using = services({
            apiVersion: PANEL_API_VERSION,
            mount: async (_root, given) => {
                host = given;
                await new Promise<void>(resolve => (go = resolve));
                given.subscribe("mlops/#", heard);
                given.onRoute(moved);
                given.navigate(["late"]);
            },
        });
        const heard = vi.fn();
        const moved = vi.fn();
        const routes: unknown[] = [];
        const stopRoutes = listen("af-route", route => routes.push(route));
        const open = view(using);
        open.build();
        await settled();

        emit("af-view-changed", { view: "feed", path: [] });
        go();
        await settled();
        open.build();
        await settled();
        emit("af-view-changed", { view: "mlops", path: ["again"] });
        using.frame("mlops/a/state", 1);
        stopRoutes();

        expect(host).toBeDefined();
        expect(heard).not.toHaveBeenCalled();
        expect(moved).not.toHaveBeenCalled();
        expect(routes).toEqual([]);
    });

    it("does not mount a script that arrives after the view was left", async () => {
        const mount = vi.fn();
        let arrive: (panel: Partial<PanelModule>) => void = () => undefined;
        const using = services({});
        using.load = () => new Promise(resolve => (arrive = resolve));
        const open = view(using);
        open.build();

        emit("af-view-changed", { view: "feed", path: [] });
        arrive({ apiVersion: PANEL_API_VERSION, mount });
        await settled();

        expect(mount).not.toHaveBeenCalled();
    });

    it.each([
        ["a script that cannot load", () => Promise.reject(new Error("404")), "failed to open"],
        ["a script with no mount", () => Promise.resolve({ apiVersion: PANEL_API_VERSION }), "no mount"],
        [
            "another version",
            () => Promise.resolve({ apiVersion: 99, mount: vi.fn() }),
            "written for dashboard API 99",
        ],
        [
            "a mount that throws",
            () =>
                Promise.resolve({
                    apiVersion: PANEL_API_VERSION,
                    mount: () => {
                        throw new Error("boom");
                    },
                }),
            "failed to open",
        ],
    ])("says so in its own view for %s", async (_name, load, words) => {
        vi.spyOn(console, "error").mockImplementation(() => undefined);
        const using = services({});
        using.load = load as PanelServices["load"];

        const root = view(using).build();
        await settled();

        expect(root.querySelector("[role=alert]")?.textContent).toContain(words);
        vi.restoreAllMocks();
    });

    it("survives a panel that throws while closing", async () => {
        vi.spyOn(console, "error").mockImplementation(() => undefined);
        const open = view(
            services({
                apiVersion: PANEL_API_VERSION,
                mount: () => () => {
                    throw new Error("boom");
                },
            }),
        );
        open.build();
        await settled();

        expect(() => open.close()).not.toThrow();
        vi.restoreAllMocks();
    });

    describe("the host", () => {
        async function opened(): Promise<{ host: PanelHost; using: ReturnType<typeof services> }> {
            const { panel, host } = mounted();
            const using = services(panel);
            view(using).build();
            await settled();
            return { host: host(), using };
        }

        it("says where the address points when the panel mounts", async () => {
            const { panel, host } = mounted();
            view(services(panel)).build();
            emit("af-view-changed", { view: "mlops", path: ["runs", "7"] });
            await settled();

            expect(host().route()).toEqual(["runs", "7"]);
        });

        it("follows the address, and stops when asked", async () => {
            const { host } = await opened();
            const heard = vi.fn();
            const stop = host.onRoute(heard);

            emit("af-view-changed", { view: "mlops", path: ["a"] });
            stop();
            emit("af-view-changed", { view: "mlops", path: ["b"] });

            expect(heard.mock.calls).toEqual([[["a"]]]);
        });

        it("asks for its own address", async () => {
            const { host } = await opened();
            const routes: unknown[] = [];
            const stop = listen("af-route", route => routes.push(route));

            host.navigate(["runs"]);
            stop();

            expect(routes).toEqual([{ view: "mlops", path: ["runs"] }]);
        });

        it("hears the topics it asked for, until it closes", async () => {
            const { host, using } = await opened();
            const heard = vi.fn();
            host.subscribe("mlops/+/state", heard);

            using.frame("mlops/a/state", 1);
            using.frame("agents/a/heartbeat", 2);
            emit("af-view-changed", { view: "feed", path: [] });
            using.frame("mlops/a/state", 3);

            expect(heard.mock.calls).toEqual([["mlops/a/state", 1]]);
        });

        it("passes on its notices", async () => {
            const { host, using } = await opened();

            host.toast({ title: "Done", message: "Trained" });

            expect(using.toasts).toEqual([{ title: "Done", message: "Trained" }]);
        });

        it("asks the server through the dashboard's client", async () => {
            const fetcher = vi.fn(() => Promise.resolve(new Response('{"runs": 2}')));
            vi.stubGlobal("fetch", fetcher);
            const { host } = await opened();

            expect(await host.request("/api/mlops/runs")).toEqual({ ok: true, value: { runs: 2 } });
            vi.unstubAllGlobals();
        });
    });
});

describe("topicMatches", () => {
    it.each([
        ["a/b", "a/b", true],
        ["a/+", "a/b", true],
        ["a/#", "a/b/c", true],
        ["#", "a", true],
        ["a/+", "a/b/c", false],
        ["a/b/c", "a/b", false],
        ["a/b", "a/c", false],
    ])("%s against %s is %s", (filter, topic, expected) => {
        expect(topicMatches(filter, topic)).toBe(expected);
    });
});

describe("the list of panels", () => {
    const stops: (() => void)[] = [];

    beforeEach(() => safeStorage.remove(PANELS_KEY));
    afterEach(() => stops.splice(0).forEach(stop => stop()));

    it("keeps what is shaped like a panel", () => {
        const stored = JSON.stringify([ENTRY, { name: 1 }, null]);

        expect(parsePanels(stored)).toEqual([ENTRY]);
    });

    it.each([null, "not json", '{"a": 1}'])("is empty for %s", stored => {
        expect(parsePanels(stored)).toEqual([]);
    });

    it("comes from /api/config, and goes when the server stops listing it", async () => {
        const answer = (panels: unknown[]) =>
            vi.fn(() => Promise.resolve(new Response(JSON.stringify({ extensions: { panels } }))));

        vi.stubGlobal("fetch", answer([ENTRY]));
        await loadServerConfig();
        expect(parsePanels(safeStorage.get(PANELS_KEY))).toEqual([ENTRY]);

        vi.stubGlobal("fetch", answer([]));
        await loadServerConfig();
        expect(safeStorage.get(PANELS_KEY)).toBeNull();
        vi.unstubAllGlobals();
    });

    it("registers a view per panel, on the bus, with an icon the dashboard has", () => {
        safeStorage.set(PANELS_KEY, JSON.stringify([{ ...ENTRY, icon: "no-such-icon" }]));
        const views: { key: string; icon: string; label: string }[] = [];
        const stopHearing = listen("af-register-view", view => views.push(view));

        stops.push(register(services({})));
        stopHearing();

        expect(views).toMatchObject([{ key: "mlops", icon: "grid", label: "MLOps" }]);
    });

    it("maps a panel's notices to the dashboard's toasts", () => {
        expect(toastOptions({ title: "t", message: "m" }).type).toBe("system");
        expect(toastOptions({ title: "t", message: "m", type: "error" }).type).toBe("alert-error");
    });
});
