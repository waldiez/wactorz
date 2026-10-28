/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The dashboard's side of the address: opening what a route names, and saying
 * where the dashboard is. The router (`src/router.ts`) keeps the address; this
 * reads what each view keeps in its route's path.
 *
 * A view's path is its own. The chat keeps the agent whose conversation is
 * open, as `[agent]`, and the Models view the model open, as `[name]`; a view
 * with levels of its own adds what it reads here.
 */
import { emit } from "../../events";
import type { Route } from "../../router";
import { safeStorage } from "../../safeStorage";
import { BUILTIN_VIEWS, SETTINGS_VIEW, type View } from "./types";

/** The browser's memory of the conversation the user last chose, by the agent's id. */
const CHAT_TARGET_KEY = "wactorz-chat-agent-id";

/** What opening a route needs from the dashboard. */
export interface ViewRoutesHost {
    /** The view open now. */
    view(): View;
    /** Open `view`; the dashboard reports it (see `ViewRoutes.current`). */
    setView(view: View): void;
    /** Draw the view open now, without reporting it. */
    render(): void;
    /** The agent whose conversation the chat has open, or "". */
    chatTarget(): string;
    /** Open `agent`'s conversation in the chat. */
    setChatTarget(agent: string): void;
    /** The model the Models view has open, or "". */
    model(): string;
    /** Open the model `name` in the Models view; "" for the list alone. */
    setModel(name: string): void;
}

/** Opens routes for the dashboard, holding one that waits for an extension's view. */
export class ViewRoutes {
    /** A route to an extension's view that has not registered yet. */
    private _pending: Route | null = null;
    /** The views that can be opened: the built-in ones, and each extension's once registered. */
    private readonly _known = new Set<View>([...BUILTIN_VIEWS.map(v => v.key), SETTINGS_VIEW.key]);

    constructor(private readonly host: ViewRoutesHost) {}

    /**
     * Open what `route` names.
     *
     * An extension registers its view when it loads, which can be after the
     * address asking for it was read: such a route waits for it, and is
     * dropped when anything else is opened first. Meanwhile the current view
     * is drawn but not reported, so the address keeps naming the one asked
     * for; if it never registers, a refresh asks again.
     */
    open(route: Route): void {
        if (!this._known.has(route.view)) {
            this._pending = route;
            this.host.render();
            return;
        }
        const agent = route.view === "chat" ? route.path[0] : undefined;
        if (agent && agent !== this.host.chatTarget()) {
            this.host.setChatTarget(agent);
        }
        if (route.view === "models") {
            this.host.setModel(route.path[0] ?? "");
        }
        this.host.setView(route.view);
    }

    /** An extension's view has registered: open it if a route was waiting for it. */
    registered(view: View): void {
        this._known.add(view);
        if (this._pending?.view === view) {
            this.open(this._pending);
        }
    }

    /** Another view was opened: a route still waiting no longer applies. */
    moved(): void {
        this._pending = null;
    }

    /** Where the dashboard is now, for the address. */
    current(): Route {
        const view = this.host.view();
        const at = view === "chat" ? this.host.chatTarget() : view === "models" ? this.host.model() : "";
        return { view, path: at ? [at] : [] };
    }
}

/** The conversation the user last chose in this browser, if any. */
export function rememberedConversation(): string | null {
    return safeStorage.get(CHAT_TARGET_KEY);
}

/**
 * Remember that the user chose the conversation of the agent `id`, called `name`,
 * and with the chat open (`view`), put it in the address. The browser keeps the
 * id, which says which agent exactly; the address says the name, which a person
 * can read and which outlives the agent spawned again.
 */
export function rememberConversation(id: string, name: string, view: View): void {
    safeStorage.set(CHAT_TARGET_KEY, id);
    if (view === "chat") {
        emit("af-view-changed", { view: "chat", path: [name] });
    }
}
