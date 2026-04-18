from dotenv import load_dotenv, find_dotenv
from dataclasses import dataclass
from pathlib import Path
import os


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
    fuseki_url: str
    fuseki_dataset: str
    fuseki_user: str
    fuseki_password: str


CONFIG = AppConfig(
    interface=os.getenv("INTERFACE", "rest" if DEV_MODE else "cli"),
    port=_env_int("PORT", 8080 if DEV_MODE else 8000),
    llm_provider=os.getenv("LLM_PROVIDER", "anthropic"),
    llm_model=os.getenv("LLM_MODEL", "claude-sonnet-4-6"),
    llm_api_key=os.getenv("LLM_API_KEY", ""),
    ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
    mqtt_host=os.getenv("MQTT_HOST", "localhost"),
    mqtt_port=_env_int("MQTT_PORT", 1883),
    ha_url=os.getenv("HA_URL", ""),
    ha_token=os.getenv("HA_TOKEN", ""),
    ha_state_bridge_output_topic=os.getenv("HA_STATE_BRIDGE_OUTPUT_TOPIC", "homeassistant/state_changes"),
    ha_state_bridge_domains=os.getenv("HA_STATE_BRIDGE_DOMAINS", ""),
    ha_state_bridge_per_entity=os.getenv("HA_STATE_BRIDGE_PER_ENTITY", "0") not in ("0", "false", "no"),
    discord_token=os.getenv("DISCORD_BOT_TOKEN", ""),
    telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
    telegram_allowed_user_id=_env_int("TELEGRAM_ALLOWED_USER_ID", 0),
    ws_port=_env_int("WS_PORT", 8888),
    nim_api_key=os.getenv("NIM_API_KEY", ""),
    nvidia_api_key=os.getenv("NVIDIA_API_KEY", ""),
    twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
    twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
    twilio_whatsapp_number=os.getenv("TWILIO_WHATSAPP_NUMBER", ""),
    api_key=os.getenv("API_KEY", ""),
    nautilus_ssh_key=os.getenv("NAUTILUS_SSH_KEY", ""),
    nautilus_strict_host_keys=os.getenv("NAUTILUS_STRICT_HOST_KEYS", "0"),
    weather_default_location=os.getenv("WEATHER_DEFAULT_LOCATION", "London"),
    fuseki_url=os.getenv("FUSEKI_URL", "http://localhost:3030"),
    fuseki_dataset=os.getenv("FUSEKI_DATASET", "wactorz"),
    fuseki_user=os.getenv("FUSEKI_USER", "admin"),
    fuseki_password=os.getenv("FUSEKI_PASSWORD", "admin")
)
