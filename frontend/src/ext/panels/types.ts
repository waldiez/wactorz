/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
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
}

/** What a panel's script exports. */
export interface PanelModule {
    /** The version of {@link PanelHost} it was written for. */
    apiVersion: number;
    /** Fill `root`; what it returns, or resolves to, is called when the panel closes. */
    mount(root: HTMLElement, host: PanelHost): void | PanelStop | Promise<void | PanelStop>;
}
