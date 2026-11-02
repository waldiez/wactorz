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
}

/** What a panel's script exports. */
export interface PanelModule {
    /** The version of {@link PanelHost} it was written for. */
    apiVersion: number;
    /** Fill `root`; what it returns, or resolves to, is called when the panel closes. */
    mount(root: HTMLElement, host: PanelHost): void | PanelStop | Promise<void | PanelStop>;
}
