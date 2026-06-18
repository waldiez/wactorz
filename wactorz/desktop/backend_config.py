"""Backend configuration the user edits from the desktop Configure view.

Non-secret values live in DATA_DIR/user.env (KEY=VALUE); secrets go to the OS
keychain via keyring. Both are merged into the backend child's environment at
spawn, taking precedence over the inherited shell/.env so the desktop config is
authoritative (this also sidesteps a stale shell var shadowing the real value).
"""
from __future__ import annotations

from wactorz.desktop.config import DATA_DIR

_USER_ENV = DATA_DIR / "user.env"
_KEYRING_SERVICE = "wactorz-desktop"

# Managed keys -> stored as a secret (keychain) rather than in user.env.
_SECRET = {
    "MQTT_HOST": False,
    "MQTT_PORT": False,
    "MQTT_USERNAME": False,
    "MQTT_PASSWORD": True,
    "HA_URL": False,
    "HA_TOKEN": True,
    "ANTHROPIC_API_KEY": True,
    "OPENAI_API_KEY": True,
    "GOOGLE_API_KEY": True,
}


def _read_user_env() -> dict[str, str]:
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


def _write_user_env(values: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in sorted(values.items()) if v]
    try:
        _USER_ENV.parent.mkdir(parents=True, exist_ok=True)
        _USER_ENV.write_text("\n".join(lines) + ("\n" if lines else ""))
    except Exception:
        pass


def _get_secret(key: str) -> str:
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, key) or ""
    except Exception:
        return ""


def _set_secret(key: str, value: str) -> None:
    try:
        import keyring

        if value:
            keyring.set_password(_KEYRING_SERVICE, key, value)
        else:
            try:
                keyring.delete_password(_KEYRING_SERVICE, key)
            except Exception:
                pass
    except Exception:
        pass


def load() -> dict[str, str]:
    """Current values of the managed keys (non-secrets from user.env, secrets
    from the keychain). Only keys that have a value are included."""
    env = _read_user_env()
    values = {key: (_get_secret(key) if secret else env.get(key, ""))
              for key, secret in _SECRET.items()}
    return {k: v for k, v in values.items() if v}


def save(values: dict[str, str]) -> None:
    """Persist managed keys: non-secrets to user.env, secrets to the keychain.
    A blank value clears that key. Unknown keys in user.env are left untouched."""
    user_env = _read_user_env()
    for key, secret in _SECRET.items():
        value = (values.get(key) or "").strip()
        if secret:
            _set_secret(key, value)
        elif value:
            user_env[key] = value
        else:
            user_env.pop(key, None)
    _write_user_env(user_env)


def env_for_backend() -> dict[str, str]:
    """Non-empty config to inject into the backend child's environment: the whole
    of user.env (including any hand-added keys) plus the managed secrets."""
    env = dict(_read_user_env())
    for key, secret in _SECRET.items():
        if secret:
            value = _get_secret(key)
            if value:
                env[key] = value
    return {k: v for k, v in env.items() if v}
