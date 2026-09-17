"""TLS for connections to the MQTT broker.

Off unless ``MQTT_TLS`` turns it on, so an install that has never set it connects
as it always has. On, the broker is verified against ``MQTT_TLS_CA``:

- empty: the CA this install generated (:mod:`.broker_tls`), under the state
  directory. That CA signs nothing but this install's broker, so the hostname is
  not checked: a node dials the broker by whatever address it was deployed with,
  and a changed LAN address must not strand it.
- ``system``: the system's trust store, for a broker whose certificate comes from
  a public CA.
- a path: that CA file, for a broker whose certificate someone else issued.

Anything but the generated CA checks the hostname, since such a CA can sign more
than this broker. ``MQTT_TLS_CHECK_HOSTNAME`` overrides that either way.

The runner, and the catalogue programs that open a connection of their own, hold
copies of this rule, since neither can import ``wactorz`` on a node.
``tests/test_mqtt_tls.py`` holds the copies to it.

Standard library only: :mod:`.mqtt` imports this, and is itself imported early.
"""

import ssl
from pathlib import Path

from .paths import resolve_state_dir

#: Where the generated CA and broker certificate live, under the state directory.
TLS_DIRNAME = "mqtt_tls"

#: The CA certificate's file name in that directory.
CA_FILE = "ca.crt"

#: The ``MQTT_TLS_CA`` value that means the system's trust store.
SYSTEM_TRUST = "system"

_ON = frozenset({"1", "true", "yes", "on"})
_OFF = frozenset({"0", "false", "no", "off"})


def tls_enabled(value: str) -> bool:
    """Whether an ``MQTT_TLS`` value turns TLS on."""
    return value.strip().lower() in _ON


def generated_ca_path(state_dir: str | None = None) -> Path:
    """Where this install's generated CA certificate is kept."""
    return Path(resolve_state_dir(state_dir)) / TLS_DIRNAME / CA_FILE


def checks_hostname(ca: str, override: str = "") -> bool:
    """Whether a client trusting ``ca`` checks the broker's hostname."""
    value = override.strip().lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    return bool(ca.strip())


def client_context(ca: str, override: str = "", state_dir: str | None = None) -> ssl.SSLContext:
    """A TLS context that verifies the broker as ``ca`` and ``override`` say.

    Raises ``OSError`` when the CA file named, or the generated one, is not there:
    a connection that cannot verify the broker should fail, not fall back to
    trusting whatever answers.
    """
    setting = ca.strip()
    if setting.lower() == SYSTEM_TRUST:
        context = ssl.create_default_context()
    else:
        cafile = Path(setting).expanduser() if setting else generated_ca_path(state_dir)
        context = ssl.create_default_context(cafile=str(cafile))
    context.check_hostname = checks_hostname(setting, override)
    return context
