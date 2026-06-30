/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Home Assistant view: either the device list panel (when credentials are
 * configured) or the config form to enter the HA host + long-lived token.
 *
 * Credentials are stored in localStorage because the browser talks to the
 * user's HA instance directly. After save/clear the dashboard reconnects and
 * re-renders via the single `onApply` callback.
 */

import { escapeHtml } from "../escapeHtml";

/**
 * Allow only http(s) links. Escaping alone does not neutralise a `javascript:`
 * or `data:` scheme (it contains no HTML metacharacters), so a hostile url could
 * otherwise execute on click — collapse anything non-http(s) to "#".
 */
function safeHref(url: string): string {
    return /^https?:\/\//i.test(url.trim()) ? url : "#";
}

export interface HaViewCallbacks {
    /** Re-init the HA client and re-render the HA view (after save/clear). */
    onApply: () => void;
}

/** Read the HA host field and normalise it to an http(s):// URL (or ""). */
function readHaUrl(form: HTMLElement): string {
    let raw = (form.querySelector<HTMLInputElement>("#ha-cfg-url")?.value ?? "").trim();
    // Detect TLS from an explicit protocol prefix (ws/wss/http/https).
    let detectedTls: boolean | null = null;
    if (/^(https|wss):\/\//i.test(raw)) {
        detectedTls = true;
    } else if (/^(http|ws):\/\//i.test(raw)) {
        detectedTls = false;
    }
    // Strip any protocol prefix — we re-add http[s] for storage.
    raw = raw.replace(/^(https?|wss?):\/\//i, "").replace(/\/$/, "");
    const tlsCheckbox = form.querySelector<HTMLInputElement>("#ha-cfg-tls")?.checked ?? false;
    const tls = detectedTls ?? tlsCheckbox;
    return raw ? `${tls ? "https" : "http"}://${raw}` : "";
}

/** Persist HA credentials from the config form, then reconnect + re-render. */
function saveHaConfig(form: HTMLElement, cb: HaViewCallbacks): void {
    const url = readHaUrl(form);
    const token = (form.querySelector<HTMLInputElement>("#ha-cfg-token")?.value ?? "").trim();
    const msg = form.querySelector<HTMLElement>("#ha-cfg-msg")!;
    if (!url || !token) {
        msg.className = "af-ha-msg is-error";
        msg.textContent = "Both fields required.";
        return;
    }
    localStorage.setItem("wactorz-ha-url", url);
    localStorage.setItem("wactorz-ha-token", token);
    msg.className = "af-ha-msg is-ok";
    msg.textContent = "Saved — reloading…";
    setTimeout(() => cb.onApply(), 600);
}

function buildHAConfigForm(haUrl: string | null, haToken: string | null, cb: HaViewCallbacks): HTMLElement {
    // Strip protocol from stored URL so we show just the host in the input.
    const storedUrl = haUrl ?? "";
    const storedHost = storedUrl.replace(/^https?:\/\//, "");
    const storedTls = storedUrl.startsWith("https://");

    const form = document.createElement("div");
    form.className = "af-panel af-ha-config";
    form.innerHTML = `
      <div class="af-panel-head"><h3>Home Assistant</h3></div>
      <p class="af-ha-hint">Enter your Home Assistant host and a long-lived access token.<br>These are stored locally in your browser only.</p>
      <label class="af-ha-field">
        Host / IP
        <input id="ha-cfg-url" name="ha-url" type="text" placeholder="192.168.1.2:8123 or ha.example.com/ha"
          value="${escapeHtml(storedHost)}" class="af-ha-input">
      </label>
      <label class="af-ha-check">
        <input id="ha-cfg-tls" name="ha-tls" type="checkbox" ${storedTls ? "checked" : ""} class="af-ha-check-box">
        Use HTTPS (TLS)
      </label>
      <label class="af-ha-field">
        Long-lived access token
        <input id="ha-cfg-token" name="ha-token" type="password" placeholder="eyJ..."
          value="${escapeHtml(haToken ?? "")}" class="af-ha-input">
      </label>
      <div class="af-ha-actions">
        <button id="ha-cfg-save" class="af-mini-btn af-ha-save">Save</button>
        ${storedHost ? `<button id="ha-cfg-clear" class="af-mini-btn danger af-ha-reset" title="Remove saved credentials">Reset</button>` : ""}
      </div>
      <div id="ha-cfg-msg" class="af-ha-msg"></div>
    `;

    form.querySelector("#ha-cfg-save")?.addEventListener("click", () => saveHaConfig(form, cb));
    form.querySelector("#ha-cfg-clear")?.addEventListener("click", () => {
        localStorage.removeItem("wactorz-ha-url");
        localStorage.removeItem("wactorz-ha-token");
        cb.onApply();
    });
    return form;
}

/** Build the HA view: device-list panel when configured, else the config form. */
export function buildHAView(haUrl: string | null, haToken: string | null, cb: HaViewCallbacks): HTMLElement {
    const el = document.createElement("div");
    el.className = "af-overview";

    if (!haUrl || !haToken) {
        el.appendChild(buildHAConfigForm(haUrl, haToken, cb));
        return el;
    }

    el.innerHTML = `
      <div class="af-panel af-ha-panel">
        <div class="af-panel-head af-ha-panel-head">
          <h3>Home Assistant Devices</h3>
          <div class="af-ha-head-actions">
            <a id="ha-open-link" href="${escapeHtml(safeHref(haUrl))}" target="_blank" rel="noopener"
               class="af-ha-open-link">
              ${escapeHtml(haUrl)} ↗
            </a>
            <button id="ha-reconfigure-btn" class="af-mini-btn af-ha-reconfigure">⚙ Configure</button>
          </div>
        </div>
        <div id="ha-devices-container" class="af-ha-devices">
          <div class="af-ha-empty">
            Connecting to Home Assistant...
          </div>
        </div>
      </div>
    `;

    el.querySelector("#ha-reconfigure-btn")?.addEventListener("click", () => {
        const panel = el.querySelector<HTMLElement>(".af-panel");
        if (panel) {
            panel.innerHTML = "";
            panel.appendChild(buildHAConfigForm(haUrl, haToken, cb));
        }
    });

    return el;
}
