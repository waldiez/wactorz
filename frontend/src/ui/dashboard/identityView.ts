/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Identity view — the Spatial Web passport register. Lists every minted
 * `did:swid` identity (agents, HA devices, areas/spaces) with its readable
 * handle, and resolves a DID on demand to show the live DID document.
 *
 * Data comes from `/api/swid/identities` (the minting index) and
 * `/1.0/identifiers/{did}` (DIF DID Resolution). All values are rendered via
 * `textContent` — nothing remote is parsed as HTML.
 */

interface Identity {
    handle: string;
    did: string;
    entityClass: string;
}

interface IdentitiesResponse {
    enabled: boolean;
    identities: Identity[];
}

function ingressPath(): string {
    return (window as unknown as { __WACTORZ_INGRESS_PATH?: string }).__WACTORZ_INGRESS_PATH ?? "";
}

/** Shorten `did:swid:zQm…` for display, keeping scheme + head/tail of the SCID. */
export function shortDid(did: string): string {
    return did.length <= 32 ? did : `${did.slice(0, 20)}…${did.slice(-6)}`;
}

function statusLine(data: IdentitiesResponse): HTMLElement {
    const status = document.createElement("div");
    status.className = "af-identity-status";
    const pill = document.createElement("span");
    pill.className = `af-identity-pill ${data.enabled ? "on" : "off"}`;
    pill.textContent = data.enabled ? "minting on" : "minting off";
    const count = document.createElement("span");
    count.className = "af-identity-count";
    const n = data.identities.length;
    count.textContent = `${n} identit${n === 1 ? "y" : "ies"}`;
    status.append(pill, count);
    return status;
}

function emptyState(enabled: boolean): HTMLElement {
    const empty = document.createElement("div");
    empty.className = "af-identity-empty";
    empty.textContent = enabled
        ? "No identities minted yet — they appear as agents start and the HA bridge seeds devices and spaces."
        : "Minting is disabled. Set SWID_KEYSTORE_PASSPHRASE in the server environment to mint DIDs; entities keep readable handles meanwhile.";
    return empty;
}

async function resolveInto(did: string, target: HTMLElement): Promise<void> {
    target.textContent = "resolving…";
    try {
        const resp = await fetch(`${ingressPath()}/1.0/identifiers/${encodeURIComponent(did)}`);
        const body = (await resp.json()) as { didDocument?: unknown };
        const pre = document.createElement("pre");
        pre.className = "af-identity-doc";
        pre.textContent = JSON.stringify(body.didDocument ?? body, null, 2);
        target.replaceChildren(pre);
    } catch (err) {
        target.textContent = `resolution failed: ${String(err)}`;
    }
}

function copyButton(fullDid: string): HTMLButtonElement {
    const copy = document.createElement("button");
    copy.className = "af-identity-btn";
    copy.textContent = "copy";
    copy.title = "Copy the full DID";
    copy.addEventListener("click", () => {
        void navigator.clipboard?.writeText(fullDid);
        copy.textContent = "copied";
        setTimeout(() => (copy.textContent = "copy"), 1200);
    });
    return copy;
}

function resolveButton(fullDid: string, detail: HTMLElement): HTMLButtonElement {
    const resolve = document.createElement("button");
    resolve.className = "af-identity-btn";
    resolve.textContent = "resolve";
    resolve.title = "Fetch and show the DID document";
    resolve.addEventListener("click", () => {
        detail.hidden = !detail.hidden;
        if (!detail.hidden && !detail.hasChildNodes()) {
            void resolveInto(fullDid, detail);
        }
    });
    return resolve;
}

function identityRow(ident: Identity): HTMLElement {
    const row = document.createElement("div");
    row.className = "af-identity-row";

    const chip = document.createElement("span");
    chip.className = `af-identity-chip af-identity-chip-${ident.entityClass || "unknown"}`;
    chip.textContent = ident.entityClass || "?";

    const handle = document.createElement("code");
    handle.className = "af-identity-handle";
    handle.textContent = ident.handle;

    const did = document.createElement("code");
    did.className = "af-identity-did";
    did.textContent = shortDid(ident.did);
    did.title = ident.did;

    const detail = document.createElement("div");
    detail.className = "af-identity-detail";
    detail.hidden = true;

    const main = document.createElement("div");
    main.className = "af-identity-row-main";
    main.append(chip, handle, did, copyButton(ident.did), resolveButton(ident.did, detail));
    row.append(main, detail);
    return row;
}

function renderList(root: HTMLElement, data: IdentitiesResponse): void {
    root.replaceChildren(statusLine(data));
    if (!data.identities.length) {
        root.appendChild(emptyState(data.enabled));
        return;
    }
    const list = document.createElement("div");
    list.className = "af-identity-list";
    data.identities.forEach(ident => list.appendChild(identityRow(ident)));
    root.appendChild(list);
}

/** Build the Identity view; fetches the register once on mount. */
export function buildIdentityView(): HTMLElement {
    const wrap = document.createElement("div");
    wrap.className = "af-identity";

    const title = document.createElement("h3");
    title.className = "af-identity-title";
    title.textContent = "Spatial Web identities";
    wrap.appendChild(title);

    const content = document.createElement("div");
    content.className = "af-identity-content";
    content.textContent = "loading…";
    wrap.appendChild(content);

    void (async () => {
        try {
            const resp = await fetch(`${ingressPath()}/api/swid/identities`);
            if (!resp.ok) {
                throw new Error(String(resp.status));
            }
            renderList(content, (await resp.json()) as IdentitiesResponse);
        } catch (err) {
            content.textContent = `Could not load identities: ${String(err)}`;
        }
    })();

    return wrap;
}
