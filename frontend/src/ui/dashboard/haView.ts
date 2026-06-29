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
        msg.style.color = "#f87171";
        msg.textContent = "Both fields required.";
        return;
    }
    localStorage.setItem("wactorz-ha-url", url);
    localStorage.setItem("wactorz-ha-token", token);
    msg.style.color = "#34d399";
    msg.textContent = "Saved — reloading…";
    setTimeout(() => cb.onApply(), 600);
}

function buildHAConfigForm(haUrl: string | null, haToken: string | null, cb: HaViewCallbacks): HTMLElement {
    // Strip protocol from stored URL so we show just the host in the input.
    const storedUrl = haUrl ?? "";
    const storedHost = storedUrl.replace(/^https?:\/\//, "");
    const storedTls = storedUrl.startsWith("https://");

    const form = document.createElement("div");
    form.className = "af-panel";
    form.style.cssText = "max-width:420px;margin:40px auto;display:flex;flex-direction:column;gap:16px;";
    form.innerHTML = `
      <div class="af-panel-head"><h3>Home Assistant</h3></div>
      <p style="font-size:12px;opacity:0.6;margin:0;">Enter your Home Assistant host and a long-lived access token.<br>These are stored locally in your browser only.</p>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;">
        Host / IP
        <input id="ha-cfg-url" type="text" placeholder="192.168.1.2:8123 or ha.example.com/ha"
          value="${escapeHtml(storedHost)}"
          style="background:#1a2230;border:1px solid #2a3a50;border-radius:4px;padding:8px 10px;color:#e2e8f0;font-size:13px;outline:none;">
      </label>
      <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;">
        <input id="ha-cfg-tls" type="checkbox" ${storedTls ? "checked" : ""}
          style="width:14px;height:14px;accent-color:#38bdf8;">
        Use HTTPS (TLS)
      </label>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;">
        Long-lived access token
        <input id="ha-cfg-token" type="password" placeholder="eyJ..."
          value="${escapeHtml(haToken ?? "")}"
          style="background:#1a2230;border:1px solid #2a3a50;border-radius:4px;padding:8px 10px;color:#e2e8f0;font-size:13px;outline:none;">
      </label>
      <div style="display:flex;gap:8px;">
        <button id="ha-cfg-save" class="af-mini-btn" style="flex:1;padding:8px;">Save</button>
        ${storedHost ? `<button id="ha-cfg-clear" class="af-mini-btn danger" style="padding:8px 12px;" title="Remove saved credentials">Reset</button>` : ""}
      </div>
      <div id="ha-cfg-msg" style="font-size:12px;min-height:16px;"></div>
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
      <div class="af-panel" style="height:100%;display:flex;flex-direction:column;overflow:hidden;">
        <div class="af-panel-head" style="display:flex;justify-content:space-between;align-items:center;flex-shrink:0;">
          <h3>Home Assistant Devices</h3>
          <div style="display:flex;align-items:center;gap:8px;">
            <a id="ha-open-link" href="${escapeHtml(safeHref(haUrl))}" target="_blank" rel="noopener"
               style="font-size:11px;opacity:0.6;color:inherit;text-decoration:none;display:flex;align-items:center;gap:4px;">
              ${escapeHtml(haUrl)} ↗
            </a>
            <button id="ha-reconfigure-btn" class="af-mini-btn" style="font-size:10px;">⚙ Configure</button>
          </div>
        </div>
        <div id="ha-devices-container" style="flex:1;overflow-y:auto;overflow-x:hidden;">
          <div style="color:rgba(255,255,255,0.4);text-align:center;grid-column:1/-1;margin-top:40px;">
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
