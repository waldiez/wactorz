/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The catalogue of stored models: read from the server, the open one read
 * again when its pointers move, a move asked for and its refusal said.
 */
import { describe, it, expect, vi } from "vitest";
import { ModelCatalog } from "../models/ModelCatalog";
import type { Answer } from "../io/api";
import type { ModelDetail, ModelSummary, PastTrial, PointerMove, ShadowTrial } from "../types/models";

const ok = <T>(value: T): Answer<T> => ({ ok: true, value });

function summary(name: string): ModelSummary {
    return { name, newest: { version: 1 } as ModelSummary["newest"], versions: 1, pointers: {} };
}

function detail(name: string): ModelDetail {
    return { name, versions: [], pointers: {} };
}

function trial(name: string, pairs = 3): ShadowTrial {
    return { name, pairs, since: 1, until: 2 } as ShadowTrial;
}

/** A model with a version on trial. */
function onTrial(name: string): ModelDetail {
    const shadow = { version: 2 } as NonNullable<ModelDetail["pointers"]["shadow"]>;
    return { ...detail(name), pointers: { shadow } };
}

/** A server that answers what a test says, and records the moves asked of it. */
function server() {
    return {
        list: vi.fn(async () => ok([summary("hvac"), summary("pump")])),
        detail: vi.fn(async (name: string) => ok(detail(name))),
        moves: vi.fn(async (): Promise<Answer<PointerMove[]>> => ok([])),
        move: vi.fn(async (): Promise<Answer<null>> => ok(null)),
        trial: vi.fn(async (name: string): Promise<Answer<ShadowTrial>> => ok(trial(name))),
        trials: vi.fn(async (): Promise<Answer<PastTrial[]>> => ok([])),
    };
}

describe("the catalogue", () => {
    it("reads the list, then a model and its moves when one is opened", async () => {
        const api = server();
        const catalog = new ModelCatalog(api);

        await catalog.refresh();
        expect(catalog.state.models?.map(m => m.name)).toEqual(["hvac", "pump"]);
        expect(api.detail).not.toHaveBeenCalled();

        await catalog.open("hvac");
        expect(catalog.state).toMatchObject({ open: "hvac", detail: { name: "hvac" }, detailError: "" });
    });

    it("tells its listeners each change, until they stop listening", async () => {
        const catalog = new ModelCatalog(server());
        const heard = vi.fn();
        const stop = catalog.subscribe(heard);

        await catalog.refresh();
        const told = heard.mock.calls.length;
        stop();
        await catalog.refresh();

        expect(told).toBeGreaterThan(0);
        expect(heard.mock.calls.length).toBe(told);
    });

    it("says why the list or a model could not be read", async () => {
        const api = server();
        api.list.mockResolvedValueOnce({ ok: false, error: "HTTP 500" });
        api.detail.mockResolvedValueOnce({ ok: false, error: "no model 'ghost' is stored" });
        const catalog = new ModelCatalog(api);

        await catalog.refresh();
        await catalog.open("ghost");

        expect(catalog.state).toMatchObject({ models: null, listError: "HTTP 500", detail: null });
        expect(catalog.state.detailError).toContain("ghost");
    });

    it("keeps a model without its history when only the history could not be read", async () => {
        const api = server();
        api.moves.mockResolvedValueOnce({ ok: false, error: "no database" });
        const catalog = new ModelCatalog(api);

        await catalog.open("hvac");

        expect(catalog.state).toMatchObject({ detail: { name: "hvac" }, moves: [], detailError: "" });
    });

    it("drops what arrives for a model no longer open", async () => {
        const api = server();
        let release: () => void = () => {};
        api.detail.mockImplementationOnce(
            name => new Promise(resolve => (release = () => resolve(ok(detail(name))))),
        );
        const catalog = new ModelCatalog(api);

        const slow = catalog.open("hvac");
        await catalog.open("pump");
        release();
        await slow;

        expect(catalog.state.detail?.name).toBe("pump");
    });

    it("reads the open model again when its pointer moves, and only the list for another", async () => {
        const api = server();
        const catalog = new ModelCatalog(api);
        await catalog.open("hvac");
        api.detail.mockClear();

        await catalog.moved("pump");
        expect(api.detail).not.toHaveBeenCalled();

        await catalog.moved("hvac");
        expect(api.detail).toHaveBeenCalledWith("hvac");
        expect(api.list).toHaveBeenCalledTimes(2);
    });

    it("asks for a move on the open model with its reason, then reads what changed", async () => {
        const api = server();
        const catalog = new ModelCatalog(api);
        await catalog.open("hvac");
        api.detail.mockClear();

        expect(await catalog.act("hvac", { kind: "promote" }, "F1 up")).toBeNull();

        expect(api.move).toHaveBeenCalledWith("hvac", { kind: "promote" }, "F1 up");
        expect(api.detail).toHaveBeenCalled();
        expect(catalog.state.moving).toBe(false);
    });

    it("reads no trial for a model with no shadow set", async () => {
        const api = server();
        const catalog = new ModelCatalog(api);

        await catalog.open("hvac");

        expect(api.trial).not.toHaveBeenCalled();
        expect(catalog.state).toMatchObject({ trial: null, trialReading: false });
    });

    it("shows the model first, and its trial once that is read", async () => {
        const api = server();
        api.detail.mockResolvedValue(ok(onTrial("hvac")));
        let answer: (got: Answer<ShadowTrial>) => void = () => {};
        api.trial.mockReturnValueOnce(new Promise(resolve => (answer = resolve)));
        const catalog = new ModelCatalog(api);

        const opening = catalog.open("hvac");
        await vi.waitFor(() => expect(catalog.state.detail).not.toBeNull());
        expect(catalog.state).toMatchObject({ trial: null, trialReading: true });

        answer(ok(trial("hvac", 8)));
        await opening;

        expect(catalog.state.trial?.pairs).toBe(8);
        expect(catalog.state.trialReading).toBe(false);
    });

    it("says why a trial could not be read, and the model stays", async () => {
        const api = server();
        api.detail.mockResolvedValue(ok(onTrial("hvac")));
        api.trial.mockResolvedValueOnce({ ok: false, error: "install wactorz[data]", status: 503 });
        const catalog = new ModelCatalog(api);

        await catalog.open("hvac");

        expect(catalog.state).toMatchObject({ trialError: "install wactorz[data]", trial: null });
        expect(catalog.state.detail?.name).toBe("hvac");
    });

    it("takes only the newest read of a trial", async () => {
        const api = server();
        api.detail.mockResolvedValue(ok(onTrial("hvac")));
        const catalog = new ModelCatalog(api);
        await catalog.open("hvac");
        let older: (got: Answer<ShadowTrial>) => void = () => {};
        api.trial.mockReturnValueOnce(new Promise(resolve => (older = resolve)));
        api.trial.mockResolvedValueOnce(ok(trial("hvac", 9)));

        const first = catalog.refreshTrial();
        await catalog.refreshTrial();
        older(ok(trial("hvac", 1)));
        await first;

        expect(catalog.state.trial?.pairs).toBe(9);
    });

    it("drops a slow trial of the model left, while the next model is still being read", async () => {
        const api = server();
        api.detail.mockResolvedValueOnce(ok(onTrial("a")));
        let trialOfA: (got: Answer<ShadowTrial>) => void = () => {};
        api.trial.mockReturnValueOnce(new Promise(resolve => (trialOfA = resolve)));
        let detailOfB: (got: Answer<ModelDetail>) => void = () => {};
        const catalog = new ModelCatalog(api);
        const openingA = catalog.open("a");
        await vi.waitFor(() => expect(catalog.state.trialReading).toBe(true));

        api.detail.mockReturnValueOnce(new Promise(resolve => (detailOfB = resolve)));
        let trialOfB: (got: Answer<ShadowTrial>) => void = () => {};
        api.trial.mockReturnValueOnce(new Promise(resolve => (trialOfB = resolve)));
        const openingB = catalog.open("b");
        trialOfA(ok(trial("a", 1)));
        await openingA;

        expect(catalog.state.trial).toBeNull();
        expect(catalog.state.trialReading).toBe(false);

        detailOfB(ok(onTrial("b")));
        await vi.waitFor(() => expect(catalog.state.detail?.name).toBe("b"));
        await vi.waitFor(() => expect(catalog.state.trialReading).toBe(true));
        expect(catalog.state.trial).toBeNull();

        trialOfB(ok(trial("b", 2)));
        await openingB;
        expect(catalog.state.trial?.name).toBe("b");
    });

    it("keeps the figures through a refresh, but not under a new shadow version", async () => {
        const api = server();
        const standing = (version: number) =>
            ok({
                ...detail("hvac"),
                pointers: { shadow: { version } as NonNullable<ModelDetail["pointers"]["shadow"]> },
            });
        const figures = (version: number) =>
            ok({ ...trial("hvac", version), shadow: { version } } as ShadowTrial);
        api.detail.mockResolvedValue(standing(8));
        api.trial.mockResolvedValueOnce(figures(8));
        const catalog = new ModelCatalog(api);
        await catalog.open("hvac");

        let answer: (got: Answer<ShadowTrial>) => void = () => {};
        api.trial.mockReturnValueOnce(new Promise(resolve => (answer = resolve)));
        const refreshing = catalog.refreshTrial();
        expect(catalog.state.trial?.pairs).toBe(8);
        answer(figures(8));
        await refreshing;

        api.detail.mockResolvedValue(standing(9));
        api.trial.mockReturnValueOnce(new Promise(resolve => (answer = resolve)));
        const moving = catalog.moved("hvac");
        await vi.waitFor(() => expect(catalog.state.detail?.pointers.shadow?.version).toBe(9));
        expect(catalog.state.trial).toBeNull();
        answer(figures(9));
        await moving;
        expect(catalog.state.trial?.pairs).toBe(9);
    });

    it("forgets the trial of the model it leaves, and one whose shadow was cleared", async () => {
        const api = server();
        api.detail.mockResolvedValue(ok(onTrial("hvac")));
        const catalog = new ModelCatalog(api);
        await catalog.open("hvac");
        expect(catalog.state.trial).not.toBeNull();

        api.detail.mockResolvedValue(ok(detail("hvac")));
        await catalog.moved("hvac");
        expect(catalog.state.trial).toBeNull();
    });

    it("keeps showing a model whose past trials could not be read", async () => {
        const api = server();
        api.trials.mockResolvedValueOnce({ ok: false, error: "no database", status: 503 });
        const catalog = new ModelCatalog(api);

        await catalog.open("hvac");

        expect(catalog.state.detail?.name).toBe("hvac");
        expect(catalog.state.pastTrials).toEqual([]);
    });

    it("reads the model again after a timeout, which may have made the move, not after a refusal", async () => {
        const api = server();
        const catalog = new ModelCatalog(api);
        await catalog.open("hvac");
        api.detail.mockClear();

        api.move.mockResolvedValueOnce({ ok: false, error: "nothing to roll back", status: 409 });
        await catalog.act("hvac", { kind: "rollback" });
        expect(api.detail).not.toHaveBeenCalled();

        api.move.mockResolvedValueOnce({ ok: false, error: "no answer", timedOut: true });
        expect(await catalog.act("hvac", { kind: "promote" })).toMatchObject({ timedOut: true });
        expect(api.detail).toHaveBeenCalledWith("hvac");
    });

    it("gives back the server's refusal, and asks nothing for no model or with a move on its way", async () => {
        const api = server();
        api.move.mockResolvedValueOnce({ ok: false, error: "nothing to roll back" });
        const catalog = new ModelCatalog(api);
        expect(await catalog.act("", { kind: "rollback" })).toBeNull();
        expect(api.move).not.toHaveBeenCalled();

        await catalog.open("hvac");
        expect(await catalog.act("hvac", { kind: "rollback" })).toMatchObject({
            error: "nothing to roll back",
        });

        let finish: () => void = () => {};
        api.move.mockImplementationOnce(() => new Promise(resolve => (finish = () => resolve(ok(null)))));
        const first = catalog.act("hvac", { kind: "promote" });
        expect(await catalog.act("hvac", { kind: "promote" })).toBeNull();
        finish();
        await first;
        expect(api.move).toHaveBeenCalledTimes(2);
    });

    it("keeps a newer read of the open model over an older one that answers later", async () => {
        const api = server();
        const withActive = (version: number): ModelDetail => ({
            ...detail("hvac"),
            pointers: { active: { version } as NonNullable<ModelDetail["pointers"]["active"]> },
        });
        let answerFirst: () => void = () => {};
        api.detail
            .mockImplementationOnce(
                () => new Promise(resolve => (answerFirst = () => resolve(ok(withActive(1))))),
            )
            .mockImplementationOnce(async () => ok(withActive(2)));
        const catalog = new ModelCatalog(api);

        const first = catalog.open("hvac");
        await catalog.moved("hvac");
        expect(catalog.state.detail?.pointers.active?.version).toBe(2);
        answerFirst();
        await first;

        expect(catalog.state.detail?.pointers.active?.version).toBe(2);
    });

    it("keeps a newer list over an older one that answers later", async () => {
        const api = server();
        let answerFirst: () => void = () => {};
        api.list
            .mockImplementationOnce(
                () => new Promise(resolve => (answerFirst = () => resolve(ok([summary("old")])))),
            )
            .mockImplementationOnce(async () => ok([summary("new")]));
        const catalog = new ModelCatalog(api);

        const first = catalog.refresh();
        await catalog.refresh();
        answerFirst();
        await first;

        expect(catalog.state.models?.map(m => m.name)).toEqual(["new"]);
    });

    it("moves the model it is told to, whichever is open", async () => {
        const api = server();
        const catalog = new ModelCatalog(api);
        await catalog.open("pump");

        await catalog.act("hvac", { kind: "promote" });

        expect(api.move).toHaveBeenCalledWith("hvac", { kind: "promote" }, "");
    });

    it("opening the model already open reads nothing", async () => {
        const api = server();
        const catalog = new ModelCatalog(api);
        await catalog.open("hvac");
        await catalog.open("hvac");
        await catalog.open("");

        expect(api.detail).toHaveBeenCalledTimes(1);
        expect(catalog.state.open).toBe("");
    });
});
