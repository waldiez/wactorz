"""Static splash / error pages for the desktop shell, kept out of app.py.

Tiny self-contained documents loaded as inline HTML — no network or backend
needed, so the splash paints before the backend is up.
"""
from __future__ import annotations

import html as _html

# Shown while the backend child starts (a few seconds, longer if it is waiting
# on an MQTT broker). Replaced by the app URL once the backend answers.
LOADING_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%}
  body{background:#0A0E1A;color:#e2e8f0;
       font-family:-apple-system,Segoe UI,Roboto,sans-serif;
       display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1.25rem}
  .ring{width:44px;height:44px;border:3px solid #1e2640;border-top-color:#6366f1;
        border-radius:50%;animation:spin .9s linear infinite}
  .name{font-size:1.15rem;font-weight:600;letter-spacing:.04em;color:#c7d2fe}
  .sub{font-size:.8rem;color:#64748b}
  @keyframes spin{to{transform:rotate(360deg)}}
</style></head><body>
  <div class="ring"></div>
  <div class="name">Wactorz</div>
  <div class="sub">Starting...</div>
</body></html>"""

# Shown if the backend never answers; {LOG_PATH} is filled in by error_html().
_ERROR_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%}
  body{background:#0A0E1A;color:#e2e8f0;
       font-family:-apple-system,Segoe UI,Roboto,sans-serif;
       display:flex;align-items:center;justify-content:center}
  .card{max-width:480px;padding:2rem;text-align:center}
  h1{font-size:1.1rem;color:#f87171;margin:0 0 .75rem}
  p{font-size:.85rem;line-height:1.55;color:#94a3b8;margin:.4rem 0}
  code{background:#11182e;padding:.1rem .35rem;border-radius:4px;color:#cbd5e1;
        font-size:.78rem;word-break:break-all}
</style></head><body><div class="card">
  <h1>Wactorz backend didn't start</h1>
  <p>The most common cause is that the configured <b>MQTT broker is unreachable</b>.
     Make sure your MQTT host is running and reachable from this machine, then reopen.</p>
  <p><button onclick="window.pywebview.api.open_config()"
        style="background:#6366f1;color:#fff;border:0;border-radius:6px;
               padding:.55rem 1.1rem;font-size:.85rem;cursor:pointer">Configure…</button></p>
  <p>Details are in the log:<br><code>{LOG_PATH}</code></p>
</div></body></html>"""


def error_html(log_path: str) -> str:
    """The backend-didn't-start page with the log path filled in."""
    return _ERROR_HTML.replace("{LOG_PATH}", log_path)


# Provider -> env var for the LLM API key (one active at a time).
_LLM_PROVIDERS = [
    ("ANTHROPIC_API_KEY", "Anthropic"),
    ("OPENAI_API_KEY", "OpenAI"),
    ("GOOGLE_API_KEY", "Google"),
]


def config_html(values: dict, message: str = "", can_cancel: bool = False) -> str:
    """The Configure form, pre-filled from `values` (env var -> current value).
    Optional `message` shows a banner (e.g. why config opened). `can_cancel` adds
    a Cancel button (only when there's a running app to return to). Submits back
    through window.pywebview.api.save_config()."""
    def val(key: str) -> str:
        return _html.escape(values.get(key, ""), quote=True)

    active = next((k for k, _ in _LLM_PROVIDERS if values.get(k)), _LLM_PROVIDERS[0][0])
    options = "".join(
        f'<option value="{k}"{" selected" if k == active else ""}>{label}</option>'
        for k, label in _LLM_PROVIDERS
    )
    banner = f'<div class="banner">{_html.escape(message)}</div>' if message else ""
    cancel = ('<button onclick="window.pywebview.api.close_config()" '
              'style="background:#334155;margin-left:.5rem">Cancel</button>' if can_cancel else "")
    # No running app to return to → offer Retry (re-connect with current config)
    # instead of Cancel, e.g. after starting the broker.
    retry = ('<button onclick="window.pywebview.api.retry()" '
             'style="background:#0ea5e9;margin-left:.5rem">Retry</button>' if not can_cancel else "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{{margin:0;height:100%;background:#0A0E1A;color:#e2e8f0;
       font-family:-apple-system,Segoe UI,Roboto,sans-serif}}
  .wrap{{max-width:460px;margin:0 auto;padding:1.75rem 1.5rem}}
  h1{{font-size:1.1rem;margin:0 0 .25rem;color:#c7d2fe}}
  .sub{{font-size:.8rem;color:#64748b;margin:0 0 1.25rem}}
  fieldset{{border:1px solid #1e2640;border-radius:8px;margin:0 0 1rem;padding:.75rem 1rem 1rem}}
  legend{{font-size:.75rem;color:#94a3b8;padding:0 .4rem;text-transform:uppercase;letter-spacing:.05em}}
  label{{display:block;font-size:.75rem;color:#94a3b8;margin:.6rem 0 .2rem}}
  input,select{{width:100%;box-sizing:border-box;background:#11182e;border:1px solid #1e2640;
       border-radius:6px;color:#e2e8f0;padding:.5rem .6rem;font-size:.85rem}}
  .row{{display:flex;gap:.75rem}} .row>div{{flex:1}}
  button{{background:#6366f1;color:#fff;border:0;border-radius:6px;padding:.6rem 1.2rem;
       font-size:.9rem;cursor:pointer;margin-top:.5rem}}
  #status{{font-size:.8rem;color:#22d3a0;margin-left:.75rem}}
  .banner{{background:#3b1d1d;color:#fca5a5;border:1px solid #7f1d1d;border-radius:6px;
       padding:.55rem .75rem;font-size:.8rem;margin:0 0 1rem}}
</style></head><body><div class="wrap">
  <h1>Configure Wactorz</h1>
  <p class="sub">Saved securely on this machine. Saving restarts the backend.</p>
  {banner}
  <fieldset><legend>MQTT broker</legend>
    <label>Host</label><input id="MQTT_HOST" value="{val('MQTT_HOST')}" placeholder="localhost">
    <div class="row">
      <div><label>Port</label><input id="MQTT_PORT" value="{val('MQTT_PORT')}" placeholder="1883"></div>
      <div><label>Username</label><input id="MQTT_USERNAME" value="{val('MQTT_USERNAME')}"></div>
    </div>
    <label>Password</label><input id="MQTT_PASSWORD" type="password" value="{val('MQTT_PASSWORD')}">
  </fieldset>
  <fieldset><legend>Home Assistant</legend>
    <label>URL</label><input id="HA_URL" value="{val('HA_URL')}" placeholder="http://homeassistant.local:8123">
    <label>Token</label><input id="HA_TOKEN" type="password" value="{val('HA_TOKEN')}">
  </fieldset>
  <fieldset><legend>LLM</legend>
    <label>Provider</label><select id="provider">{options}</select>
    <label>API key</label><input id="apikey" type="password" value="{val(active)}">
  </fieldset>
  <button onclick="save()">Save &amp; Restart</button>{retry}{cancel}<span id="status"></span>
<script>
  async function save() {{
    const v = {{}};
    for (const id of ["MQTT_HOST","MQTT_PORT","MQTT_USERNAME","MQTT_PASSWORD","HA_URL","HA_TOKEN"])
      v[id] = document.getElementById(id).value.trim();
    v[document.getElementById("provider").value] = document.getElementById("apikey").value.trim();
    document.getElementById("status").textContent = "Saving & restarting…";
    await window.pywebview.api.save_config(v);
  }}
</script>
</div></body></html>"""
