/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach } from "vitest";
import { confirmDialog } from "../ui/dashboard/confirmDialog";

const opts = { title: "Delete agent?", message: "Are you sure?", confirmLabel: "Delete" };

function overlay(): HTMLElement {
    return document.querySelector<HTMLElement>(".af-confirm-backdrop")!;
}

function click(selector: string): void {
    document.querySelector<HTMLButtonElement>(selector)!.click();
}

function press(key: string, shiftKey = false): void {
    document.dispatchEvent(new KeyboardEvent("keydown", { key, shiftKey }));
}

describe("confirmDialog", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("asks the question it was given", () => {
        const answer = confirmDialog(opts);
        expect(document.querySelector(".af-confirm-title")!.textContent).toBe("Delete agent?");
        expect(document.querySelector(".af-confirm-message")!.textContent).toBe("Are you sure?");
        expect(document.querySelector(".af-confirm-ok")!.textContent).toBe("Delete");
        click(".af-confirm-cancel");
        return expect(answer).resolves.toBe(false);
    });

    it("announces itself as a modal dialog", () => {
        const answer = confirmDialog(opts);
        expect(overlay().getAttribute("role")).toBe("dialog");
        expect(overlay().getAttribute("aria-modal")).toBe("true");
        expect(overlay().getAttribute("aria-label")).toBe("Delete agent?");
        click(".af-confirm-cancel");
        return answer;
    });

    it("resolves true only when the confirming button is pressed", async () => {
        const answer = confirmDialog(opts);
        click(".af-confirm-ok");
        await expect(answer).resolves.toBe(true);
        expect(document.querySelector(".af-confirm-backdrop")).toBeNull();
    });

    it("resolves false on cancel", async () => {
        const answer = confirmDialog(opts);
        click(".af-confirm-cancel");
        await expect(answer).resolves.toBe(false);
        expect(document.querySelector(".af-confirm-backdrop")).toBeNull();
    });

    it("resolves false on Escape", async () => {
        const answer = confirmDialog(opts);
        press("Escape");
        await expect(answer).resolves.toBe(false);
    });

    it("resolves false when the backdrop is clicked", async () => {
        const answer = confirmDialog(opts);
        overlay().click();
        await expect(answer).resolves.toBe(false);
    });

    it("stays open when the box itself is clicked", () => {
        const answer = confirmDialog(opts);
        document.querySelector<HTMLElement>(".af-confirm")!.click();
        expect(document.querySelector(".af-confirm-backdrop")).not.toBeNull();
        click(".af-confirm-cancel");
        return answer;
    });

    it("renders the message as text, never as markup", () => {
        // An agent is named by whoever spawned it, and the name arrives over MQTT.
        const answer = confirmDialog({ ...opts, message: "delete <img src=x onerror=alert(1)>?" });
        expect(document.querySelector(".af-confirm-message")!.querySelector("img")).toBeNull();
        expect(document.querySelector(".af-confirm-message")!.textContent).toContain("<img");
        click(".af-confirm-cancel");
        return answer;
    });

    it("focuses cancel, so nothing destructive is one keypress away", () => {
        const answer = confirmDialog(opts);
        expect(document.activeElement).toBe(document.querySelector(".af-confirm-cancel"));
        click(".af-confirm-cancel");
        return answer;
    });

    it("keeps Tab inside the dialog", () => {
        const answer = confirmDialog(opts);
        press("Tab");
        expect(document.activeElement).toBe(document.querySelector(".af-confirm-ok"));
        press("Tab");
        expect(document.activeElement).toBe(document.querySelector(".af-confirm-cancel"));
        press("Tab", true);
        expect(document.activeElement).toBe(document.querySelector(".af-confirm-ok"));
        click(".af-confirm-cancel");
        return answer;
    });

    it("returns focus to whatever opened it", async () => {
        const opener = document.createElement("button");
        document.body.appendChild(opener);
        opener.focus();

        const answer = confirmDialog(opts);
        click(".af-confirm-ok");
        await answer;

        expect(document.activeElement).toBe(opener);
    });

    it("keeps one open at a time, and the replaced one settles as a cancel", async () => {
        const first = confirmDialog(opts);
        const second = confirmDialog({ ...opts, title: "Reset everything?" });

        expect(document.querySelectorAll(".af-confirm-backdrop").length).toBe(1);
        await expect(first).resolves.toBe(false);

        click(".af-confirm-ok");
        await expect(second).resolves.toBe(true);
    });

    it("does not leave the replaced instance listening on document", async () => {
        const first = confirmDialog(opts);
        const second = confirmDialog(opts);
        await first;

        // one Escape must settle the one that is open, not merely the stale one
        press("Escape");
        await expect(second).resolves.toBe(false);
        expect(document.querySelector(".af-confirm-backdrop")).toBeNull();
    });

    it("leaves Tab alone when there is nothing inside to focus", () => {
        const answer = confirmDialog(opts);
        const buttons = overlay().querySelectorAll("button");
        buttons.forEach(btn => btn.remove());

        press("Tab"); // the guard: no focusables, so nothing to cycle through

        expect(document.querySelector(".af-confirm-backdrop")).not.toBeNull();
        press("Escape");
        return expect(answer).resolves.toBe(false);
    });

    it("stops answering the keyboard once it has settled", async () => {
        const answer = confirmDialog(opts);
        click(".af-confirm-cancel");
        await answer;

        press("Escape"); // no dialog, no listener — must not throw
        expect(document.querySelector(".af-confirm-backdrop")).toBeNull();
    });
});
