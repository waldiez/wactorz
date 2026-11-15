/**
 * SPDX-License-Identifier: Apache-2.0
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { listen } from "../../../events";
import { PANELS_KEY, register } from "../../../ext/panels";
import { PanelView, type PanelServices } from "../../../ext/panels/PanelView";
import {
    PanelSettings,
    readSettings,
    settingsSection,
    settingsSectionKey,
    settingsStorageKey,
} from "../../../ext/panels/settings";
import { PANEL_API_VERSION, type PanelHost } from "../../../ext/panels/types";
import { safeStorage } from "../../../safeStorage";
import type { SettingsSection } from "../../../types/settings";
import { SettingsView } from "../../../ui/settings/SettingsView";
import { allByRole, byRole } from "../../queries";

const UNITS = {
    key: "temperature",
    label: "Temperature",
    hint: "How temperatures read.",
    kind: "choice",
    default: "c",
    choices: [
        { value: "c", label: "°C" },
        { value: "f", label: "°F" },
    ],
};
const COMPACT = { key: "compact", label: "Compact cards", hint: "", kind: "switch", default: false };
const KEEP = {
    key: "keep",
    label: "Points kept",
    hint: "",
    kind: "number",
    default: 400,
    min: 50,
    max: 2000,
};
const ENTRY = { name: "air", title: "Air", icon: "grid", settings: [UNITS, COMPACT, KEEP] };
const STORED = settingsStorageKey("air");

function store(): PanelSettings {
    return new PanelSettings("air", readSettings(ENTRY.settings));
}

/** Another window of this browser keeps `value` under `key`, or clears everything (`key` null), and this one is told. */
function fromAnotherWindow(key: string | null, value: string | null): void {
    if (key === null) {
        localStorage.clear();
    } else if (value === null) {
        safeStorage.remove(key);
    } else {
        safeStorage.set(key, value);
    }
    window.dispatchEvent(new StorageEvent("storage", { key, newValue: value }));
}

beforeEach(() => {
    safeStorage.remove(STORED);
    safeStorage.remove(PANELS_KEY);
});

describe("what the server says a panel's settings are", () => {
    it("keeps each kind as it was sent", () => {
        expect(readSettings(ENTRY.settings)).toEqual([UNITS, COMPACT, KEEP]);
    });

    it.each([
        ["nothing", undefined],
        ["something that is no list", { key: "a" }],
        ["a key that is no address", [{ ...COMPACT, key: "Compact" }]],
        ["a blank label", [{ ...COMPACT, label: " " }]],
        ["a kind it cannot draw", [{ ...COMPACT, kind: "slider" }]],
        ["a switch whose default is a word", [{ ...COMPACT, default: "on" }]],
        ["a choice without choices", [{ ...UNITS, choices: undefined }]],
        ["a choice whose default is not offered", [{ ...UNITS, default: "k" }]],
        ["a number whose default is out of range", [{ ...KEEP, default: 10 }]],
        ["a number whose default is a word", [{ ...KEEP, default: "many" }]],
        ["no setting at all", [null]],
    ])("leaves out %s", (_what, sent) => {
        expect(readSettings(sent)).toEqual([]);
    });

    it("keeps the first setting of a key, drops ill-shaped choices and bounds, and reads a missing hint as none", () => {
        const sent = [
            { ...UNITS, hint: undefined, choices: [...UNITS.choices, { value: 1 }] },
            { ...COMPACT, key: "temperature" },
            { ...KEEP, min: "low", step: 0 },
        ];

        expect(readSettings(sent)).toEqual([
            { ...UNITS, hint: "" },
            { key: "keep", label: "Points kept", hint: "", kind: "number", default: 400, max: 2000 },
        ]);
    });
});

describe("a panel's values in this browser", () => {
    it("are the defaults until something is chosen, and nothing for a key not declared", () => {
        const settings = store();

        expect(settings.get("temperature")).toBe("c");
        expect(settings.get("compact")).toBe(false);
        expect(settings.get("keep")).toBe(400);
        expect(settings.get("other")).toBeUndefined();
    });

    it("are kept under the panel's own key, beside what was already there", () => {
        const settings = store();

        settings.set("temperature", "f");
        settings.set("compact", true);

        expect(JSON.parse(safeStorage.get(STORED) ?? "")).toEqual({ temperature: "f", compact: true });
        expect(store().get("temperature")).toBe("f");
    });

    it.each([
        ["temperature", "k"],
        ["temperature", true],
        ["compact", "yes"],
        ["keep", 10],
        ["keep", 5000],
        ["keep", NaN],
        ["other", 1],
    ])("ignore %s set to %s", (key, value) => {
        const settings = store();
        const told = vi.fn();
        settings.follow(key, told);

        settings.set(key, value);

        expect(told).not.toHaveBeenCalled();
        expect(safeStorage.get(STORED)).toBeNull();
    });

    it.each([
        ["what is not JSON", "{"],
        ["a list", "[1]"],
        ["values of the wrong kind", JSON.stringify({ temperature: 3, compact: "no", keep: "400" })],
        [
            "a choice no longer offered and a number out of range",
            JSON.stringify({ temperature: "k", keep: 9 }),
        ],
    ])("read as the defaults over %s", (_what, stored) => {
        safeStorage.set(STORED, stored);
        const settings = store();

        expect([settings.get("temperature"), settings.get("compact"), settings.get("keep")]).toEqual([
            "c",
            false,
            400,
        ]);
    });

    it("tell whoever follows a key of its change, once, until they stop", () => {
        const settings = store();
        const told = vi.fn();
        const stop = settings.follow("temperature", told);

        settings.set("temperature", "f");
        settings.set("temperature", "f");
        settings.set("compact", true);
        expect(told.mock.calls).toEqual([["f"]]);

        stop();
        settings.set("temperature", "c");
        expect(told).toHaveBeenCalledTimes(1);
    });

    it("hear a change made in another window, for the keys that changed, until stopped", () => {
        const settings = store();
        const units = vi.fn();
        const compact = vi.fn();
        settings.follow("temperature", units);
        settings.follow("compact", compact);
        const stop = settings.start();

        fromAnotherWindow("another-key", JSON.stringify({ temperature: "f" }));
        fromAnotherWindow(STORED, JSON.stringify({ temperature: "f" }));
        // Removed there: back to the default here.
        fromAnotherWindow(STORED, null);
        expect(units.mock.calls).toEqual([["f"], ["c"]]);
        expect(compact).not.toHaveBeenCalled();

        stop();
        fromAnotherWindow(STORED, JSON.stringify({ temperature: "f" }));
        expect(units).toHaveBeenCalledTimes(2);
    });

    it("hear another window clearing the whole storage, which names no key", () => {
        const settings = store();
        const units = vi.fn();
        const compact = vi.fn();
        settings.follow("temperature", units);
        settings.follow("compact", compact);
        const stop = settings.start();
        // Chosen here, so the event is the first this window hears of anything.
        settings.set("temperature", "f");
        units.mockClear();

        fromAnotherWindow(null, null);

        expect(units.mock.calls).toEqual([["c"]]);
        expect(compact).not.toHaveBeenCalled();
        stop();
    });
});

describe("a panel's section of Settings", () => {
    function drawn(settings = store()): { section: SettingsSection; body: HTMLElement } {
        const section = settingsSection(ENTRY, "grid", settings);
        const body = section.build();
        document.body.replaceChildren(body);
        return { section, body };
    }

    it("is named after the tab, kept in this browser, at an address none of the dashboard's sections has", () => {
        const { section } = drawn();

        expect(section).toMatchObject({
            key: "panel-air",
            label: "Air",
            line: "Temperature, Compact cards, Points kept",
            scope: "browser",
            icon: "grid",
        });
        expect(settingsSectionKey("sound")).not.toBe("sound");
    });

    it("draws a row per setting, showing what was chosen, with its hint read out", () => {
        const settings = store();
        settings.set("temperature", "f");
        const { body } = drawn(settings);

        const units = byRole(body, "radiogroup", "Temperature");
        expect(allByRole(units, "radio").map(r => (r as HTMLInputElement).checked)).toEqual([false, true]);
        const hint = document.getElementById(units.getAttribute("aria-describedby") ?? "");
        expect(hint?.textContent).toBe("How temperatures read.");
        expect((byRole(body, "switch", "Compact cards") as HTMLInputElement).checked).toBe(false);
        expect((byRole(body, "spinbutton", "Points kept") as HTMLInputElement).value).toBe("400");
    });

    it("keeps a choice and a switch as they are changed", () => {
        const settings = store();
        const { body } = drawn(settings);

        const fahrenheit = allByRole(
            byRole(body, "radiogroup", "Temperature"),
            "radio",
        )[1] as HTMLInputElement;
        fahrenheit.checked = true;
        fahrenheit.dispatchEvent(new Event("change"));
        const compact = byRole(body, "switch", "Compact cards") as HTMLInputElement;
        compact.checked = true;
        compact.dispatchEvent(new Event("change"));

        expect(settings.get("temperature")).toBe("f");
        expect(settings.get("compact")).toBe(true);
    });

    it("keeps a number in range, and puts back the last one over what it cannot hold", () => {
        const settings = store();
        const { body } = drawn(settings);
        const keep = byRole(body, "spinbutton", "Points kept") as HTMLInputElement;
        expect([keep.min, keep.max, keep.step]).toEqual(["50", "2000", ""]);

        keep.value = "800";
        keep.dispatchEvent(new Event("change"));
        expect(settings.get("keep")).toBe(800);

        for (const typed of ["9000", ""]) {
            keep.value = typed;
            keep.dispatchEvent(new Event("change"));
            expect(settings.get("keep")).toBe(800);
            expect(keep.value).toBe("800");
        }
    });
});

describe("registering panels", () => {
    const stops: (() => void)[] = [];
    const services = (panel: object = {}): PanelServices => ({
        frames: () => () => undefined,
        toast: () => undefined,
        load: () => Promise.resolve(panel),
    });

    afterEach(() => stops.splice(0).forEach(stop => stop()));

    it("adds a section for a panel with settings, which Settings opens at its address, and none for one without", () => {
        safeStorage.set(PANELS_KEY, JSON.stringify([ENTRY, { name: "mlops", title: "MLOps", icon: "grid" }]));
        const view = new SettingsView([]);
        stops.push(listen("af-register-settings", section => view.register(section)));

        stops.push(register(services()));
        view.show("panel-air");
        const page = view.build();

        expect(allByRole(page, "link").map(link => link.getAttribute("href"))).toEqual([
            "#/settings/panel-air",
        ]);
        expect(byRole(page, "region", "Air").textContent).toContain("Kept in this browser");
        expect(byRole(page, "radiogroup", "Temperature")).toBeTruthy();
    });

    it("gives the panel what Settings changed, and tells it of a change from another window while it is open", async () => {
        safeStorage.set(PANELS_KEY, JSON.stringify([ENTRY]));
        let host: PanelHost | undefined;
        const panel = {
            apiVersion: PANEL_API_VERSION,
            mount: (_root: HTMLElement, h: PanelHost) => void (host = h),
        };
        let section: SettingsSection | undefined;
        let build: (() => HTMLElement) | undefined;
        stops.push(listen("af-register-settings", s => (section = s)));
        stops.push(listen("af-register-view", v => (build = v.build)));
        stops.push(register(services(panel)));

        const body = section?.build() as HTMLElement;
        const fahrenheit = allByRole(
            byRole(body, "radiogroup", "Temperature"),
            "radio",
        )[1] as HTMLInputElement;
        fahrenheit.checked = true;
        fahrenheit.dispatchEvent(new Event("change"));

        build?.();
        await new Promise(resolve => setTimeout(resolve));
        expect(host?.setting("temperature")).toBe("f");
        expect(host?.setting("other")).toBeUndefined();

        const told = vi.fn();
        host?.onSetting("temperature", told);
        fromAnotherWindow(STORED, JSON.stringify({ temperature: "c" }));
        expect(told.mock.calls).toEqual([["c"]]);
    });

    it("stops telling a panel once its view is left", async () => {
        const settings = store();
        let host: PanelHost | undefined;
        const panel = {
            apiVersion: PANEL_API_VERSION,
            mount: (_root: HTMLElement, h: PanelHost) => void (host = h),
        };
        const view = new PanelView(ENTRY, services(panel), settings);
        view.build();
        await new Promise(resolve => setTimeout(resolve));
        const told = vi.fn();
        host?.onSetting("temperature", told);

        view.close();
        settings.set("temperature", "f");

        expect(told).not.toHaveBeenCalled();
    });

    it("reads a panel's settings from its entry when none are shared", async () => {
        let host: PanelHost | undefined;
        const panel = {
            apiVersion: PANEL_API_VERSION,
            mount: (_root: HTMLElement, h: PanelHost) => void (host = h),
        };
        const view = new PanelView(ENTRY, services(panel));
        view.build();
        await new Promise(resolve => setTimeout(resolve));

        expect(host?.setting("keep")).toBe(400);
        view.close();
    });
});
