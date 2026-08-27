"""Runtime settings, read from the environment once at import.

Two shapes, for one reason: an ``AppConfig`` instance carries the settings a
caller wants as a group, and bare module constants carry the ones the web layer
and extensions reach for individually. Both are values rather than lookups, so a
setting takes effect when the process starts and not before.

The ``.env`` file is loaded above all of them, and that position is load-bearing.
Anything read further up the module sees only real environment variables, which
leaves a file that is plainly obeyed elsewhere doing nothing for that one
setting -- indistinguishable, from the outside, from a misspelled name. New
settings go below the load. Real environment variables win over the file.
"""

import os
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# Loaded before anything below reads the environment. Settings further down are
# read at import, so a file loaded after them would reach only the ones built
# later -- the dataclass would see it and the bare constants would not, which is
# the sort of split nobody can guess at from a .env that looks obeyed.
# Real environment variables still win: load_dotenv does not override them.
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    load_dotenv(_env_file)
else:
    load_dotenv(find_dotenv())


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on", "dev"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        # Raised at *import*, so a stray character in one variable took the
        # process down with a traceback that named neither the variable nor the
        # value. Falling back keeps a typo from being fatal, and says which
        # setting was ignored.
        #
        # `warnings`, not `logging`: this module is imported before logging is
        # configured, so a log record here would go to the root handler-of-last-
        # resort and often nowhere at all.
        warnings.warn(
            f"{name}={value!r} is not a whole number — using {default} instead",
            RuntimeWarning,
            stacklevel=2,
        )
        return default


def _env_id_set(*names: str) -> frozenset[int]:
    """Parse a comma/space separated list of numeric ids from the first set var.

    Used for the social-channel sender allow-lists, where an empty result means
    "nobody is allowed" rather than "everybody" — the caller decides, but the
    parse never silently turns junk into an open door.
    """
    for name in names:
        raw = (os.getenv(name) or "").strip()
        if not raw:
            continue
        ids = set()
        for part in raw.replace(";", ",").replace(" ", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                continue
            # 0 is the add-on's "not set" default, and no real account id is
            # <= 0 — treat it as absent so the channel stays in setup mode.
            if value > 0:
                ids.add(value)
        if ids:
            return frozenset(ids)
    return frozenset()


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    if not value:
        return default
    return float(value)


_QUOTES = ('"', "'")


def _unquote(value: str) -> str:
    """Strip surrounding quotes, whether or not they are matched.

    Quoting the whole value is a reasonable thing to write, and several ways of
    setting one keep the quotes as part of the value — Windows `set VAR="…"`,
    Docker's `env_file`, an unbalanced quote in a hand-edited `.env`. For a
    model or provider name that quote travels all the way to the API, which
    answers `404 … model: claude-sonnet-4-6"` for a name that all but exists.
    A quote is never legitimate in those values, so removing it costs nothing.
    """
    value = value.strip()
    # `startswith`, not `value[:1] in _QUOTES`: an empty slice is "in" every
    # string, so the slice form spins forever once the value is exhausted.
    while value.startswith(_QUOTES):
        value = value[1:].strip()
    while value.endswith(_QUOTES):
        value = value[:-1].strip()
    return value


def _env_name(name: str, default: str) -> str:
    """Identifier-shaped env var (provider, model) — unquoted and trimmed."""
    return _unquote(os.getenv(name, "") or "") or default


def _env_opt_float(name: str) -> float | None:
    """Float env var where unset/empty means None (no override)."""
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return float(value)


DEV_MODE = _env_truthy("WACTORZ_DEV_MODE")

#: Largest request body any HTTP endpoint accepts. Nothing here uploads — the
#: biggest legitimate body is a chat message or a spawn config, both far under
#: this. aiohttp defaults to 1 MiB, which is generous for an API with no upload
#: route, and it applies per request on a surface that has no authentication.
MAX_REQUEST_BYTES = 256 * 1024


def _bind_host() -> str:
    """The interface every HTTP/WebSocket server listens on.

    Defaults to loopback. Most installs are single-host and should never have
    been reachable from the network — the previous default served the dashboard
    and the whole API to the LAN out of the box.

    A container publishes its own ports and a process cannot see its own port
    mappings, so a process bound to loopback inside one is unreachable through
    them. Both shipped deployments therefore set this explicitly to `0.0.0.0`
    and declare `WACTORZ_EXPOSED_OK=1` alongside it; a hand-rolled container
    must do the same.
    """
    return os.getenv("WACTORZ_BIND_HOST", "").strip() or "127.0.0.1"


#: Whether chat file attachments may be uploaded. On by default now that the
#: feature is complete, and still a flag: the endpoint writes caller-supplied
#: bytes to disk with nothing pruning them, and a deployment that does not want
#: attachment storage growing there turns it off.  Like every other route it is
#: unauthenticated, so an install exposed beyond its own network has an open
#: 25 MB write endpoint until authentication lands.
UPLOADS_ENABLED = os.getenv("WACTORZ_UPLOADS", "1").strip().lower() not in ("", "0", "false", "no")

#: Largest single upload. Matches the limit the browser enforces before sending,
#: so a file the UI accepts is not refused by the server.
UPLOAD_MAX_BYTES = _env_int("WACTORZ_UPLOAD_MAX_BYTES", 25 * 1024 * 1024)

#: The speech-to-text branches, in the order they concede privacy.
#:
#: ``off``      no transcription; the browser hides the microphone.
#: ``browser``  the client transcribes locally and sends only text.
#: ``server``   the browser captures, the host transcribes.
#: ``host``     the host captures from its own microphone and transcribes.
#:
#: ``server`` and ``host`` differ in who owns the microphone, which is what
#: decides whether a secure context is needed: only browser capture calls
#: ``getUserMedia``, so ``host`` is the branch that works over plain HTTP -- and
#: the only one where the server hears the room.
STT_MODES = ("off", "browser", "server", "host")


def _stt_mode() -> str:
    """The configured branch, or ``off`` when unset or unrecognised."""
    value = _unquote(os.getenv("WACTORZ_STT", "") or "").lower()
    if not value:
        return "off"
    if value not in STT_MODES:
        # Named rather than ignored: a typo here shows up as a microphone that
        # never appears, which looks like a broken feature rather than a setting.
        warnings.warn(
            f"WACTORZ_STT={value!r} is not one of {', '.join(STT_MODES)} — using 'off'",
            RuntimeWarning,
            stacklevel=2,
        )
        return "off"
    return value


#: Which branch this deployment offers. The browser learns it from here rather
#: than from how the bundle was built, so one wheel serves every deployment and
#: the microphone is offered exactly where it can work.
STT_MODE = _stt_mode()

#: Whether this deployment sits behind Home Assistant's ingress. Off unless the
#: add-on says so: the bypass below skips the origin and host checks, and a
#: deployment with no Supervisor must never offer it. Inferring it from the peer's
#: address is not enough — Docker's default pool covers the Supervisor's range, so
#: an ordinary network can land on it by coincidence.
INGRESS_ENABLED = os.getenv("WACTORZ_INGRESS", "0").strip().lower() not in ("", "0", "false", "no")

#: Addresses the Home Assistant ingress bypass is accepted from, comma-separated
#: CIDRs. Defaults to the Supervisor proxy's own range. The bypass exists because
#: Supervisor authenticates the user before proxying; a header alone cannot show
#: a request came from it, since any peer on the container network can set one.
INGRESS_PEERS = os.getenv("WACTORZ_INGRESS_PEERS", "").strip() or "172.30.32.0/23"

#: Extra browser origins allowed to call the API, comma-separated. The page the
#: server serves is always allowed; this is for a dashboard hosted elsewhere.
CORS_ORIGINS = os.getenv("WACTORZ_CORS_ORIGINS", "")

#: Extra hostnames this server answers to, comma-separated. Loopback names and
#: address literals are always accepted. A name that is neither is refused even
#: when it resolves here, because that is what a DNS rebinding attack looks
#: like — set this to reach the dashboard by an mDNS or LAN name.
ALLOWED_HOSTS = os.getenv("WACTORZ_ALLOWED_HOSTS", "")


@dataclass(frozen=True)
class DeployTarget:
    """One SSH deploy target — a remote machine ``/deploy <name>`` can bootstrap.

    Targets come from the environment and nowhere else. SSH credentials used to
    be typed into chat as ``/deploy <node> <host> <user> <password>``, which put
    the password into the reply stream, the persisted conversation history and
    every chat log that history feeds. A target read from the environment never
    travels through a message.

    ``password`` is ``repr=False``: a target that ends up in a log line, an
    exception message or a debugger dump must not render its own secret. Prefer
    ``key_path`` — password auth is supported because most Pi images ship with
    it, not because it is the better option.
    """

    name: str
    host: str = ""
    user: str = "pi"
    key_path: str = ""
    password: str = field(default="", repr=False)
    ssh_port: int = 22
    broker: str = ""
    broker_port: int = 1883
    #: Broker credentials for this node. Empty means "use the server's own" —
    #: resolved at the point of use, not here, because this dataclass is built
    #: inside the ``AppConfig(...)`` call where ``CONFIG`` is still unbound.
    #: Resolving late also keeps the secret out of a frozen, logged dataclass.
    broker_user: str = ""
    broker_password: str = field(default="", repr=False)


def _env_slug(name: str) -> str:
    """Node name → the env-var infix it is configured under (``rpi-kitchen`` → ``RPI_KITCHEN``)."""
    return re.sub(r"[^A-Z0-9]+", "_", name.strip().upper()).strip("_")


#: Characters a node name may not contain, because it becomes one level of an
#: MQTT topic: ``#`` and ``+`` are the wildcards, ``/`` is the level separator.
_NODE_NAME_FORBIDDEN = ("#", "+", "/")


def deploy_name_error(node_name: str) -> str | None:
    """Why ``node_name`` cannot name a node, or None if it can.

    The name is interpolated straight into the runner's MQTT topics
    (``nodes/<name>/heartbeat`` to publish, ``nodes/<name>/spawn`` to
    subscribe), so a wildcard or a separator in it produces a runner that
    connects to the broker and is then refused on every publish and every
    subscribe. It retries forever, never appears in ``/nodes``, and fills its
    log — while the deploy that started it reports success. Catching the name
    up front is the difference between an error and a silent non-working node.
    """
    name = (node_name or "").strip()
    if not name:
        return "Node name is empty."
    bad = [c for c in _NODE_NAME_FORBIDDEN if c in name]
    if bad:
        return (
            f"Node name {name!r} contains {' and '.join(repr(c) for c in bad)}, which "
            f"cannot appear in an MQTT topic — the runner would be refused by the "
            f"broker on every publish and subscribe. Rename the node without it.\n"
            f"Note: '#' with no space before it does not start a comment in a .env "
            f"file, so `DEPLOY_TARGETS=rpi-kitchen#,rpi-garage` names a node "
            f"'rpi-kitchen#'."
        )
    if name != node_name:
        return f"Node name {node_name!r} has leading or trailing whitespace."
    return None


def _deploy_targets() -> tuple[DeployTarget, ...]:
    """Parse ``DEPLOY_TARGETS`` plus the ``DEPLOY_<NODE>_*`` block for each entry."""
    raw = (os.getenv("DEPLOY_TARGETS") or "").replace(";", ",")
    targets: list[DeployTarget] = []
    seen: set[str] = set()
    for entry in raw.split(","):
        node = entry.strip()
        slug = _env_slug(node)
        # A name that slugs to nothing ("---") has no env block to read, and a
        # duplicate would silently shadow the first — skip both rather than
        # build a target whose credentials come from somewhere unexpected.
        if not slug or slug in seen:
            continue
        seen.add(slug)
        targets.append(
            DeployTarget(
                name=node,
                host=os.getenv(f"DEPLOY_{slug}_HOST", "").strip(),
                user=os.getenv(f"DEPLOY_{slug}_USER", "").strip() or "pi",
                key_path=os.getenv(f"DEPLOY_{slug}_KEY", "").strip(),
                password=os.getenv(f"DEPLOY_{slug}_PASSWORD", ""),
                broker=os.getenv(f"DEPLOY_{slug}_BROKER", "").strip(),
                broker_port=_env_int(f"DEPLOY_{slug}_BROKER_PORT", 1883),
                ssh_port=_env_int(f"DEPLOY_{slug}_SSH_PORT", 22),
                broker_user=os.getenv(f"DEPLOY_{slug}_BROKER_USER", "").strip(),
                broker_password=os.getenv(f"DEPLOY_{slug}_BROKER_PASSWORD", ""),
            )
        )
    return tuple(targets)


@dataclass(frozen=True)
class AppConfig:
    """The settings a caller wants as a group, resolved once into `CONFIG`.

    Frozen because these describe the process rather than the moment: a caller
    that reads `CONFIG.port` halfway through a run gets what the process started
    with, and nothing can hand it something else. Anything that genuinely varies
    at runtime belongs in state, not here.

    Fields carry secrets -- API keys, broker and Home Assistant credentials -- so
    an instance is never serialised whole to a caller. `/api/config` names the
    handful of fields the browser needs and sends only those.
    """

    interface: str
    port: int
    llm_provider: str
    llm_model: str
    llm_api_key: str
    llm_overrides: str
    llm_temperature: float | None
    ollama_url: str
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    ha_url: str
    ha_token: str
    ha_state_bridge_output_topic: str
    ha_state_bridge_domains: str
    ha_state_bridge_per_entity: bool
    discord_token: str
    discord_webhook_url: str
    telegram_token: str
    telegram_allowed_user_id: int
    ws_port: int
    #: Interface every HTTP/WebSocket server binds to. See _bind_host().
    bind_host: str
    nim_api_key: str
    nvidia_api_key: str
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_whatsapp_number: str
    api_key: str
    # Remote-node deployment. Targets are environment-only (see DeployTarget);
    # the host-key settings govern every SSH connection the installer makes.
    deploy_targets: tuple[DeployTarget, ...]
    deploy_known_hosts: str
    deploy_strict_host_keys: bool
    weather_default_location: str
    llm_cost_limit_usd: float
    llm_cost_limit_period: str
    energy_rate: float
    energy_currency: str
    openai_url: str
    # Social-channel safety. The allow-lists say who may talk to a bot at all;
    # empty means the channel refuses to start rather than serving everyone.
    discord_allowed_user_ids: frozenset[int]
    telegram_allowed_user_ids: frozenset[int]
    whatsapp_allowed_numbers: frozenset[str]
    social_rate_limit_per_min: int


CONFIG = AppConfig(
    interface=os.getenv("INTERFACE", "rest" if DEV_MODE else "cli"),
    port=_env_int("PORT", 8080 if DEV_MODE else 8000),
    llm_provider=_env_name("LLM_PROVIDER", "anthropic"),
    llm_model=_env_name("LLM_MODEL", "claude-sonnet-4-6"),
    llm_api_key=os.getenv("LLM_API_KEY", ""),
    # Per-call-site model overrides, e.g. "intent=ollama:qwen3:4b,planner=anthropic:claude-sonnet-4-6".
    # See wactorz/llm_factory.py for the site list and format.
    llm_overrides=os.getenv("LLM_OVERRIDES", ""),
    # Sampling temperature for every LLM call (0.0 = deterministic). Unset or
    # empty keeps each provider's own default (the previous behavior).
    llm_temperature=_env_opt_float("LLM_TEMPERATURE"),
    ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
    bind_host=_bind_host(),
    mqtt_host=os.getenv("MQTT_HOST", "localhost"),
    mqtt_port=_env_int("MQTT_PORT", 1883),
    mqtt_username=os.getenv("MQTT_USERNAME", ""),
    mqtt_password=os.getenv("MQTT_PASSWORD", ""),
    ha_url=os.getenv("HA_URL", ""),
    ha_token=os.getenv("HA_TOKEN", ""),
    ha_state_bridge_output_topic=os.getenv(
        "HA_STATE_BRIDGE_OUTPUT_TOPIC", "homeassistant/state_changes"
    ),
    ha_state_bridge_domains=os.getenv("HA_STATE_BRIDGE_DOMAINS", ""),
    ha_state_bridge_per_entity=_env_truthy("HA_STATE_BRIDGE_PER_ENTITY"),
    # Also accept the shorter DISCORD_TOKEN / TELEGRAM_TOKEN names.
    discord_token=os.getenv("DISCORD_BOT_TOKEN", "") or os.getenv("DISCORD_TOKEN", ""),
    discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", "").strip(),
    telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_TOKEN", ""),
    telegram_allowed_user_id=_env_int("TELEGRAM_ALLOWED_USER_ID", 0),
    ws_port=_env_int("WS_PORT", 8888),
    nim_api_key=os.getenv("NIM_API_KEY", ""),
    nvidia_api_key=os.getenv("NVIDIA_API_KEY", ""),
    twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
    twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
    twilio_whatsapp_number=os.getenv("TWILIO_WHATSAPP_NUMBER", ""),
    api_key=os.getenv("API_KEY", ""),
    deploy_targets=_deploy_targets(),
    # Empty means "<state dir>/known_hosts", resolved at connect time so the
    # path follows WACTORZ_STATE_DIR instead of freezing the import-time value.
    deploy_known_hosts=os.getenv("DEPLOY_KNOWN_HOSTS", "").strip(),
    # Off by default: turning it on makes the first deploy to any machine fail
    # until its key is in the known-hosts file, which would break every existing
    # install. Off still verifies — it just learns an unknown key on first
    # contact instead of refusing it. See installer_agent._ssh_kwargs.
    deploy_strict_host_keys=_env_truthy("DEPLOY_STRICT_HOST_KEYS"),
    weather_default_location=os.getenv("WEATHER_DEFAULT_LOCATION", "London"),
    llm_cost_limit_usd=_env_float("LLM_COST_LIMIT_USD", 0.0),
    llm_cost_limit_period=os.getenv("LLM_COST_LIMIT_PERIOD", "monthly"),
    energy_rate=_env_float("ENERGY_RATE", 0.138),
    energy_currency=os.getenv("ENERGY_CURRENCY", "EUR"),
    openai_url=os.getenv("OPENAI_URL", ""),
    discord_allowed_user_ids=_env_id_set("DISCORD_ALLOWED_USER_IDS", "DISCORD_ALLOWED_USER_ID"),
    # TELEGRAM_ALLOWED_USER_ID (singular) predates the list form; still honored.
    telegram_allowed_user_ids=_env_id_set("TELEGRAM_ALLOWED_USER_IDS", "TELEGRAM_ALLOWED_USER_ID"),
    whatsapp_allowed_numbers=frozenset(
        n.strip()
        for n in (os.getenv("WHATSAPP_ALLOWED_NUMBERS", "") or "").replace(";", ",").split(",")
        if n.strip()
    ),
    social_rate_limit_per_min=_env_int("SOCIAL_RATE_LIMIT_PER_MIN", 12),
)


def deploy_target(node_name: str) -> DeployTarget | None:
    """The configured target for ``node_name``, or None if it isn't configured.

    Matching is on the slug, so ``/deploy rpi-kitchen`` and ``/deploy RPi Kitchen``
    both find the target configured under ``DEPLOY_RPI_KITCHEN_*``.
    """
    slug = _env_slug(node_name)
    if not slug:
        return None
    return next((t for t in CONFIG.deploy_targets if _env_slug(t.name) == slug), None)


def deploy_target_for_host(host: str) -> DeployTarget | None:
    """The configured target whose ``host`` matches, or None.

    Lets the installer re-authenticate to a node it only knows by address —
    ``node_install`` and ``node_run`` are given a host, not a node name.
    """
    host = (host or "").strip().lower()
    if not host:
        return None
    return next((t for t in CONFIG.deploy_targets if t.host.strip().lower() == host), None)


def deploy_target_names() -> list[str]:
    """Configured target names, for usage/error messages."""
    return [t.name for t in CONFIG.deploy_targets]


def deploy_env_prefix(node_name: str) -> str:
    """The env-var prefix a target for ``node_name`` is read from.

    ``deploy_env_prefix("rpi-kitchen")`` → ``"DEPLOY_RPI_KITCHEN"``, so the
    "not configured" messages can name the exact variables to set.
    """
    return f"DEPLOY_{_env_slug(node_name)}"


def deploy_target_help(node_name: str) -> str:
    """Why ``node_name`` was refused, and the env block that would configure it.

    Lives here rather than in a chat module because every surface that offers
    ``/deploy`` — web chat, the main actor's stream, the CLI — has to say the
    same thing, and what it says is a list of environment variable names.
    """
    configured = deploy_target_names()
    known = ", ".join(configured) if configured else "(none configured)"
    prefix = deploy_env_prefix(node_name)
    return (
        f"'{node_name}' is not a configured deploy target.\n"
        f"Configured targets: {known}\n\n"
        f"Deploy targets are set in the environment, not in chat:\n"
        f"  DEPLOY_TARGETS={node_name}\n"
        f"  {prefix}_HOST=192.168.1.50\n"
        f"  {prefix}_USER=pi\n"
        f"  {prefix}_KEY=/path/to/private_key   # or {prefix}_PASSWORD\n"
        f"  {prefix}_BROKER=192.168.1.10"
    )
