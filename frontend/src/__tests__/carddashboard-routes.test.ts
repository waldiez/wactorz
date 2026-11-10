/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The dashboard opens what the address names, follows it when it changes, and
 * says where it is whenever the user moves, through the event bus only.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import type { CardDashboard } from "../ui/CardDashboard";
import { emit, listen } from "../events";
import { VIEW_KEY, type Route } from "../router";
import { setMuted } from "../io/soundMute";
import { provideVoiceControls, voiceControls } from "../io/voiceControls";

/** The voice controls the dashboard has with no extension to speak. */
const silent = voiceControls();
import type { AgentStore } from "../agents/AgentStore";
import { addressee, agent, openDashboard, pressView, settled, shownView } from "./dashboard";
import { allByRole, byRole, byTestId } from "./queries";

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
    let heard: () => void;

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
        heard();
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

    it("opens the section of Settings the address names, and says so", () => {
        history.replaceState(null, "", "#/settings/sound");

        open();

        expect(shownView()).toBe("settings");
        expect(byRole(document, "region", "Sound")).toBeTruthy();
        expect(changes.at(-1)).toEqual({ view: "settings", path: ["sound"] });
    });

    it("opens the first section of Settings for an address naming none, or one there is not", () => {
        history.replaceState(null, "", "#/settings/nowhere");

        open();

        expect(byRole(document, "region", "Budget")).toBeTruthy();
        expect(changes.at(-1)).toEqual({ view: "settings", path: [] });
    });

    it("takes a tab hidden in Settings out of the header at once, and it still opens by its address", () => {
        history.replaceState(null, "", "#/settings/tabs");
        open();
        const views = () => allByRole(document, "navigation", "Views")[0]!;
        expect(allByRole(views(), "button", "Feed")).toHaveLength(1);

        const feed = byRole(document, "checkbox", "Show the Feed tab") as HTMLInputElement;
        feed.checked = false;
        feed.dispatchEvent(new Event("change"));

        expect(allByRole(views(), "button", "Feed")).toHaveLength(0);
        emit("af-route", { view: "feed", path: [] });
        expect(shownView()).toBe("feed");
    });

    describe("a spend figure arriving while Settings is open", () => {
        /** Hold the cost answer back until `arrive` is called, as a slow poll does. */
        function holdCost(): (info: object) => void {
            let arrive: (r: Response) => void = () => {};
            vi.spyOn(globalThis, "fetch").mockImplementation(url =>
                String(url).includes("/api/cost")
                    ? new Promise<Response>(resolve => {
                          arrive = resolve;
                      })
                    : Promise.resolve(new Response("{}", { status: 200 })),
            );
            return info =>
                arrive(
                    new Response(JSON.stringify(info), {
                        status: 200,
                        headers: { "Content-Type": "application/json" },
                    }),
                );
        }
        const SPEND = { limit_usd: 5, period: "monthly", spend_usd: 1 };

        afterEach(() => vi.restoreAllMocks());

        it("leaves another section's control, and its focus, as they were", async () => {
            const arrive = holdCost();
            history.replaceState(null, "", "#/settings/tabs");
            open();
            const feed = byRole(document, "checkbox", "Show the Feed tab");
            feed.focus();

            arrive(SPEND);
            await settled();
            await new Promise(resolve => setTimeout(resolve, 0));

            expect(feed.isConnected).toBe(true);
            expect(document.activeElement).toBe(feed);
        });

        it("draws Budget again with it, when Budget is open", async () => {
            const arrive = holdCost();
            history.replaceState(null, "", "#/settings/budget");
            open();

            arrive(SPEND);

            await vi.waitFor(() =>
                expect(byRole(document, "region", "Budget").textContent).toContain("$1.0000 / $5.00"),
            );
        });
    });

    it("shows on the speaker a mute made anywhere", () => {
        // A beep chosen, so there is something to mute.
        provideVoiceControls({ ...voiceControls(), beepEnabled: true });
        open();

        setMuted(true);
        expect(allByRole(document, "button", "Unmute sound")).toHaveLength(1);
        setMuted(false);
        expect(allByRole(document, "button", "Mute sound")).toHaveLength(1);
        provideVoiceControls(silent);
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

    it("keeps the levels of an extension's view in the address, and returns to them", () => {
        history.replaceState(null, "", "#/experiments/runs/7");
        open();
        cd.registerView("experiments", "grid", "Experiments", () => document.createElement("div"));

        expect(changes.at(-1)).toEqual({ view: "experiments", path: ["runs", "7"] });
        pressView("Feed");
        pressView("Experiments");
        expect(changes.at(-1)).toEqual({ view: "experiments", path: ["runs", "7"] });
        emit("af-route", { view: "experiments", path: ["runs", "8"] });
        expect(changes.at(-1)).toEqual({ view: "experiments", path: ["runs", "8"] });
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
