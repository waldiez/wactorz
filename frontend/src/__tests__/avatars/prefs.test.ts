/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** This browser's avatar choices: the defaults, what is kept, and what is put on the page. */
import { beforeEach, describe, expect, it } from "vitest";

import {
    AVATAR_PREFS_KEY,
    defaultAvatarPrefs,
    isDisc,
    loadAvatarPrefs,
    saveAvatarPrefs,
} from "../../config/avatarPrefs";
import { applyAvatarPrefs, discColour, discPaint, ringColour } from "../../ui/avatars";
import { luminance } from "../../ui/avatars/svg";

beforeEach(() => {
    localStorage.clear();
});

describe("the avatar choices kept in a browser", () => {
    it("start off, every place ready for when they are turned on, on the purple disc, with a dot", () => {
        expect(loadAvatarPrefs()).toEqual({
            on: false,
            where: { cards: true, chat: true, wiring: true, notifications: true },
            disc: "purple",
            style: "dot",
        });
    });

    it("are read back as they were kept", () => {
        const prefs = defaultAvatarPrefs();
        prefs.on = false;
        prefs.where.wiring = false;
        prefs.disc = "#1E294B";
        prefs.style = "both";
        saveAvatarPrefs(prefs);
        expect(loadAvatarPrefs()).toEqual(prefs);
    });

    it("fall back, choice by choice, where what is kept is not a choice there is", () => {
        const kept = (value: unknown): void => localStorage.setItem(AVATAR_PREFS_KEY, JSON.stringify(value));
        kept({ on: "no", where: { cards: false, chat: "no", attic: true }, disc: "plaid", style: "halo" });
        expect(loadAvatarPrefs()).toEqual({
            ...defaultAvatarPrefs(),
            where: { ...defaultAvatarPrefs().where, cards: false },
        });
        kept({ where: null, disc: "none" });
        expect(loadAvatarPrefs()).toEqual({ ...defaultAvatarPrefs(), disc: "none" });
        for (const junk of [null, 7, "purple"]) {
            kept(junk);
            expect(loadAvatarPrefs()).toEqual(defaultAvatarPrefs());
        }
        localStorage.setItem(AVATAR_PREFS_KEY, "{not json");
        expect(loadAvatarPrefs()).toEqual(defaultAvatarPrefs());
    });

    it("know a disc: one offered by name, or a colour", () => {
        for (const disc of ["purple", "light", "violet", "lilac", "none", "#a1B2c3"]) {
            expect(isDisc(disc)).toBe(true);
        }
        for (const not of ["plaid", "#abc", "", 3, null]) {
            expect(isDisc(not)).toBe(false);
        }
    });
});

describe("how a disc is drawn", () => {
    it("has a ring darker than a light disc, and lighter than a dark one", () => {
        expect(luminance(ringColour("#cb95fa"))).toBeLessThan(luminance("#cb95fa"));
        expect(luminance(ringColour("#facc15"))).toBeLessThan(luminance("#facc15"));
        expect(luminance(ringColour("#1e294b"))).toBeGreaterThan(luminance("#1e294b"));
        expect(luminance(ringColour("#050508"))).toBeGreaterThan(luminance("#050508"));
        // Mid-dark colours, where a darker ring would be lost on the page.
        expect(luminance(ringColour("#0f766e"))).toBeGreaterThan(luminance("#0f766e"));
        expect(luminance(ringColour("#dc2626"))).toBeGreaterThan(luminance("#dc2626"));
    });

    it("fills each disc offered by name, and one of any colour", () => {
        for (const disc of ["purple", "light", "violet", "lilac"]) {
            const paint = discPaint(disc);
            expect(paint?.fill).toContain("radial-gradient");
            expect(paint?.ring).toBe(ringColour(discColour(disc)!));
        }
        expect(discPaint("#1e294b")).toEqual({
            fill: expect.stringContaining("#1e294b") as string,
            ring: ringColour("#1e294b"),
        });
        expect(discColour("#1e294b")).toBe("#1e294b");
    });

    it("is nothing for none, and for a name that is not a disc", () => {
        for (const disc of ["none", "plaid", "toString"]) {
            expect(discPaint(disc)).toBeNull();
            expect(discColour(disc)).toBeNull();
        }
    });
});

describe("the choices put on the page", () => {
    it("name the places, the style, and the disc's colours on the root", () => {
        const root = document.createElement("div");
        const prefs = { ...defaultAvatarPrefs(), on: true };
        prefs.where.wiring = false;
        prefs.style = "state";
        applyAvatarPrefs(prefs, root);
        expect(root.dataset["avatars"]).toBe("cards chat notifications");
        expect(root.dataset["avatarStyle"]).toBe("state");
        expect(root.dataset["avatarDisc"]).toBe("on");
        expect(root.style.getPropertyValue("--af-avatar-disc")).toContain("radial-gradient");
        expect(root.style.getPropertyValue("--af-avatar-ring")).toBe(ringColour("#cb95fa"));
    });

    it("name no place while avatars are off, whatever each place is set to", () => {
        const root = document.createElement("div");
        applyAvatarPrefs({ ...defaultAvatarPrefs(), on: false }, root);
        expect(root.dataset["avatars"]).toBe("");
        expect(root.dataset["avatarDisc"]).toBe("on");
    });

    it("take the disc's colours away with the disc", () => {
        const root = document.createElement("div");
        applyAvatarPrefs(defaultAvatarPrefs(), root);
        applyAvatarPrefs({ ...defaultAvatarPrefs(), disc: "none" }, root);
        expect(root.dataset["avatarDisc"]).toBe("none");
        expect(root.style.getPropertyValue("--af-avatar-disc")).toBe("");
        expect(root.style.getPropertyValue("--af-avatar-ring")).toBe("");
    });

    it("go on the page's own root when no other is named", () => {
        applyAvatarPrefs({
            ...defaultAvatarPrefs(),
            on: true,
            where: { cards: false, chat: false, wiring: false, notifications: false },
        });
        expect(document.documentElement.dataset["avatars"]).toBe("");
        applyAvatarPrefs({ ...defaultAvatarPrefs(), on: true });
        expect(document.documentElement.dataset["avatars"]).toBe("cards chat wiring notifications");
    });
});
