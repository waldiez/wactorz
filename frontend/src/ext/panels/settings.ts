/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * A panel's settings: what the server says it has, what this browser chose for
 * each, and the section of Settings they are changed in.
 *
 * The server only describes them (`wactorz/ext/_panel.py`). The values are this
 * browser's, kept under one key per panel, and what is kept is trusted no more
 * than anything else in storage: a value of the wrong kind, a choice no longer
 * offered or a number out of range reads as the setting's default.
 */
import { safeStorage } from "../../safeStorage";
import type { SettingsSection } from "../../types/settings";
import { el, named } from "../../ui/dom";
import { choiceGroup, settingRow, settingRows, settingSwitch } from "../../ui/settings/layout";
import type { PanelEntry, PanelSetting, PanelSettingValue, PanelStop } from "./types";

/** What a setting's key looks like, as the server checks it. */
const KEY = /^[a-z][a-z0-9-]*$/;

/** Where the values this browser chose for the panel `name` are kept. */
export function settingsStorageKey(name: string): string {
    return `wactorz-panel-settings-${name}`;
}

/** The Settings section of the panel `name`: prefixed, so it never takes the place of one of the dashboard's. */
export function settingsSectionKey(name: string): string {
    return `panel-${name}`;
}

function isNumber(value: unknown): value is number {
    return typeof value === "number" && Number.isFinite(value);
}

function isChoice(value: unknown): value is { value: string; label: string } {
    const fields = (value ?? {}) as Record<string, unknown>;
    return typeof fields.value === "string" && typeof fields.label === "string";
}

/** `value` if `setting` can hold it, else nothing. */
function held(setting: PanelSetting, value: unknown): PanelSettingValue | undefined {
    if (setting.kind === "switch") {
        return typeof value === "boolean" ? value : undefined;
    }
    if (setting.kind === "choice") {
        return typeof value === "string" && setting.choices.some(c => c.value === value) ? value : undefined;
    }
    const inRange =
        isNumber(value) &&
        (setting.min === undefined || value >= setting.min) &&
        (setting.max === undefined || value <= setting.max);
    return inRange ? value : undefined;
}

/** A setting of the kind `fields` names, its default still to be read; nothing for a kind not drawn. */
function shaped(
    base: { key: string; label: string; hint: string },
    fields: Record<string, unknown>,
): PanelSetting | undefined {
    const kind = fields.kind;
    if (kind === "switch") {
        return { ...base, kind, default: false };
    }
    if (kind === "choice" && Array.isArray(fields.choices)) {
        const choices = (fields.choices as unknown[]).filter(isChoice);
        return {
            ...base,
            kind,
            default: "",
            choices: choices.map(c => ({ value: c.value, label: c.label })),
        };
    }
    if (kind === "number") {
        return {
            ...base,
            kind,
            default: 0,
            ...(isNumber(fields.min) ? { min: fields.min } : {}),
            ...(isNumber(fields.max) ? { max: fields.max } : {}),
            ...(isNumber(fields.step) && fields.step > 0 ? { step: fields.step } : {}),
        };
    }
    return undefined;
}

/** One setting from what the server sent, or nothing when it is not shaped like one. */
function readSetting(raw: unknown): PanelSetting | undefined {
    const fields = (raw ?? {}) as Record<string, unknown>;
    const { key, label } = fields;
    if (typeof key !== "string" || !KEY.test(key) || typeof label !== "string" || !label.trim()) {
        return undefined;
    }
    const setting = shaped({ key, label, hint: typeof fields.hint === "string" ? fields.hint : "" }, fields);
    // The default is a value like any other: one the setting cannot hold leaves no setting.
    const fallback = setting && held(setting, fields.default);
    return setting && fallback !== undefined
        ? ({ ...setting, default: fallback } as PanelSetting)
        : undefined;
}

/** The settings in what the server sent for a panel, leaving out anything not shaped like one, and a key's second use. */
export function readSettings(raw: unknown): PanelSetting[] {
    const settings: PanelSetting[] = [];
    for (const item of Array.isArray(raw) ? (raw as unknown[]) : []) {
        const setting = readSetting(item);
        if (setting && !settings.some(s => s.key === setting.key)) {
            settings.push(setting);
        }
    }
    return settings;
}

/** What is kept under a panel's key, as an object; empty when it is anything else. */
function kept(stored: string | null): Record<string, unknown> {
    try {
        const parsed: unknown = JSON.parse(stored ?? "{}");
        return parsed && typeof parsed === "object" && !Array.isArray(parsed)
            ? (parsed as Record<string, unknown>)
            : {};
    } catch {
        return {};
    }
}

/** One panel's settings and this browser's values for them. */
export class PanelSettings {
    private readonly _handlers = new Map<string, Set<(value: PanelSettingValue) => void>>();
    private readonly _storageKey: string;

    constructor(
        panel: string,
        /** The panel's settings, in the order drawn. */
        readonly settings: readonly PanelSetting[],
    ) {
        this._storageKey = settingsStorageKey(panel);
    }

    /** The value of `key`: what this browser chose, else its default; nothing for a key the panel did not declare. */
    get(key: string): PanelSettingValue | undefined {
        const setting = this.settings.find(s => s.key === key);
        return setting && (held(setting, kept(safeStorage.get(this._storageKey))[key]) ?? setting.default);
    }

    /** Choose `value` for `key` and tell whoever follows it. A value the setting cannot hold is ignored. */
    set(key: string, value: PanelSettingValue): void {
        const setting = this.settings.find(s => s.key === key);
        if (!setting || held(setting, value) === undefined || this.get(key) === value) {
            return;
        }
        safeStorage.set(
            this._storageKey,
            JSON.stringify({ ...kept(safeStorage.get(this._storageKey)), [key]: value }),
        );
        this._tell(key, value);
    }

    /** Be told when `key` changes; returns how to stop. */
    follow(key: string, handler: (value: PanelSettingValue) => void): PanelStop {
        const handlers = this._handlers.get(key) ?? new Set();
        this._handlers.set(key, handlers);
        handlers.add(handler);
        return () => handlers.delete(handler);
    }

    /**
     * Hear the changes made in this browser's other windows; returns how to stop.
     *
     * Each value is compared with the one last seen here, not with what the
     * event says was there before: a window that clears the whole storage names
     * no key and no earlier value, and every setting is then back at its default.
     */
    start(): PanelStop {
        const seen = new Map(this.settings.map(({ key }) => [key, this.get(key)]));
        const stopSeeing = this.settings.map(({ key }) => this.follow(key, value => seen.set(key, value)));
        const heard = (event: StorageEvent): void => {
            if (event.key !== null && event.key !== this._storageKey) {
                return;
            }
            for (const { key } of this.settings) {
                const now = this.get(key);
                if (now !== undefined && now !== seen.get(key)) {
                    this._tell(key, now);
                }
            }
        };
        window.addEventListener("storage", heard);
        return () => {
            window.removeEventListener("storage", heard);
            stopSeeing.forEach(stop => stop());
        };
    }

    private _tell(key: string, value: PanelSettingValue): void {
        for (const handler of [...(this._handlers.get(key) ?? [])]) {
            handler(value);
        }
    }
}

/** The field of a number setting: what is typed is kept once it is a value the setting can hold, and put back otherwise. */
function numberField(
    name: string,
    setting: PanelSetting & { kind: "number" },
    store: PanelSettings,
): HTMLElement {
    const input = named(el("input", "af-cfg-input"), name, setting.label);
    input.type = "number";
    input.inputMode = "decimal";
    for (const bound of ["min", "max", "step"] as const) {
        const value = setting[bound];
        if (value !== undefined) {
            input[bound] = String(value);
        }
    }
    input.value = String(store.get(setting.key));
    input.addEventListener("change", () => {
        store.set(setting.key, input.value.trim() === "" ? NaN : Number(input.value));
        input.value = String(store.get(setting.key));
    });
    return input;
}

/** The control of `setting`, showing the value in `store` and changing it there. */
function control(panel: string, setting: PanelSetting, store: PanelSettings): HTMLElement {
    const name = `panel-${panel}-${setting.key}`;
    if (setting.kind === "switch") {
        return settingSwitch(name, setting.label, store.get(setting.key) === true, on =>
            store.set(setting.key, on),
        );
    }
    if (setting.kind === "choice") {
        return choiceGroup(name, setting.label, setting.choices, String(store.get(setting.key)), value =>
            store.set(setting.key, value),
        );
    }
    return numberField(name, setting, store);
}

/** The section of Settings for the panel `entry`, a row per setting in `store`, under `icon`. */
export function settingsSection(entry: PanelEntry, icon: string, store: PanelSettings): SettingsSection {
    return {
        key: settingsSectionKey(entry.name),
        label: entry.title,
        line: store.settings.map(s => s.label).join(", "),
        scope: "browser",
        icon,
        build(): HTMLElement {
            const body = el("div", "af-settings-body");
            body.appendChild(
                settingRows(
                    ...store.settings.map(s => settingRow(s.label, s.hint, control(entry.name, s, store))),
                ),
            );
            return body;
        },
    };
}
