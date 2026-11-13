/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * One package's tab: loads its script when the tab opens, mounts it, and ends it
 * when another view opens.
 *
 * A panel is someone else's code in the page, so whatever goes wrong with it stops
 * at its own view, as a message there. The dashboard may ask for the view's element
 * several times while it is open, so the element is kept and the panel is mounted
 * once per visit.
 */
import { emit, listen } from "../../events";
import { apiUrl, requestJson } from "../../io/api";
import { LineChart, SERIES_COLORS, type ChartOptions, type Series } from "../../ui/charts";
import { PanelSettings, readSettings } from "./settings";
import {
    PANEL_API_VERSION,
    type PanelChart,
    type PanelChartOptions,
    type PanelEntry,
    type PanelHost,
    type PanelModule,
    type PanelSeries,
    type PanelStop,
    type PanelToast,
} from "./types";

/** A panel's series with a color each: its own, else the next of the dashboard's. */
export function colored(series: readonly PanelSeries[]): Series[] {
    return series.map((s, i) => ({
        ...s,
        color: s.color ?? SERIES_COLORS[i % SERIES_COLORS.length] ?? "#3987e5",
    }));
}

/** A panel's chart options with a format: its own, else the number as the reader's locale writes it. */
export function formatted(options: PanelChartOptions): ChartOptions {
    return { ...options, format: options.format ?? (v => v.toLocaleString()) };
}

/** Reads a panel's script. */
export type PanelLoader = (url: string) => Promise<Partial<PanelModule>>;

/** What a panel needs from the running page. */
export interface PanelServices {
    /** Be told of every message the server relays; returns how to stop. */
    frames(handler: (topic: string, payload: unknown) => void): PanelStop;
    /** Show a notice. */
    toast(notice: PanelToast): void;
    /** Reads a script. */
    load: PanelLoader;
}

/** Whether an MQTT `filter` matches `topic`. */
export function topicMatches(filter: string, topic: string): boolean {
    const wanted = filter.split("/");
    const levels = topic.split("/");
    for (const [index, part] of wanted.entries()) {
        if (part === "#") {
            return true;
        }
        if (index >= levels.length || (part !== "+" && part !== levels[index])) {
            return false;
        }
    }
    return wanted.length === levels.length;
}

/** Say why `value` cannot be mounted, or nothing if it can. */
function refusal(value: Partial<PanelModule>): string | null {
    if (typeof value.mount !== "function") {
        return "Its script exports no mount function.";
    }
    if (value.apiVersion !== PANEL_API_VERSION) {
        return `It was written for dashboard API ${String(value.apiVersion)}, and this dashboard offers ${PANEL_API_VERSION}.`;
    }
    return null;
}

/** See the file's description. */
export class PanelView {
    /** The view's element, kept across visits. */
    readonly root = document.createElement("div");
    /** Set while the panel is open; each visit gets its own. */
    private _visit: object | null = null;
    /** What ends this visit, the panel's own cleanup among it. */
    private _stops: PanelStop[] = [];
    /** Those told when the address moves within the panel. */
    private _routeHandlers = new Set<(path: string[]) => void>();
    /** Where within the panel the address points. */
    private _path: string[] = [];

    constructor(
        private readonly entry: PanelEntry,
        private readonly services: PanelServices,
        /** The panel's settings: the ones Settings changes, when the caller shares them. */
        private readonly settings = new PanelSettings(entry.name, readSettings(entry.settings)),
    ) {
        this.root.className = "af-ext-panel";
    }

    /** Follow the address; returns how to stop, which closes the panel. */
    start(): PanelStop {
        const stop = listen("af-view-changed", route => this._followed(route.view, route.path));
        return () => {
            stop();
            this.close();
        };
    }

    /** The view's element, opening the panel if it is closed. */
    build(): HTMLElement {
        if (!this._visit) {
            void this._open();
        }
        return this.root;
    }

    /** End this visit: the panel cleans up and its element is emptied. */
    close(): void {
        this._visit = null;
        for (const stop of this._stops.splice(0)) {
            try {
                stop();
            } catch (error) {
                console.error(`[panel] ${this.entry.name} failed while closing`, error);
            }
        }
        this._routeHandlers.clear();
        this.root.replaceChildren();
    }

    private _followed(view: string, path: string[]): void {
        if (view !== this.entry.name) {
            this.close();
            return;
        }
        this._path = path;
        for (const handler of [...this._routeHandlers]) {
            handler([...path]);
        }
    }

    private async _open(): Promise<void> {
        const visit = {};
        this._visit = visit;
        this._say("Loading…", "status");
        const url = apiUrl(`/api/ext/${encodeURIComponent(this.entry.name)}/panel.js`);
        try {
            const panel = await this.services.load(url);
            if (this._visit === visit) {
                await this._mount(panel, visit);
            }
        } catch (error) {
            console.error(`[panel] ${this.entry.name} failed`, error);
            if (this._visit === visit) {
                this._say(`${this.entry.title} failed to open. The browser's console says why.`, "alert");
            }
        }
    }

    /** Mount `panel` for `visit`, or say why it cannot be. */
    private async _mount(panel: Partial<PanelModule>, visit: object): Promise<void> {
        const refused = refusal(panel);
        if (refused) {
            this._say(`${this.entry.title} cannot open. ${refused}`, "alert");
            return;
        }
        const stage = document.createElement("div");
        this.root.replaceChildren(stage);
        const ended = await (panel as PanelModule).mount(stage, this._host(visit));
        if (typeof ended !== "function") {
            return;
        }
        if (this._visit === visit) {
            this._stops.push(ended);
        } else {
            ended();
        }
    }

    /** Put one line in the view. */
    private _say(text: string, role: "status" | "alert"): void {
        const line = document.createElement("p");
        line.className = "af-ext-panel-note";
        line.setAttribute("role", role);
        line.textContent = text;
        this.root.replaceChildren(line);
    }

    /** Keep `stop` for the end of `visit`; returns it, removing itself when called early.
     *  A visit already over ends it at once: a panel still mounting when its view closed
     *  must not leave a listener behind, nor hand one to the next visit. */
    private _keep(visit: object, stop: PanelStop): PanelStop {
        if (this._visit !== visit) {
            stop();
            return () => undefined;
        }
        this._stops.push(stop);
        return () => {
            this._stops = this._stops.filter(kept => kept !== stop);
            stop();
        };
    }

    /** The dashboard as the panel mounted for `visit` sees it; inert once that visit is over. */
    private _host(visit: object): PanelHost {
        const open = (): boolean => this._visit === visit;
        return {
            apiVersion: PANEL_API_VERSION,
            name: this.entry.name,
            route: () => [...this._path],
            onRoute: handler => {
                if (open()) {
                    this._routeHandlers.add(handler);
                }
                return this._keep(visit, () => this._routeHandlers.delete(handler));
            },
            navigate: path => {
                // A closed panel moving the address would pull the reader back to it.
                if (open()) {
                    emit("af-route", { view: this.entry.name, path: [...path] });
                }
            },
            request: (path, init) => requestJson(apiUrl(path), init),
            subscribe: (filter, handler) =>
                this._keep(
                    visit,
                    this.services.frames((topic, payload) => {
                        if (topicMatches(filter, topic)) {
                            handler(topic, payload);
                        }
                    }),
                ),
            toast: notice => this.services.toast(notice),
            chart: (series, options) => this._chart(visit, series, options),
            setting: key => this.settings.get(key),
            onSetting: (key, handler) => this._keep(visit, this.settings.follow(key, handler)),
        };
    }

    /** The dashboard's chart for the panel mounted for `visit`, ended with that visit. */
    private _chart(visit: object, series: PanelSeries[], options: PanelChartOptions): PanelChart {
        const chart = new LineChart(colored(series), formatted(options));
        const end = this._keep(visit, () => chart.destroy());
        return {
            element: chart.element,
            update: (next, nextOptions) =>
                chart.update(colored(next), nextOptions ? formatted(nextOptions) : undefined),
            destroy: end,
        };
    }
}
