/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The dashboard opens what the address names, follows it when it changes, and
 * says where it is whenever the user moves, through the event bus only.
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";

import type { CardDashboard } from "../ui/CardDashboard";
import { emit, listen } from "../events";
import { VIEW_KEY, type Route } from "../router";
import type { AgentStore } from "../agents/AgentStore";
import { addressee, agent, openDashboard, pressView, settled, shownView } from "./dashboard";
import { byRole, byTestId } from "./queries";

const AGENTS = [agent("main"), agent("weather")];

/** Pick `name` in the chat's target dropdown, as a person does. */
function pick(name: string): void {
    const select = byRole(document, "combobox", "Chat target agent") as HTMLSelectElement;
    select.value = name;
    select.dispatchEvent(new Event("change"));
}

describe("CardDashboard and the address", () => {
    let cd: CardDashboard;
    let changes: Route[];
    let heard: EventListener;

    function open(): void {
        ({ dashboard: cd } = openDashboard(AGENTS));
    }

    beforeEach(() => {
        document.body.replaceChildren();
        localStorage.clear();
        history.replaceState(null, "", "#");
        changes = [];
        heard = listen("af-view-changed", r => changes.push(r));
    });

    afterEach(() => {
        document.removeEventListener("af-view-changed", heard);
        cd.destroy();
    });

    it("opens the view the address names, and says so", () => {
        history.replaceState(null, "", "#/feed");

        open();

        expect(shownView()).toBe("feed");
        expect(changes.at(-1)).toEqual({ view: "feed", path: [] });
    });

    it("opens a conversation the address names", () => {
        history.replaceState(null, "", "#/chat/weather");

        open();

        expect(shownView()).toBe("chat");
        expect(addressee()).toBe("weather");
        expect(changes.at(-1)).toEqual({ view: "chat", path: ["weather"] });
    });

    it("renames the open conversation when another agent takes its name, and back when it goes", async () => {
        history.replaceState(null, "", "#/chat/weather");
        let store: AgentStore;
        ({ dashboard: cd, store } = openDashboard(AGENTS));
        const title = () => byTestId(document, "chat-title").textContent;
        expect(title()).toBe("@weather");

        store.addOrUpdateAgent(agent("weather", { id: "weather-on-pi", node: "pi" }));
        await settled();

        expect(title()).toBe("@weather@main");
        expect(changes.at(-1)).toEqual({ view: "chat", path: ["weather@main"], replace: true });

        store.removeAgent("weather-on-pi");
        await settled();

        expect(title()).toBe("@weather");
        expect(changes.at(-1)).toEqual({ view: "chat", path: ["weather"], replace: true });
    });

    it("leaves the address alone when an agent of another name comes", async () => {
        history.replaceState(null, "", "#/chat/weather");
        let store: AgentStore;
        ({ dashboard: cd, store } = openDashboard(AGENTS));
        const said = changes.length;

        store.addOrUpdateAgent(agent("counter", { node: "pi" }));
        await settled();

        expect(changes).toHaveLength(said);
    });

    it("opens the view last open in this browser when the address names none", () => {
        localStorage.setItem(VIEW_KEY, "settings");

        open();

        expect(shownView()).toBe("settings");
    });

    it("opens the overview on a first visit", () => {
        open();

        expect(shownView()).toBe("overview");
    });

    it("follows the address when it changes", () => {
        open();

        emit("af-route", { view: "feed", path: [] });
        expect(shownView()).toBe("feed");
        emit("af-route", { view: "chat", path: ["main"] });
        expect([shownView(), addressee()]).toEqual(["chat", "main"]);
        emit("af-route", { view: "chat", path: ["weather"] });
        expect(addressee()).toBe("weather");
    });

    it("says where it is when the user moves", () => {
        open();

        pressView("Settings");

        expect(changes.at(-1)).toEqual({ view: "settings", path: [] });
    });

    it("says which conversation the user picks while the chat is open", () => {
        open();
        pressView("Chat");

        pick("weather");

        expect(changes.at(-1)).toEqual({ view: "chat", path: ["weather"] });
    });

    it("keeps an extension's view waiting until it registers, the current view shown meanwhile", () => {
        history.replaceState(null, "", "#/experiments");
        open();
        expect(shownView()).toBe("overview");
        // Not reported, so the address keeps naming the view asked for.
        expect(changes).toEqual([]);

        cd.registerView("experiments", "grid", "Experiments", () => document.createElement("div"));

        expect(shownView()).toBe("experiments");
        expect(changes).toEqual([{ view: "experiments", path: [] }]);
    });

    it("remembers an agent a link names even when it does not exist, and opens a real one next time", () => {
        history.replaceState(null, "", "#/chat/nobody-by-that-name");
        open();
        expect(addressee()).toBe("nobody-by-that-name");
        cd.destroy();

        history.replaceState(null, "", "#");
        localStorage.setItem(VIEW_KEY, "chat");
        open();

        expect(addressee()).toBe("main");
    });

    it("drops the wait when the user opens something else first", () => {
        history.replaceState(null, "", "#/models");
        open();

        pressView("Feed");
        cd.registerView("models", "grid", "Models", () => document.createElement("div"));

        expect(shownView()).toBe("feed");
    });

    it("stops following the address once hidden", () => {
        open();
        cd.hide();

        emit("af-route", { view: "feed", path: [] });

        expect(shownView()).toBe("overview");
    });
});
