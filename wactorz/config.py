import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on", "dev"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    if not value:
        return default
    return int(value)


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


DEV_MODE = _env_truthy("WACTORZ_DEV_MODE")

_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    load_dotenv(_env_file)
else:
    load_dotenv(find_dotenv())


@dataclass(frozen=True)
class AppConfig:
    interface: str
    port: int
    llm_provider: str
    llm_model: str
    llm_api_key: str
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
    telegram_token: str
    telegram_allowed_user_id: int
    ws_port: int
    nim_api_key: str
    nvidia_api_key: str
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_whatsapp_number: str
    api_key: str
    nautilus_ssh_key: str
    nautilus_strict_host_keys: bool
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
    llm_provider=os.getenv("LLM_PROVIDER", "anthropic"),
    llm_model=os.getenv("LLM_MODEL", "claude-sonnet-4-6"),
    llm_api_key=os.getenv("LLM_API_KEY", ""),
    ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
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
    ha_state_bridge_per_entity=os.getenv("HA_STATE_BRIDGE_PER_ENTITY", "0")
    not in ("0", "false", "no"),
    # Also accept the shorter DISCORD_TOKEN / TELEGRAM_TOKEN names.
    discord_token=os.getenv("DISCORD_BOT_TOKEN", "") or os.getenv("DISCORD_TOKEN", ""),
    telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_TOKEN", ""),
    telegram_allowed_user_id=_env_int("TELEGRAM_ALLOWED_USER_ID", 0),
    ws_port=_env_int("WS_PORT", 8888),
    nim_api_key=os.getenv("NIM_API_KEY", ""),
    nvidia_api_key=os.getenv("NVIDIA_API_KEY", ""),
    twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
    twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
    twilio_whatsapp_number=os.getenv("TWILIO_WHATSAPP_NUMBER", ""),
    api_key=os.getenv("API_KEY", ""),
    nautilus_ssh_key=os.getenv("NAUTILUS_SSH_KEY", ""),
    nautilus_strict_host_keys=_env_truthy("NAUTILUS_STRICT_HOST_KEYS"),
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
