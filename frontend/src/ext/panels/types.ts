/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * What a package's dashboard panel and the dashboard promise each other.
 *
 * This is a public interface: a panel ships apart from the dashboard, so a change
 * that breaks one raises {@link PANEL_API_VERSION}. Adding to it does not.
 */
import type { Answer } from "../../io/api";
import type { ChartOptions, Format, Series } from "../../ui/charts";

/** The version of {@link PanelHost} this dashboard offers. */
export const PANEL_API_VERSION = 1;

/** What a panel's setting can hold: a switch's state, a choice's value, or a number. */
export type PanelSettingValue = boolean | string | number;

/** What every setting of a panel says of itself. */
interface PanelSettingBase {
    /** What the panel asks for it by. */
    key: string;
    /** The row's name. */
    label: string;
    /** One line under the name, on what it does; may be empty. */
    hint: string;
}

/** One value a panel lets each browser choose, as the server describes it. */
export type PanelSetting =
    | (PanelSettingBase & { kind: "switch"; default: boolean })
    | (PanelSettingBase & {
          kind: "choice";
          default: string;
          /** Each value and what it says, in the order drawn. */
          choices: { value: string; label: string }[];
      })
    | (PanelSettingBase & {
          kind: "number";
          default: number;
          /** The least it may be, when there is one. */
          min?: number;
          /** The most it may be, when there is one. */
          max?: number;
          /** The distance between the values offered, when it matters. */
          step?: number;
      });

/** A panel's tab, as `/api/config` lists it. */
export interface PanelEntry {
    /** The extension's short name: the view's key and address. */
    name: string;
    /** The tab's label. */
    title: string;
    /** The name of one of the dashboard's icons. */
    icon: string;
    /** The text of the package's own icon file, when it ships one: drawn instead of `icon`. */
    icon_svg?: string;
    /** What each browser may choose for the panel. Read with `readSettings`, which keeps the well-formed. */
    settings?: unknown;
}

/** A notice a panel shows. */
export interface PanelToast {
    /** Its heading. */
    title: string;
    /** Its text. */
    message: string;
    /** How it reads: `info` when left out. */
    type?: "info" | "warning" | "error";
}

/** Ends what a host call started. */
export type PanelStop = () => void;

/** A series a panel charts: as the dashboard's, the color chosen for it when left out. */
export type PanelSeries = Omit<Series, "color"> & { color?: string };

/** How a panel's chart is drawn: as the dashboard's, values plainly formatted when no format is given. */
export type PanelChartOptions = Omit<ChartOptions, "format"> & { format?: Format };

/** A chart a panel placed: to be given new series, and ended before the panel is. */
export interface PanelChart {
    /** The chart, for the panel to place. */
    readonly element: HTMLElement;
    /** Draw these series instead, with these options when given; a zoom in place stays. */
    update(series: PanelSeries[], options?: PanelChartOptions): void;
    /** Take it out of the page. The panel unmounting does this for every chart still drawn. */
    destroy(): void;
}

/** The dashboard, as a panel sees it. Everything it starts ends when the panel unmounts. */
export interface PanelHost {
    /** The version of this interface. */
    readonly apiVersion: number;
    /** The panel's name, as in its address. */
    readonly name: string;
    /** Where within the panel the address points: `#/<name>/a/b` is `["a", "b"]`. */
    route(): string[];
    /** Be told when the address moves within the panel. */
    onRoute(handler: (path: string[]) => void): PanelStop;
    /** Move the address within the panel. */
    navigate(path: string[]): void;
    /** JSON from the server, with a timeout. Never rejects: it answers with the body or why not. */
    request<T>(path: string, init?: RequestInit): Promise<Answer<T>>;
    /** Be told of messages on a topic the dashboard relays, `+` and `#` allowed. */
    subscribe(filter: string, handler: (topic: string, payload: unknown) => void): PanelStop;
    /** Show a notice. */
    toast(notice: PanelToast): void;
    /**
     * The dashboard's line chart: thresholds, bands, marks, a choice of how points
     * are drawn, and zoom, as `ChartOptions` describes them. Times are milliseconds
     * since the epoch. Added within version 1, so a panel that may meet an older
     * dashboard checks `typeof host.chart === "function"` first.
     */
    chart(series: PanelSeries[], options: PanelChartOptions): PanelChart;
    /**
     * What this browser chose for the setting `key` the panel declared on the
     * server, its default while nothing was chosen, and `undefined` for a key it
     * did not declare. Added within version 1, so a panel that may meet an older
     * dashboard checks `typeof host.setting === "function"` first.
     */
    setting(key: string): PanelSettingValue | undefined;
    /**
     * Be told when the setting `key` changes while the panel is open, which is
     * when it is changed in another window of this browser. Added with `setting`.
     */
    onSetting(key: string, handler: (value: PanelSettingValue) => void): PanelStop;
}

/** What a panel's script exports. */
export interface PanelModule {
    /** The version of {@link PanelHost} it was written for. */
    apiVersion: number;
    /** Fill `root`; what it returns, or resolves to, is called when the panel closes. */
    mount(root: HTMLElement, host: PanelHost): void | PanelStop | Promise<void | PanelStop>;
}
