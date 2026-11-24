/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** The avatar as an element, avatars found by a name alone, and how the page starts showing them. */
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { AVATAR_PREFS_KEY, defaultAvatarPrefs } from "../../config/avatarPrefs";
import { avatar, facesByName, paintAvatar, setAvatarMood, showAvatars } from "../../ui/avatars";
import { lookFor } from "../../ui/avatars/cast";
import { avatarUrl } from "../../ui/avatars/draw";
import { toast } from "../../ui/ToastManager";
import { agent } from "../dashboard";

const src = (node: HTMLElement): string => node.querySelector("img")?.getAttribute("src") ?? "";

beforeEach(() => {
    localStorage.clear();
});

afterEach(() => {
    document.body.querySelectorAll(".wz-toast").forEach(node => node.remove());
});

describe("an avatar", () => {
    it("is an image of the agent's character, with nothing for a screen reader to read", () => {
        const node = avatar({ name: "main", avatar: { kind: "main" } }, "running", "card");
        expect(node.className).toBe("af-avatar af-avatar-card");
        expect(node.getAttribute("aria-hidden")).toBe("true");
        expect(node.dataset["mood"]).toBe("running");
        expect(node.querySelector("img")?.alt).toBe("");
        expect(src(node)).toBe(avatarUrl(lookFor("main", { kind: "main" }, "running"), "full"));
    });

    it("shows head and shoulders when small", () => {
        const node = avatar({ name: "main", avatar: { kind: "main" } }, "running", "small");
        expect(src(node)).toBe(avatarUrl(lookFor("main", { kind: "main" }, "running"), "head"));
    });

    it("is painted again for another mood or another agent, and left alone for the same", () => {
        const node = avatar({ name: "weather" }, "running", "small");
        const img = node.querySelector("img")!;
        const awake = src(node);
        let sets = 0;
        const attribute = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "src")!;
        Object.defineProperty(img, "src", {
            configurable: true,
            get: () => attribute.get!.call(img) as string,
            set: (value: string) => {
                sets += 1;
                attribute.set!.call(img, value);
            },
        });

        paintAvatar(node, { name: "weather" }, "running");
        expect(sets).toBe(0);
        paintAvatar(node, { name: "weather" }, "stopped");
        expect(sets).toBe(1);
        expect(src(node)).not.toBe(awake);
        expect(node.dataset["mood"]).toBe("stopped");
        paintAvatar(node, { name: "recorder" }, "stopped");
        expect(sets).toBe(2);
    });

    it("changes mood on its own, keeping whose character it is", () => {
        const node = avatar({ name: "weather", avatar: { kind: "weather-agent" } }, "running", "card");
        setAvatarMood(node, "late");
        expect(node.dataset["mood"]).toBe("late");
        expect(src(node)).toBe(avatarUrl(lookFor("weather", { kind: "weather-agent" }, "late"), "full"));
        const late = src(node);
        setAvatarMood(node, "late");
        expect(src(node)).toBe(late);
    });

    it("leaves an element it did not make as it is", () => {
        const stranger = document.createElement("span");
        setAvatarMood(stranger, "late");
        expect(stranger.dataset["mood"]).toBeUndefined();
        paintAvatar(stranger, { name: "x" }, "late");
        expect(stranger.dataset["mood"]).toBe("late");
    });
});

describe("an avatar found by a name alone", () => {
    const known = [
        agent("weather", { avatar: { kind: "weather-agent" } }),
        agent("twin", { id: "t1", avatar: { kind: "flic" } }),
        agent("twin", { id: "t2", avatar: { kind: "rule" } }),
    ];
    const faceOf = facesByName(() => known);

    it("is the agent's own when one agent has the name", () => {
        expect(src(faceOf("weather"))).toBe(
            avatarUrl(lookFor("weather", { kind: "weather-agent" }, "running"), "head"),
        );
    });

    it("is made up from the name when nobody has it, or two do", () => {
        expect(src(faceOf("gone"))).toBe(avatarUrl(lookFor("gone", undefined, "running"), "head"));
        expect(src(faceOf("twin"))).toBe(avatarUrl(lookFor("twin", undefined, "running"), "head"));
    });

    it("looks among the agents as they are when asked", () => {
        const agents = [agent("late-comer")];
        const lookup = facesByName(() => agents);
        const before = src(lookup("late-comer"));
        agents[0] = agent("late-comer", { avatar: { kind: "main" } });
        expect(src(lookup("late-comer"))).not.toBe(before);
    });
});

describe("starting to show avatars", () => {
    it("puts this browser's choices on the page", () => {
        localStorage.setItem(
            AVATAR_PREFS_KEY,
            JSON.stringify({ ...defaultAvatarPrefs(), style: "state", disc: "none" }),
        );
        showAvatars(() => []);
        expect(document.documentElement.dataset["avatarStyle"]).toBe("state");
        expect(document.documentElement.dataset["avatarDisc"]).toBe("none");
    });

    it("shows on a notification from an agent its avatar, beside the initials it replaces", () => {
        showAvatars(() => [agent("weather", { avatar: { kind: "weather-agent" } })]);
        toast.show({ type: "chat", title: "weather", message: "Sunny." });
        const badge = document.body.querySelector<HTMLElement>(".wz-toast__avatar")!;
        expect(badge.classList.contains("wz-toast__avatar--face")).toBe(true);
        expect(badge.textContent).toBe("WE");
        expect(src(badge.querySelector<HTMLElement>(".af-avatar")!)).toBe(
            avatarUrl(lookFor("weather", { kind: "weather-agent" }, "running"), "head"),
        );
    });

    it("leaves the dashboard's own notifications with their initials alone", () => {
        showAvatars(() => []);
        toast.show({ type: "alert-error", title: "Upload failed", message: "Too large." });
        const badge = document.body.querySelector<HTMLElement>(".wz-toast__avatar")!;
        expect(badge.querySelector(".af-avatar")).toBeNull();
        expect(badge.classList.contains("wz-toast__avatar--face")).toBe(false);
        expect(badge.textContent).toBe("UF");
    });
});
