/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { buildIdentityView, shortDid } from "../ui/dashboard/identityView";

const DID = "did:swid:zQmcn8EtYXq3CETZxfom5FJzHJYy2BBchWMGbAB5NnvyKpX";
const IDENT = { handle: "swid:agent:home:main-abc123de", did: DID, entityClass: "agent" };
const DOC = { id: DID, alsoKnownAs: [IDENT.handle] };

function mockFetch(responses: Record<string, unknown>): void {
    globalThis.fetch = vi.fn().mockImplementation(async (url: string) => {
        for (const [needle, body] of Object.entries(responses)) {
            if (url.includes(needle)) {
                return { ok: true, json: async () => body } as Response;
            }
        }
        return { ok: false, status: 500, json: async () => ({}) } as Response;
    });
}

function flush(): Promise<void> {
    return new Promise(r => setTimeout(r, 0));
}

afterEach(() => vi.restoreAllMocks());

describe("shortDid", () => {
    it("keeps short ids and truncates long ones with head+tail", () => {
        expect(shortDid("did:swid:zshort")).toBe("did:swid:zshort");
        const s = shortDid(DID);
        expect(s.startsWith("did:swid:z")).toBe(true);
        expect(s).toContain("…");
        expect(s.endsWith(DID.slice(-6))).toBe(true);
    });
});

describe("identityView", () => {
    it("lists identities with class chip, handle, and truncated did", async () => {
        mockFetch({ "/api/swid/identities": { enabled: true, identities: [IDENT] } });
        const el = buildIdentityView();
        await flush();
        expect(el.querySelector(".af-identity-pill")!.textContent).toBe("minting on");
        expect(el.querySelector(".af-identity-chip")!.textContent).toBe("agent");
        expect(el.querySelector(".af-identity-handle")!.textContent).toBe(IDENT.handle);
        expect(el.querySelector<HTMLElement>(".af-identity-did")!.title).toBe(DID);
        expect(el.querySelector(".af-identity-count")!.textContent).toBe("1 identity");
    });

    it("resolve toggles the detail and shows the DID document", async () => {
        mockFetch({
            "/api/swid/identities": { enabled: true, identities: [IDENT] },
            "/1.0/identifiers/": { didDocument: DOC },
        });
        const el = buildIdentityView();
        await flush();
        const buttons = [...el.querySelectorAll<HTMLButtonElement>(".af-identity-btn")];
        const resolve = buttons.find(b => b.textContent === "resolve")!;
        resolve.click();
        await flush();
        const detail = el.querySelector<HTMLElement>(".af-identity-detail")!;
        expect(detail.hidden).toBe(false);
        expect(detail.querySelector(".af-identity-doc")!.textContent).toContain(DID);
        resolve.click(); // second click hides without re-fetching
        expect(detail.hidden).toBe(true);
    });

    it("copy writes the full did to the clipboard", async () => {
        mockFetch({ "/api/swid/identities": { enabled: true, identities: [IDENT] } });
        const writeText = vi.fn();
        vi.spyOn(navigator.clipboard, "writeText").mockImplementation(writeText);
        const el = buildIdentityView();
        await flush();
        const copy = [...el.querySelectorAll<HTMLButtonElement>(".af-identity-btn")].find(
            b => b.textContent === "copy",
        )!;
        copy.click();
        expect(writeText).toHaveBeenCalledWith(DID);
        expect(copy.textContent).toBe("copied");
    });

    it("shows the disabled hint when minting is off and nothing is minted", async () => {
        mockFetch({ "/api/swid/identities": { enabled: false, identities: [] } });
        const el = buildIdentityView();
        await flush();
        expect(el.querySelector(".af-identity-pill")!.textContent).toBe("minting off");
        expect(el.querySelector(".af-identity-empty")!.textContent).toContain("SWID_KEYSTORE_PASSPHRASE");
    });

    it("shows the enabled empty state when nothing is minted yet", async () => {
        mockFetch({ "/api/swid/identities": { enabled: true, identities: [] } });
        const el = buildIdentityView();
        await flush();
        expect(el.querySelector(".af-identity-empty")!.textContent).toContain("No identities");
    });

    it("reports a load failure without throwing", async () => {
        mockFetch({});
        const el = buildIdentityView();
        await flush();
        expect(el.querySelector(".af-identity-content")!.textContent).toContain("Could not load identities");
    });

    it("reports a resolution failure inside the detail", async () => {
        mockFetch({ "/api/swid/identities": { enabled: true, identities: [IDENT] } });
        const el = buildIdentityView();
        await flush();
        globalThis.fetch = vi.fn().mockRejectedValue(new Error("net down"));
        const resolve = [...el.querySelectorAll<HTMLButtonElement>(".af-identity-btn")].find(
            b => b.textContent === "resolve",
        )!;
        resolve.click();
        await flush();
        expect(el.querySelector(".af-identity-detail")!.textContent).toContain("resolution failed");
    });
});
