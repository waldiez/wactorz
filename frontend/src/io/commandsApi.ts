/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The commands the chat can send, as `GET /api/commands` lists them. It asks the
 * orchestrator that answers the chat, so what is listed is what will be answered.
 */
import { apiUrl, requestJson } from "./api";

/** One way to type a command, as the server lists it. */
export interface CommandForm {
    /** What is typed, arguments in `<…>` and optional parts in `[…]`. */
    usage: string;
    /** What is typed before the first argument: what accepting a form puts in the box. */
    typed: string;
    /** What it does; empty for an orchestrator that does not say. */
    summary: string;
}

function isForm(value: unknown): value is CommandForm {
    const v = value as Partial<CommandForm> | null;
    return typeof v?.usage === "string" && typeof v.typed === "string" && typeof v.summary === "string";
}

/** The forms the server lists. Rejects when it could not be asked, so the caller can ask again. */
export async function fetchCommandForms(): Promise<CommandForm[]> {
    const got = await requestJson<{ commands?: unknown } | null>(apiUrl("/api/commands"));
    if (!got.ok) {
        throw new Error(got.error);
    }
    const commands = got.value?.commands;
    return Array.isArray(commands) ? commands.filter(isForm) : [];
}
