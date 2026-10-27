/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Opening the dashboard in a test the way the page opens it: a store, the
 * dashboard observing it, and the changes driven through the store or the bus.
 * What the test then checks is what is on screen, found by role, name or test id.
 */
import { AgentStore } from "../agents/AgentStore";
import { emit } from "../events";
import type { AgentInfo } from "../types/agent";
import { CardDashboard } from "../ui/CardDashboard";
import type { View } from "../ui/dashboard/types";
import { allByRole, byRole } from "./queries";

/** A running agent called `name`, whose id is its name unless `over` says otherwise. */
export function agent(name: string, over: Partial<AgentInfo> = {}): AgentInfo {
    return { id: name, name, state: "running", protected: false, ...over };
}

/** A store and the dashboard drawing it. */
export interface Opened {
    store: AgentStore;
    dashboard: CardDashboard;
}

/** Build the dashboard over a store holding `agents`, without showing it. */
export function buildDashboard(agents: AgentInfo[] = []): Opened {
    const store = new AgentStore();
    agents.forEach(a => store.addOrUpdateAgent(a));
    return { store, dashboard: new CardDashboard(store) };
}

/** Build the dashboard over a store holding `agents`, and show it. */
export function openDashboard(agents: AgentInfo[] = []): Opened {
    const opened = buildDashboard(agents);
    opened.dashboard.show();
    return opened;
}

/** Wait for the store to tell the dashboard what changed: it does so once per microtask. */
export function settled(): Promise<void> {
    return new Promise(resolve => queueMicrotask(resolve));
}

/** Open `view`, as following a link to it does. */
export function goTo(view: View, ...path: string[]): void {
    emit("af-route", { view, path });
}

/** The key of the view on screen, from its root's test id. */
export function shownView(): string {
    const root = document.querySelector<HTMLElement>('[data-testid^="view-"]');
    return root?.dataset["testid"]?.slice("view-".length) ?? "";
}

/** Press the header's button for `view`, as a person does. */
export function pressView(label: string): void {
    byRole(allByRole(document, "navigation", "Views")[0]!, "button", label).click();
}

/** The chat's composer. */
export function composer(): HTMLTextAreaElement {
    return byRole(document, "textbox", "Chat message") as HTMLTextAreaElement;
}

/** Who the composer says a message goes to: it names its recipient whatever view is open. */
export function addressee(): string {
    return /^Message @(.*)…$/.exec(composer().placeholder)?.[1] ?? "";
}

/** The agent's card, by what it is called on screen. */
export function cardOf(label: string): HTMLElement {
    return byRole(document, "listitem", label);
}
