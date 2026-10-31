/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The Models view as a person uses it: the address opens a model, its moves
 * are buttons that ask first (with a reason), a refusal is said, and a pointer
 * moved anywhere shows here.
 */
import { REQUEST_TIMEOUT_MS } from "../io/api";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import type { CardDashboard } from "../ui/CardDashboard";
import { emit, listen } from "../events";
import type { Route } from "../router";
import { toast } from "../ui/ToastManager";
import { openDashboard, shownView } from "./dashboard";
import { allByRole, byRole, byTestId } from "./queries";

const VERSION = (n: number) => ({
    name: "hvac",
    version: n,
    sha256: `sha-${n}`,
    format: "onnx",
    size: 2048,
    runs_code: false,
    added_at: 1_700_000_000 + n,
    metadata:
        n === 8 ? { description: "Autoencoder on supply-air temperature", source: "cli", f1: 0.86 } : {},
    reference: `hvac:${n}`,
});
const POINTER = (which: string, n: number, previous: number | null = null) => ({
    name: "hvac",
    pointer: which,
    version: n,
    previous,
    moved_at: 1_700_000_100,
    by: "cli",
    reason: null,
    holds: true,
});
const HVAC = {
    name: "hvac",
    versions: [6, 7, 8].map(VERSION),
    pointers: { active: POINTER("active", 7, 6), shadow: POINTER("shadow", 8) },
};
const MODELS = [
    { name: "hvac", newest: VERSION(8), versions: 3, pointers: HVAC.pointers },
    { name: "pump", newest: { ...VERSION(2), name: "pump" }, versions: 2, pointers: {} },
];
const MOVES = [
    {
        at: 1_700_000_100,
        pointer: "shadow",
        change: "moved",
        version: 8,
        from: null,
        by: "agent:controller",
        reason: "regime change",
    },
];

/** A server holding hvac and pump; it answers moves as `moving` says. */
function server(moving: () => Response = () => Response.json({ pointer: {} })) {
    return vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === "POST" || init?.method === "DELETE") {
            return moving();
        }
        if (url.endsWith("/api/models")) {
            return Response.json({ models: MODELS });
        }
        if (url.endsWith("/moves")) {
            return Response.json({ name: "hvac", moves: MOVES });
        }
        if (url.endsWith("/api/models/hvac")) {
            return Response.json(HVAC);
        }
        return Response.json({ error: "not here" }, { status: 404 });
    });
}

describe("the Models view", () => {
    let cd: CardDashboard;
    let fetched: ReturnType<typeof server>;
    let changes: Route[];
    let heard: () => void;

    async function open(hash: string, answering = server()): Promise<void> {
        fetched = answering;
        vi.stubGlobal("fetch", fetched);
        history.replaceState(null, "", hash);
        ({ dashboard: cd } = openDashboard([]));
        await vi.waitFor(() =>
            expect(document.querySelector(".af-model-detail, .af-models-list a")).not.toBeNull(),
        );
    }

    const model = () => byRole(document, "article", "hvac");
    const press = (name: string) => byRole(document, "button", name).click();
    const posted = () => fetched.mock.calls.filter(([, init]) => init?.method && init.method !== "GET");

    beforeEach(() => {
        document.body.replaceChildren();
        localStorage.clear();
        changes = [];
        heard = listen("af-view-changed", r => changes.push(r));
    });

    afterEach(() => {
        heard();
        cd.destroy();
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it("opens the model the address names, beside the list of every model", async () => {
        await open("#/models/hvac");
        await vi.waitFor(() => model());

        expect(shownView()).toBe("models");
        const links = allByRole(byRole(document, "list", "Stored models"), "link");
        expect(links.map(a => a.getAttribute("href"))).toEqual(["#/models/hvac", "#/models/pump"]);
        expect(links[0]?.getAttribute("aria-current")).toBe("page");
        expect(links[0]?.textContent).toContain("on trial");
        expect(changes.at(-1)).toEqual({ view: "models", path: ["hvac"] });
        expect(byRole(document, "heading", "Models")).toBeTruthy();
        expect(byRole(document, "complementary").textContent).toContain("Stored models2 names");
    });

    it("leads with the promote and marks the rollback, the clear left plain", async () => {
        await open("#/models/hvac");
        await vi.waitFor(() => model());

        const tone = (name: string) => byRole(model(), "button", name).classList;
        expect(tone("Promote v8").contains("primary")).toBe(true);
        expect(tone("Roll back to v6").contains("danger")).toBe(true);
        expect([...tone("Clear shadow")].some(c => c === "primary" || c === "danger")).toBe(false);
    });

    it("marks the served, the tried and the previous version, in words", async () => {
        await open("#/models/hvac");
        await vi.waitFor(() => model());

        const steps = allByRole(byRole(model(), "list", "Versions over time"), "listitem");
        expect(steps.map(s => s.getAttribute("aria-label"))).toEqual([
            "v6, previous",
            "v7, active",
            "v8, shadow",
        ]);
        expect(model().textContent).toContain("Autoencoder on supply-air temperature");
        const v8 = byRole(model(), "table").querySelector("tbody tr")!;
        expect([...v8.querySelectorAll("td")].slice(2, 6).map(td => td.textContent)).toEqual([
            "cli",
            "onnx",
            "2 KB",
            "f1=0.86",
        ]);
        expect(byRole(model(), "list", "Pointer moves").textContent).toContain(
            "shadow → v8, by agent:controller",
        );
        expect(byRole(model(), "list", "Pointer moves").textContent).toContain("“regime change”");
    });

    it("asks before a move, with a reason, and sends both", async () => {
        await open("#/models/hvac");
        await vi.waitFor(() => model());

        press("Promote v8");
        const dialog = byTestId(document, "confirm-dialog");
        expect(byTestId(dialog, "confirm-message").textContent).toContain(
            "hvac v8 becomes the version agents serve",
        );
        (byRole(dialog, "textbox", "Reason (optional)") as HTMLTextAreaElement).value = "  F1 up by 0.02 ";
        byTestId(dialog, "confirm-ok").click();

        await vi.waitFor(() => expect(posted()).toHaveLength(1));
        expect(posted()[0]).toEqual([
            "/api/models/hvac/promote",
            expect.objectContaining({ method: "POST", body: JSON.stringify({ reason: "F1 up by 0.02" }) }),
        ]);
    });

    it("moves the model whose button was pressed, though another was opened while asked", async () => {
        await open("#/models/hvac");
        await vi.waitFor(() => model());

        press("Promote v8");
        emit("af-route", { view: "models", path: ["pump"] });
        byTestId(document, "confirm-ok").click();

        await vi.waitFor(() => expect(posted()).toHaveLength(1));
        expect(posted()[0]?.[0]).toBe("/api/models/hvac/promote");
    });

    it("sends nothing when the question is cancelled", async () => {
        await open("#/models/hvac");
        await vi.waitFor(() => model());

        press("Roll back to v6");
        byTestId(document, "confirm-cancel").click();
        await Promise.resolve();

        expect(posted()).toEqual([]);
    });

    it("clears a shadow after asking why, and sends the reason", async () => {
        await open("#/models/hvac");
        await vi.waitFor(() => model());

        press("Clear shadow");
        const dialog = byTestId(document, "confirm-dialog");
        (byRole(dialog, "textbox", "Reason (optional)") as HTMLTextAreaElement).value = "noisy at night";
        byTestId(dialog, "confirm-ok").click();

        await vi.waitFor(() => expect(posted()).toHaveLength(1));
        expect(posted()[0]).toEqual([
            "/api/models/hvac/shadow",
            expect.objectContaining({ method: "DELETE", body: JSON.stringify({ reason: "noisy at night" }) }),
        ]);
    });

    it("says the server's refusal in a notice", async () => {
        const shown = vi.spyOn(toast, "show");
        await open(
            "#/models/hvac",
            server(() => Response.json({ error: "hvac:8 is already active" }, { status: 409 })),
        );
        await vi.waitFor(() => model());

        press("Try v6 as shadow");
        byTestId(document, "confirm-ok").click();

        await vi.waitFor(() =>
            expect(shown).toHaveBeenCalledWith(
                expect.objectContaining({ message: "hvac:8 is already active" }),
            ),
        );
    });

    /** A server whose hvac has v8 on trial, its figures from `trial`, its ended trials from `past`. */
    function onTrial(
        trial: () => Response,
        past: () => Response = () => Response.json({ trials: [] }),
        overTime: () => Response = () => Response.json({ buckets: [] }),
    ) {
        const reads = server();
        return vi.fn(async (url: string, init?: RequestInit) => {
            if (url.endsWith("/trial")) {
                return trial();
            }
            if (url.endsWith("/trials")) {
                return past();
            }
            if (url.endsWith("/trial/series")) {
                return overTime();
            }
            return reads(url, init);
        });
    }

    const FIGURES = {
        name: "hvac",
        since: 1_760_000_000,
        until: 1_760_003_600,
        pairs: 41230,
        agreement: 0.914,
        flagged: { active: 41, shadow: 38 },
        shadow_errors: 2,
        latency: { active: { mean_ms: 1, p95_ms: 4.1 }, shadow: { mean_ms: 1, p95_ms: 4.6 } },
        score: { active_mean: 0, shadow_mean: 0, difference_mean: 0, scored: 0 },
    };

    const trialCard = () => byRole(document, "region", "Shadow trial");

    it("shows what the trial under way shows so far", async () => {
        await open("#/models/hvac", onTrial(() => Response.json(FIGURES)) as never);

        await vi.waitFor(() => expect(trialCard().textContent).toContain("91.4%"));

        const text = trialCard().textContent;
        expect(text).toContain("Shadow trial: v8 beside v7");
        expect(text).toContain("41 → 38");
        expect(text).toContain("4.1 → 4.6");
        // No output carried a score, so no line says the scores were equal.
        expect(text).not.toContain("Mean score");
    });

    const bucket = (ts: number, score: number | null, ms: number | null) => ({
        ts,
        pairs: ms === null ? 0 : 5,
        active_score: score,
        shadow_score: score,
        active_ms: ms,
        shadow_ms: ms,
    });
    const noPast = () => Response.json({ trials: [] });

    it("charts the score and the latency over the trial", async () => {
        const buckets = [bucket(1_760_000_000, 2, 3), bucket(1_760_000_600, 2.5, 4)];
        await open(
            "#/models/hvac",
            onTrial(
                () => Response.json(FIGURES),
                noPast,
                () => Response.json({ buckets }),
            ) as never,
        );

        await vi.waitFor(() => expect(trialCard().querySelectorAll("figure")).toHaveLength(2));
        const titles = [...trialCard().querySelectorAll("figcaption")].map(t => t.textContent);
        expect(titles).toEqual(["Score", "Latency"]);
        expect(trialCard().textContent).toContain("shadow");
    });

    it("leaves out the score chart when no output carried one", async () => {
        const buckets = [bucket(1_760_000_000, null, 3), bucket(1_760_000_600, null, 4)];
        await open(
            "#/models/hvac",
            onTrial(
                () => Response.json(FIGURES),
                noPast,
                () => Response.json({ buckets }),
            ) as never,
        );

        await vi.waitFor(() => expect(trialCard().querySelectorAll("figure")).toHaveLength(1));
        expect(trialCard().querySelector("figcaption")?.textContent).toBe("Latency");
    });

    it("shows the figures alone when the series could not be read", async () => {
        await open(
            "#/models/hvac",
            onTrial(
                () => Response.json(FIGURES),
                noPast,
                () => Response.json({ error: "nope" }, { status: 500 }),
            ) as never,
        );

        await vi.waitFor(() => expect(trialCard().textContent).toContain("91.4%"));
        expect(trialCard().querySelector("figure")).toBeNull();
    });

    it("says when a trial has no pairs yet", async () => {
        await open(
            "#/models/hvac",
            onTrial(() => Response.json({ ...FIGURES, pairs: 0, agreement: null })) as never,
        );

        await vi.waitFor(() => expect(trialCard().textContent).toContain("No pairs yet"));
    });

    it("says why the figures could not be read, and the model stays", async () => {
        await open(
            "#/models/hvac",
            onTrial(() => Response.json({ error: "install wactorz[data]" }, { status: 503 })) as never,
        );

        await vi.waitFor(() => expect(trialCard().textContent).toContain("install wactorz[data]"));
        expect(model().textContent).toContain("hvac");
    });

    it("reads the figures again on Refresh, and on nothing else", async () => {
        let pairs = 10;
        const answering = onTrial(() => Response.json({ ...FIGURES, pairs }));
        await open("#/models/hvac", answering as never);
        await vi.waitFor(() => expect(trialCard().textContent).toContain("on 10 inputs"));
        const reads = () => answering.mock.calls.filter(([url]) => url.endsWith("/trial")).length;
        const before = reads();

        pairs = 20;
        byRole(trialCard(), "button", "Refresh").click();

        await vi.waitFor(() => expect(trialCard().textContent).toContain("on 20 inputs"));
        expect(reads()).toBe(before + 1);
    });

    it("shows no trial card for a model with no shadow set", async () => {
        const reads = server();
        const answering = vi.fn(async (url: string, init?: RequestInit) =>
            url.endsWith("/api/models/hvac")
                ? Response.json({ ...HVAC, pointers: { active: HVAC.pointers.active } })
                : reads(url, init),
        );
        await open("#/models/hvac", answering as never);
        await vi.waitFor(() => model());

        expect(document.querySelector(".af-model-trial")).toBeNull();
        expect(answering.mock.calls.some(([url]) => url.endsWith("/trial"))).toBe(false);
    });

    it("lists how earlier trials ended, and no card when there are none", async () => {
        const past = {
            ...FIGURES,
            id: "t1",
            by: "dashboard",
            shadow_version: 5,
            active_version: 4,
            ended: "promoted",
        };
        await open(
            "#/models/hvac",
            onTrial(
                () => Response.json(FIGURES),
                () => Response.json({ trials: [past] }),
            ) as never,
        );

        await vi.waitFor(() =>
            expect(byRole(document, "region", "Past trials").textContent).toContain("v5 beside v4, promoted"),
        );
        expect(byRole(document, "region", "Past trials").textContent).toContain(
            "91.4% agreed on 41,230 inputs",
        );
    });

    it("reads a dash for a figure the server has no value for, not a zero", async () => {
        const unmeasured = {
            ...FIGURES,
            agreement: null,
            latency: { active: { mean_ms: null, p95_ms: null }, shadow: { mean_ms: 1, p95_ms: 4.6 } },
        };
        await open("#/models/hvac", onTrial(() => Response.json(unmeasured)) as never);

        await vi.waitFor(() => expect(trialCard().textContent).toContain("– → 4.6"));
    });

    it("shows the score when the outputs carried one", async () => {
        const scored = {
            ...FIGURES,
            score: { active_mean: 2.1, shadow_mean: 2, difference_mean: -0.1, scored: 8 },
        };
        await open("#/models/hvac", onTrial(() => Response.json(scored)) as never);

        await vi.waitFor(() => expect(trialCard().textContent).toContain("2.10 → 2.00"));
    });

    it("says why a past trial's pairs could not be read, rather than that it had none", async () => {
        const past = {
            ...FIGURES,
            id: "t3",
            by: "chat",
            shadow_version: 3,
            active_version: 2,
            ended: "cleared",
            pairs: 0,
            pairs_unavailable: "install wactorz[data]",
        };
        await open(
            "#/models/hvac",
            onTrial(
                () => Response.json(FIGURES),
                () => Response.json({ trials: [past] }),
            ) as never,
        );

        await vi.waitFor(() =>
            expect(byRole(document, "region", "Past trials").textContent).toContain(
                "its pairs could not be read: install wactorz[data]",
            ),
        );
    });

    it("says a trial ended with no pairs, and one with no version served", async () => {
        const past = {
            ...FIGURES,
            id: "t2",
            by: "chat",
            shadow_version: 3,
            active_version: null,
            ended: "cleared",
            pairs: 0,
        };
        await open(
            "#/models/hvac",
            onTrial(
                () => Response.json(FIGURES),
                () => Response.json({ trials: [past] }),
            ) as never,
        );

        await vi.waitFor(() => {
            const text = byRole(document, "region", "Past trials").textContent;
            expect(text).toContain("v3, cleared");
            expect(text).toContain("no pairs");
        });
    });

    it("warns of a move that timed out instead of calling it not done", async () => {
        const shown = vi.spyOn(toast, "show");
        const reads = server();
        const silentOnMoves = vi.fn((url: string, init?: RequestInit) =>
            init?.method === "POST"
                ? new Promise<Response>((_resolve, reject) =>
                      init.signal?.addEventListener("abort", () => reject(new Error("aborted"))),
                  )
                : reads(url, init),
        );
        await open("#/models/hvac", silentOnMoves as unknown as typeof reads);
        await vi.waitFor(() => model());

        vi.useFakeTimers();
        try {
            press("Try v6 as shadow");
            byTestId(document, "confirm-ok").click();
            await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 1);

            expect(shown).toHaveBeenCalledWith(
                expect.objectContaining({
                    type: "alert-warning",
                    title: "Try v6 as shadow: unconfirmed",
                }),
            );
        } finally {
            vi.useRealTimers();
        }
    });

    it("reads the model again when its pointer moves elsewhere, while on screen", async () => {
        await open("#/models/hvac");
        await vi.waitFor(() => model());
        const before = fetched.mock.calls.length;

        emit("af-model-pointer", { name: "hvac", pointer: "active" });

        await vi.waitFor(() =>
            expect(fetched.mock.calls.slice(before).map(c => c[0])).toContain("/api/models/hvac"),
        );
    });

    it("says when a pointer's version no longer holds its bytes, and offers no promote of it", async () => {
        const broken = {
            ...HVAC,
            versions: [6, 7, 8].map(n =>
                n === 6 ? { ...VERSION(6), runs_code: true, format: "pickle" } : VERSION(n),
            ),
            pointers: { ...HVAC.pointers, shadow: { ...POINTER("shadow", 8), holds: false } },
        };
        const answering = vi.fn(async (url: string) => {
            if (url.endsWith("/moves")) {
                return Response.json({ moves: [] });
            }
            return url.endsWith("/api/models") ? Response.json({ models: MODELS }) : Response.json(broken);
        });
        await open("#/models/hvac", answering as unknown as ReturnType<typeof server>);
        await vi.waitFor(() => model());

        expect(model().textContent).toContain("shadow v8 no longer holds the bytes it was set to");
        expect((byRole(model(), "button", "Promote v8") as HTMLButtonElement).disabled).toBe(true);
        expect(model().textContent).toContain("v8 no longer holds the bytes it was tried with.");
        expect(model().textContent).toContain("pickle (runs code)");
        expect(model().textContent).toContain("No pointer has moved yet.");
    });

    it("narrows the list to the names the filter finds", async () => {
        await open("#/models");

        const filter = byRole(document, "searchbox", "Filter models") as HTMLInputElement;
        filter.value = "PU";
        filter.dispatchEvent(new Event("input"));

        expect(allByRole(byRole(document, "list", "Stored models"), "link").map(a => a.textContent)).toEqual([
            expect.stringContaining("pump"),
        ]);
        filter.value = "zzz";
        filter.dispatchEvent(new Event("input"));
        expect(byRole(document, "list", "Stored models").textContent).toContain(
            "No model's name has that in it.",
        );
    });

    it("says which model is open when the address names another", async () => {
        await open("#/models");
        expect(byRole(document, "region", "Model").textContent ?? "").toContain("Pick a model");

        emit("af-route", { view: "models", path: ["pump"] });

        expect(changes.at(-1)).toEqual({ view: "models", path: ["pump"] });
        await vi.waitFor(() => expect(document.body.textContent).toContain("Could not read pump: not here"));
        expect(byRole(document, "link", "← All models").getAttribute("href")).toBe("#/models");
    });
});
