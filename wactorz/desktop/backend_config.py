"""Backend configuration the user edits from the desktop Configure view.

All values are stored in DATA_DIR/user.env (KEY=VALUE, chmod 0600 — owner only)
and merged into the backend child's environment at spawn, taking precedence over
the inherited shell/.env so the desktop config is authoritative (this also
sidesteps a stale shell var shadowing the real value). Keys mirror .env.template.
"""

from __future__ import annotations

import os
import stat

from wactorz.desktop.config import DATA_DIR

_USER_ENV = DATA_DIR / "user.env"

# Managed keys, mirroring .env.template — everything the Configure view sets.
KEYS = (
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_API_KEY",
    "OPENAI_URL",
    "OLLAMA_URL",
    "MQTT_HOST",
    "MQTT_PORT",
    "MQTT_USERNAME",
    "MQTT_PASSWORD",
    "HA_URL",
    "HA_TOKEN",
)


def _read() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in _USER_ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return out


def _write(values: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in sorted(values.items()) if v]
    try:
        _USER_ENV.parent.mkdir(parents=True, exist_ok=True)
        _USER_ENV.write_text("\n".join(lines) + ("\n" if lines else ""))
        # Owner-only: it holds secrets (API key, HA token, MQTT password).
        os.chmod(_USER_ENV, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def load() -> dict[str, str]:
    """Current values of the managed keys (only those with a value)."""
    env = _read()
    return {k: env[k] for k in KEYS if env.get(k)}


def save(values: dict[str, str]) -> None:
    """Persist the managed keys to user.env. A blank value clears that key;
    unknown keys already in the file are left untouched.
    """
    env = _read()
    for key in KEYS:
        value = (values.get(key) or "").strip()
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    _write(env)


def env_for_backend() -> dict[str, str]:
    """Non-empty config to inject into the backend child's environment (the whole
    user.env, including any hand-added keys).
    """
    return {k: v for k, v in _read().items() if v}
