/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The dashboard opens what the address names, follows it when it changes, and
 * says where it is whenever the user moves, through the event bus only.
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";

import { CardDashboard } from "../ui/CardDashboard";
import { emit, listen } from "../events";
import { VIEW_KEY, type Route } from "../router";
import type { AgentInfo } from "../types/agent";

interface Internals {
    view: string;
    _setView(v: string): void;
    _chat: { chatTarget: string; setTarget(name: string): void };
}

function agent(name: string): AgentInfo {
    return { id: name, name, state: "running", protected: false };
}

const AGENTS = [agent("main"), agent("weather")];

describe("CardDashboard and the address", () => {
    let cd: CardDashboard;
    let changes: Route[];
    let heard: EventListener;

    const internals = (): Internals => cd as unknown as Internals;

    function open(): void {
        cd = new CardDashboard();
        cd.show(AGENTS);
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

        expect(internals().view).toBe("feed");
        expect(changes.at(-1)).toEqual({ view: "feed", path: [] });
    });

    it("opens a conversation the address names", () => {
        history.replaceState(null, "", "#/chat/weather");

        open();

        expect(internals().view).toBe("chat");
        expect(internals()._chat.chatTarget).toBe("weather");
        expect(changes.at(-1)).toEqual({ view: "chat", path: ["weather"] });
    });

    it("opens the view last open in this browser when the address names none", () => {
        localStorage.setItem(VIEW_KEY, "settings");

        open();

        expect(internals().view).toBe("settings");
    });

    it("opens the overview on a first visit", () => {
        open();

        expect(internals().view).toBe("overview");
    });

    it("follows the address when it changes", () => {
        open();

        emit("af-route", { view: "feed", path: [] });
        expect(internals().view).toBe("feed");
        emit("af-route", { view: "chat", path: ["main"] });
        expect([internals().view, internals()._chat.chatTarget]).toEqual(["chat", "main"]);
        emit("af-route", { view: "chat", path: ["weather"] });
        expect(internals()._chat.chatTarget).toBe("weather");
    });

    it("says where it is when the user moves", () => {
        open();

        internals()._setView("settings");

        expect(changes.at(-1)).toEqual({ view: "settings", path: [] });
    });

    it("says which conversation the user picks while the chat is open", () => {
        open();
        internals()._setView("chat");

        internals()._chat.setTarget("weather");

        expect(changes.at(-1)).toEqual({ view: "chat", path: ["weather"] });
    });

    it("keeps an extension's view waiting until it registers, the current view shown meanwhile", () => {
        history.replaceState(null, "", "#/models");
        open();
        expect(internals().view).toBe("overview");
        // Not reported, so the address keeps naming the view asked for.
        expect(changes).toEqual([]);

        cd.registerView("models", "grid", "Models", () => document.createElement("div"));

        expect(internals().view).toBe("models");
        expect(changes).toEqual([{ view: "models", path: [] }]);
    });

    it("remembers an agent a link names even when it does not exist, and opens a real one next time", () => {
        history.replaceState(null, "", "#/chat/nobody-by-that-name");
        open();
        expect(internals()._chat.chatTarget).toBe("nobody-by-that-name");
        cd.destroy();

        history.replaceState(null, "", "#");
        localStorage.setItem(VIEW_KEY, "chat");
        open();

        expect(internals()._chat.chatTarget).toBe("main");
    });

    it("drops the wait when the user opens something else first", () => {
        history.replaceState(null, "", "#/models");
        open();

        internals()._setView("feed");
        cd.registerView("models", "grid", "Models", () => document.createElement("div"));

        expect(internals().view).toBe("feed");
    });

    it("stops following the address once hidden", () => {
        open();
        cd.hide();

        emit("af-route", { view: "feed", path: [] });

        expect(internals().view).toBe("overview");
    });
});
