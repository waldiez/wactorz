"""Static splash / error pages for the desktop shell, kept out of app.py.

Tiny self-contained documents loaded as inline HTML — no network or backend
needed, so the splash paints before the backend is up.
"""
from __future__ import annotations

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
  <p>Details are in the log:<br><code>{LOG_PATH}</code></p>
</div></body></html>"""


def error_html(log_path: str) -> str:
    """The backend-didn't-start page with the log path filled in."""
    return _ERROR_HTML.replace("{LOG_PATH}", log_path)
