/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The `/` command list in the chat box: what it offers as you type, the keys
 * that drive it, and what it leaves alone (history, sending a finished command).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

import { ChatInput } from "../ui/dashboard/chatInput";
import {
    CommandCatalog,
    CommandCompletion,
    MAX_SHOWN,
    matchingForms,
} from "../ui/dashboard/commandCompletion";
import type { CommandForm } from "../io/commandsApi";
import { allByRole, byRole } from "./queries";

const FORMS: CommandForm[] = [
    { usage: "/plans", typed: "/plans", summary: "list pending pipeline proposals" },
    { usage: "/plans approve <id>", typed: "/plans approve", summary: "execute a proposed pipeline" },
    { usage: "/plans reject <id>", typed: "/plans reject", summary: "discard a proposed pipeline" },
    { usage: "/help", typed: "/help", summary: "show this help" },
    { usage: "/own", typed: "/own", summary: "" },
];

const key = (k: string, init: KeyboardEventInit = {}) =>
    new KeyboardEvent("keydown", { key: k, cancelable: true, ...init });

describe("matchingForms", () => {
    it("offers the forms that start with what is typed, in their order", () => {
        expect(matchingForms(FORMS, "/pl").map(f => f.usage)).toEqual([
            "/plans",
            "/plans approve <id>",
            "/plans reject <id>",
        ]);
        expect(matchingForms(FORMS, "/PLANS A").map(f => f.usage)).toEqual(["/plans approve <id>"]);
    });

    it("offers nothing once arguments are being typed, or for a message", () => {
        expect(matchingForms(FORMS, "/plans approve 12")).toEqual([]);
        expect(matchingForms(FORMS, "plans")).toEqual([]);
        expect(matchingForms(FORMS, "/plans\nmore")).toEqual([]);
    });

    it("shows a few at a time", () => {
        const many = Array.from({ length: 30 }, (_, i) => ({
            usage: `/c${i}`,
            typed: `/c${i}`,
            summary: "",
        }));
        expect(matchingForms(many, "/")).toHaveLength(MAX_SHOWN);
    });
});

describe("CommandCatalog", () => {
    it("asks the server once", async () => {
        const fetchForms = vi.fn(async () => FORMS);
        const catalog = new CommandCatalog(fetchForms);

        expect(catalog.current()).toBeNull();
        await catalog.load();
        await catalog.load();

        expect(catalog.current()).toEqual(FORMS);
        expect(fetchForms).toHaveBeenCalledTimes(1);
    });

    it("asks again after a failed fetch", async () => {
        const fetchForms = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(FORMS);
        const catalog = new CommandCatalog(fetchForms);

        expect(await catalog.load()).toEqual([]);
        expect(await catalog.load()).toEqual(FORMS);
    });

    it("reads the forms from /api/commands, under the ingress path", async () => {
        window.__WACTORZ_INGRESS_PATH = "/ingress/abc";
        const fetched = vi.fn(
            async () =>
                new Response(JSON.stringify({ commands: [FORMS[0], { usage: 3 }] }), {
                    headers: { "Content-Type": "application/json" },
                }),
        );
        vi.stubGlobal("fetch", fetched);
        try {
            expect(await new CommandCatalog().load()).toEqual([FORMS[0]]);
            expect(fetched).toHaveBeenCalledWith("/ingress/abc/api/commands", expect.anything());
        } finally {
            vi.unstubAllGlobals();
            delete window.__WACTORZ_INGRESS_PATH;
        }
    });
});

interface Harness {
    ci: ChatInput;
    input: HTMLTextAreaElement;
    select: HTMLSelectElement;
    ghost: HTMLElement;
    panel: HTMLElement;
    send: ReturnType<typeof vi.fn>;
}

async function setup(forms: CommandForm[] = FORMS): Promise<Harness> {
    document.body.replaceChildren();
    const catalog = new CommandCatalog(async () => forms);
    await catalog.load();
    const send = vi.fn();
    const ci = new ChatInput(
        { agentNames: () => [], setTarget: vi.fn(), send },
        new CommandCompletion(catalog),
    );
    const ghost = document.createElement("div");
    ghost.className = "af-input-ghost";
    const input = document.createElement("textarea");
    const panel = document.createElement("div");
    const select = document.createElement("select");
    document.body.append(ci.commands.element, ghost, input, panel);
    ci.commands.attach(input);
    return { ci, input, select, ghost, panel, send };
}

function type(h: Harness, text: string): void {
    h.input.value = text;
    h.ci.onChange(h.input, h.select, h.ghost, h.panel);
}

function press(h: Harness, k: string, init: KeyboardEventInit = {}): KeyboardEvent {
    const e = key(k, init);
    h.ci.onKeydown(e, h.input, h.select, h.ghost, h.panel);
    return e;
}

describe("the command list", () => {
    let h: Harness;
    beforeEach(async () => {
        h = await setup();
    });

    it("opens on '/' as a listbox the box controls, the first option highlighted", () => {
        type(h, "/");

        const list = byRole(document.body, "listbox", "Commands");
        expect(list.hidden).toBe(false);
        const options = allByRole(list, "option");
        expect(options).toHaveLength(FORMS.length);
        expect(options[0]!.getAttribute("aria-selected")).toBe("true");
        expect(h.input.getAttribute("aria-autocomplete")).toBe("list");
        expect(h.input.getAttribute("aria-controls")).toBe(list.id);
        expect(h.input.getAttribute("aria-activedescendant")).toBe(options[0]!.id);
    });

    it("shows how each form is typed and what it does", () => {
        type(h, "/plans a");

        const option = byRole(document.body, "option");
        expect(option.textContent).toContain("/plans approve <id>");
        expect(option.textContent).toContain("execute a proposed pipeline");
    });

    it("moves the highlight with the arrows, wrapping at the ends", () => {
        type(h, "/pl");

        press(h, "ArrowDown");
        expect(h.input.getAttribute("aria-activedescendant")).toBe("af-command-option-1");
        press(h, "ArrowUp");
        press(h, "ArrowUp");
        expect(h.input.getAttribute("aria-activedescendant")).toBe("af-command-option-2");
    });

    it("completes with Tab, leaving a space when arguments follow", () => {
        type(h, "/pl");
        press(h, "ArrowDown");

        const e = press(h, "Tab");

        expect(e.defaultPrevented).toBe(true);
        expect(h.input.value).toBe("/plans approve ");
        expect(byRole(document.body, "listbox", "Commands").hidden).toBe(true);
        expect(h.send).not.toHaveBeenCalled();
    });

    it("completes a command with no arguments without a trailing space", () => {
        type(h, "/he");

        press(h, "Enter");

        expect(h.input.value).toBe("/help");
        expect(h.send).not.toHaveBeenCalled();
    });

    it("sends with Enter once the command is typed out", () => {
        type(h, "/help");

        press(h, "Enter");

        expect(h.send).toHaveBeenCalledTimes(1);
    });

    it("closes on Escape, and the arrows go back to history", () => {
        h.ci.recordSent("an earlier message", h.input);
        type(h, "/");

        press(h, "Escape");
        press(h, "ArrowUp");

        expect(byRole(document.body, "listbox", "Commands").hidden).toBe(true);
        expect(h.input.getAttribute("aria-activedescendant")).toBeNull();
        expect(h.input.value).toBe("an earlier message");
    });

    it("completes on a pointer press, without the box losing focus", () => {
        type(h, "/pl");
        const option = allByRole(document.body, "option")[2]!;

        const down = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
        option.dispatchEvent(down);

        expect(down.defaultPrevented).toBe(true);
        expect(h.input.value).toBe("/plans reject ");
    });

    it("shows a command an orchestrator does not describe by its name alone", () => {
        type(h, "/ow");

        expect(byRole(document.body, "option").textContent).toBe("/own");
    });

    it("closes once nothing matches, and for a plain message", () => {
        type(h, "/nothing");
        expect(byRole(document.body, "listbox", "Commands").hidden).toBe(true);
        type(h, "hello");
        expect(byRole(document.body, "listbox", "Commands").hidden).toBe(true);
    });
});

describe("before the commands have arrived", () => {
    it("asks once per keystroke when the server keeps failing, not in a loop", async () => {
        const fetchForms = vi.fn(async (): Promise<CommandForm[]> => {
            throw new Error("503");
        });
        const catalog = new CommandCatalog(fetchForms);
        const completion = new CommandCompletion(catalog);
        const input = document.createElement("textarea");
        completion.attach(input);
        input.value = "/";

        completion.update(input);
        await vi.waitFor(() => expect(fetchForms).toHaveBeenCalledTimes(1));
        await new Promise(resolve => setTimeout(resolve, 20));
        expect(fetchForms).toHaveBeenCalledTimes(1);
        expect(completion.open).toBe(false);

        completion.update(input);
        await vi.waitFor(() => expect(fetchForms).toHaveBeenCalledTimes(2));
    });

    it("opens as soon as they do, if the box still starts with '/'", async () => {
        let answer: (forms: CommandForm[]) => void = () => {};
        const catalog = new CommandCatalog(() => new Promise(resolve => (answer = resolve)));
        const ci = new ChatInput(
            { agentNames: () => [], setTarget: vi.fn(), send: vi.fn() },
            new CommandCompletion(catalog),
        );
        const input = document.createElement("textarea");
        ci.commands.attach(input);
        input.value = "/pl";

        expect(ci.commands.update(input)).toBe(false);
        answer(FORMS);
        await catalog.load();
        await Promise.resolve();

        expect(ci.commands.open).toBe(true);
    });
});
