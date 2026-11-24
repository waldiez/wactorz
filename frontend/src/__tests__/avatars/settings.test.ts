/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** Settings → Avatars: each choice is kept for this browser and put on the page at once. */
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
    AVATAR_PREFS_KEY,
    defaultAvatarPrefs,
    loadAvatarPrefs,
    saveAvatarPrefs,
} from "../../config/avatarPrefs";
import { avatarsSection } from "../../ui/settings/avatars";
import { dashboardSettings } from "../../ui/settings/dashboardSettings";
import { allByRole, byRole } from "../queries";

const root = document.documentElement;
const draw = (): HTMLElement => document.body.appendChild(avatarsSection().build());
const radio = (body: HTMLElement, name: string): HTMLInputElement =>
    byRole(body, "radio", name) as HTMLInputElement;
const colour = (body: HTMLElement): HTMLInputElement =>
    body.querySelector<HTMLInputElement>('input[type="color"]')!;

/** Pick a radio the way a person does: it becomes the one checked, and says so. */
function pick(body: HTMLElement, name: string): void {
    const chosen = radio(body, name);
    chosen.checked = true;
    chosen.dispatchEvent(new Event("change", { bubbles: true }));
}

beforeEach(() => {
    localStorage.clear();
});

afterEach(() => {
    document.body.replaceChildren();
});

describe("Settings → Avatars", () => {
    it("is one of the dashboard's sections, kept in this browser", () => {
        const section = avatarsSection();
        expect(section).toMatchObject({ key: "avatars", label: "Avatars", scope: "browser", icon: "smile" });
        const settings = dashboardSettings({
            extraViews: () => [],
            haConfigured: () => false,
            tabsChanged: () => {},
            budget: {
                key: "budget",
                label: "Budget",
                line: "",
                scope: "server",
                build: () => document.createElement("div"),
            },
        });
        const page = document.body.appendChild(settings.build());
        expect(allByRole(page, "link").some(link => link.textContent?.includes("Avatars"))).toBe(true);
    });

    it("shows a sample of one agent in every mood", () => {
        const body = draw();
        const sample = body.querySelector<HTMLElement>(".af-avatar-sample")!;
        expect(
            [...sample.querySelectorAll<HTMLElement>(".af-avatar")].map(node => node.dataset["mood"]),
        ).toEqual(["running", "initializing", "late", "stopped", "failed"]);
        expect(
            [...sample.querySelectorAll(".af-avatar-sample-item > span:last-child")].map(n => n.textContent),
        ).toEqual(["running", "starting", "late", "stopped", "failed"]);
    });

    it("starts from the choices as they are kept", () => {
        saveAvatarPrefs({
            ...defaultAvatarPrefs(),
            where: { ...defaultAvatarPrefs().where, chat: false },
            style: "state",
        });
        const body = draw();
        expect((byRole(body, "switch", "Chat") as HTMLInputElement).checked).toBe(false);
        expect((byRole(body, "switch", "Wiring") as HTMLInputElement).checked).toBe(true);
        expect(radio(body, "Ring").checked).toBe(true);
        expect(radio(body, "Purple").checked).toBe(true);
        expect(colour(body).classList.contains("chosen")).toBe(false);
    });

    it("starts with avatars off, and one switch shows them everywhere", () => {
        const body = draw();
        const all = byRole(body, "switch", "Show avatars") as HTMLInputElement;
        expect(all.checked).toBe(false);
        expect(root.dataset["avatars"] ?? "").toBe("");
        all.click();
        expect(loadAvatarPrefs().on).toBe(true);
        expect(root.dataset["avatars"]).toBe("cards chat wiring notifications");
    });

    it("turns a place off and on", () => {
        saveAvatarPrefs({ ...defaultAvatarPrefs(), on: true });
        const body = draw();
        const wiring = byRole(body, "switch", "Wiring") as HTMLInputElement;
        wiring.click();
        expect(loadAvatarPrefs().where.wiring).toBe(false);
        expect(root.dataset["avatars"]).toBe("cards chat notifications");
        wiring.click();
        expect(loadAvatarPrefs().where.wiring).toBe(true);
        expect(root.dataset["avatars"]).toBe("cards chat wiring notifications");
    });

    it("has a switch for every place, each saying what it does", () => {
        const body = draw();
        for (const name of ["Show avatars", "Overview cards", "Chat", "Wiring", "Notifications"]) {
            const box = byRole(body, "switch", name);
            expect(document.getElementById(box.getAttribute("aria-describedby")!)?.textContent).toBeTruthy();
        }
    });

    it("turns them all off with one switch, keeping what each place is set to", () => {
        saveAvatarPrefs({ ...defaultAvatarPrefs(), on: true });
        const body = draw();
        const places = ["Overview cards", "Chat", "Wiring", "Notifications"].map(
            name => byRole(body, "switch", name) as HTMLInputElement,
        );
        places[2]!.click();
        const all = byRole(body, "switch", "Show avatars") as HTMLInputElement;
        expect(all.checked).toBe(true);

        all.click();
        expect(loadAvatarPrefs()).toMatchObject({ on: false, where: { cards: true, wiring: false } });
        expect(root.dataset["avatars"]).toBe("");
        expect(places.every(box => box.disabled)).toBe(true);
        expect(places.map(box => box.checked)).toEqual([true, true, false, true]);

        all.click();
        expect(root.dataset["avatars"]).toBe("cards chat notifications");
        expect(places.some(box => box.disabled)).toBe(false);
    });

    it("opens with the places waiting when avatars are off", () => {
        saveAvatarPrefs({ ...defaultAvatarPrefs(), on: false });
        const body = draw();
        expect((byRole(body, "switch", "Show avatars") as HTMLInputElement).checked).toBe(false);
        expect((byRole(body, "switch", "Chat") as HTMLInputElement).disabled).toBe(true);
    });

    it("changes how the state shows", () => {
        const body = draw();
        pick(body, "Both");
        expect(loadAvatarPrefs().style).toBe("both");
        expect(root.dataset["avatarStyle"]).toBe("both");
        pick(body, "Ring");
        expect(loadAvatarPrefs().style).toBe("state");
        expect(root.dataset["avatarStyle"]).toBe("state");
        pick(body, "Dot");
        expect(root.dataset["avatarStyle"]).toBe("dot");
    });

    it("changes the disc to one offered by name, or to none", () => {
        const body = draw();
        pick(body, "Lilac");
        expect(loadAvatarPrefs().disc).toBe("lilac");
        expect(root.dataset["avatarDisc"]).toBe("on");
        pick(body, "None");
        expect(loadAvatarPrefs().disc).toBe("none");
        expect(root.dataset["avatarDisc"]).toBe("none");
    });

    it("takes a colour of one's own, which is none of the named ones, and gives it up for a named one", () => {
        const body = draw();
        const own = colour(body);
        own.value = "#1e294b";
        own.dispatchEvent(new Event("input", { bubbles: true }));
        expect(loadAvatarPrefs().disc).toBe("#1e294b");
        expect(own.classList.contains("chosen")).toBe(true);
        expect(
            allByRole(byRole(body, "radiogroup", "Disc"), "radio").some(r => (r as HTMLInputElement).checked),
        ).toBe(false);
        expect(root.style.getPropertyValue("--af-avatar-disc")).toContain("#1e294b");

        pick(body, "Violet");
        expect(own.classList.contains("chosen")).toBe(false);
        expect(loadAvatarPrefs().disc).toBe("violet");
    });

    it("opens on a colour of one's own when that is what is kept", () => {
        localStorage.setItem(AVATAR_PREFS_KEY, JSON.stringify({ ...defaultAvatarPrefs(), disc: "#1e294b" }));
        const body = draw();
        expect(colour(body).value).toBe("#1e294b");
        expect(colour(body).classList.contains("chosen")).toBe(true);
        expect(
            allByRole(byRole(body, "radiogroup", "Disc"), "radio").some(r => (r as HTMLInputElement).checked),
        ).toBe(false);
    });

    it("offers the purple in the colour box when there is no disc", () => {
        saveAvatarPrefs({ ...defaultAvatarPrefs(), disc: "none" });
        expect(colour(draw()).value).toBe("#cb95fa");
    });
});
