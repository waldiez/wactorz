"""CatalogAgent — Pre-built Agent Recipe Library
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Holds a catalog of ready-made DynamicAgent recipes (name → full spawn config).
On request it spawns any catalog agent by sending its full config to main,
which handles the actual DynamicAgent creation via the existing spawn pipeline.

This means:
  - No demo agents hardcoded in start.py
  - New recipes added here automatically become available system-wide
  - Main/planner discover catalog via capabilities and ask it to spawn by name
  - The spawned agent is saved in main's spawn registry (persists across restarts)

USAGE (from CLI or any agent):
  @catalog spawn anomaly-detector
  @catalog spawn timeseries-collector
  @catalog list
  @catalog info manual-agent

Or via main (natural language):
  "spawn the anomaly detector agent"   → main finds catalog → catalog spawns it
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import inspect
import logging
import pathlib
import time
from typing import TYPE_CHECKING, cast

from ..core.actor import Actor, Message, MessageType

if TYPE_CHECKING:
    from .main_actor import MainActor

logger = logging.getLogger(__name__)

BETA_WARNING = (
    "Experimental/Beta agent: behavior may change, fail, or be removed. "
    "Use it for trials, not unattended production workflows."
)

# ──────────────────────────────────────────────────────────────────────────────
# RECIPE IMPORTS
# ──────────────────────────────────────────────────────────────────────────────


def _chat_message_with_beta_warning(message: str, beta_warning: str) -> str:
    if not beta_warning:
        return message
    return f"{message}\n\nWarning: {beta_warning}"


def _load_recipe(filename: str) -> str | None:
    import importlib.util

    path = pathlib.Path(__file__).parent.parent / "catalogue_agents" / filename
    if not path.exists():
        logger.warning(f"[catalog] Recipe file not found: {path}")
        return None
    try:
        spec = importlib.util.spec_from_file_location("_recipe", path)
        if spec is None or spec.loader is None:
            logger.warning(f"[catalog] Could not build import spec for recipe: {path}")
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "AGENT_CODE", None)
    except Exception as e:
        logger.warning(f"[catalog] Could not load recipe from {filename}: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# CATALOG
# ──────────────────────────────────────────────────────────────────────────────


def _build_native_catalog() -> dict:
    """Native Actor subclasses spawned directly by the catalog."""
    native = {}

    try:
        from ..catalogue_agents.weather_agent import WeatherAgent

        native["weather-agent"] = {
            "name": "weather-agent",
            "type": "native",
            "factory": WeatherAgent,
            "description": "Natural-language weather: current conditions, forecast, and history via Open-Meteo. No API key required.",
            "capabilities": ["weather.current", "weather.forecast", "weather.history"],
            "input_schema": {
                "action": "current | forecast | history | set-default",
                "location": "str - city name or lat,lon (optional, falls back to default)",
                "days": "int - forecast horizon 1-16 (forecast only, default 3)",
                "date": "str - ISO date or 'yesterday' (history only)",
            },
            "output_schema": {
                "location": "str",
                "temp_c": "float",
                "feels_like_c": "float",
                "condition": "str",
                "humidity": "int",
                "wind_kph": "float",
            },
        }
        logger.info("[catalog] Loaded weather-agent recipe")
    except ImportError as e:
        logger.warning(f"[catalog] weather-agent unavailable: {e}")

    try:
        from .google_calendar_agent import GoogleCalendarAgent

        native["google-calendar-agent"] = {
            "name": "google-calendar-agent",
            "type": "native",
            "factory": GoogleCalendarAgent,
            "description": "Accesses Google Calendar: list today/upcoming events, create events, and delete events.",
            "capabilities": [
                "google_calendar",
                "calendar",
                "schedule",
                "events",
                "list_events",
                "create_event",
                "delete_event",
            ],
            "input_schema": {
                "text": "str - natural-language calendar request, e.g. 'what is on my calendar today?'",
                "operation": "status | list_events | today | tomorrow | week | create_event | delete_event",
                "summary": "str - event title for create_event",
                "start": "str - ISO-8601 start datetime for create_event",
                "event_id": "str - event id for delete_event",
            },
            "output_schema": {
                "result": "str - human-readable calendar response",
                "events": "list - returned events for list operations",
                "event": "dict - created event for create_event",
            },
        }
        logger.info("[catalog] Loaded google-calendar-agent recipe")
    except ImportError as e:
        logger.warning(f"[catalog] google-calendar-agent unavailable: {e}")

    try:
        from .gmail_agent import GmailAgent

        native["gmail-agent"] = {
            "name": "gmail-agent",
            "type": "native",
            "factory": GmailAgent,
            "description": "Accesses Gmail: search/read mail, list labels and drafts, and create drafts (never sends).",
            "capabilities": [
                "gmail",
                "email",
                "mail",
                "inbox",
                "search_email",
                "read_email",
                "create_draft",
                "labels",
            ],
            "input_schema": {
                "text": "str - natural-language Gmail request, e.g. 'any unread email?'",
                "operation": "status | search | unread | inbox | read | labels | drafts | create_draft",
                "query": "str - Gmail search query for search",
                "to": "str - recipient for create_draft",
                "subject": "str - subject for create_draft",
                "body": "str - body text for create_draft",
                "thread_id": "str - thread/message id for read",
            },
            "output_schema": {
                "result": "str - human-readable Gmail response",
                "status": "dict - sanitized Gmail configuration status",
            },
        }
        logger.info("[catalog] Loaded gmail-agent recipe")
    except ImportError as e:
        logger.warning(f"[catalog] gmail-agent unavailable: {e}")

    return native


def _build_experimental_catalog() -> dict:
    """Experimental native agents exposed through catalog with beta warnings."""
    experimental = {}

    try:
        from ..experimental_agents.code_agent import CodeAgent
        from ..experimental_agents.news_agent import NewsAgent
        from ..experimental_agents.qa_agent import QAAgent
        from ..experimental_agents.tick_agent import TickAgent
        from ..experimental_agents.wif_agent import WifAgent
        from ..experimental_agents.wiz_agent import WizAgent

        beta_recipes = {
            "code-agent": {
                "factory": CodeAgent,
                "description": "Experimental Python code generation and execution agent with configurable sandboxing.",
                "capabilities": ["code_generation", "code_execution", "python"],
                "input_schema": CodeAgent.INPUT_SCHEMA,
                "output_schema": CodeAgent.OUTPUT_SCHEMA,
            },
            "news-agent": {
                "factory": NewsAgent,
                "description": "Experimental on-demand Hacker News headlines agent. No API key required.",
                "capabilities": ["news", "hacker_news", "headlines"],
                "input_schema": {
                    "text": "str - optional feed/count command, e.g. 'top 10', 'new', 'jobs', or 'help'"
                },
                "output_schema": {"content": "str - formatted headline list or help text"},
            },
            "qa-agent": {
                "factory": QAAgent,
                "description": "Experimental passive safety observer for prompt injection, raw data bleed, and response timeouts.",
                "capabilities": [
                    "qa",
                    "safety",
                    "prompt_injection_detection",
                    "response_monitoring",
                ],
                "input_schema": {"from": "str", "content": "str - chat text to inspect"},
                "output_schema": {"category": "str", "severity": "str", "excerpt": "str"},
            },
            "chron-agent": {
                "factory": TickAgent,
                "description": "Experimental in-process scheduler for one-off and recurring reminders/tasks.",
                "capabilities": ["scheduler", "timers", "reminders"],
                "input_schema": {"text": "str - scheduling request or command"},
                "output_schema": {"result": "str - timer status or command response"},
            },
            "wif-agent": {
                "factory": WifAgent,
                "description": "Experimental local finance helper for expenses, budgets, ROI, loans, tax, and tips.",
                "capabilities": ["finance", "budgeting", "expense_tracking", "calculators"],
                "input_schema": {"text": "str - finance command such as 'add 12 food' or 'report'"},
                "output_schema": {"result": "str - finance calculation or report"},
            },
            "wiz-agent": {
                "factory": WizAgent,
                "description": "Experimental WaldiezCoin in-game economy tracker for agent activity and system events.",
                "capabilities": ["gamification", "coin_economy", "activity_tracking"],
                "input_schema": {
                    "text": "str - economy command such as 'balance', 'history', or 'earn 5'"
                },
                "output_schema": {"result": "str - balance or transaction response"},
            },
        }
        for name, recipe in beta_recipes.items():
            experimental[name] = {
                "name": name,
                "type": "native",
                "stability": "beta",
                "experimental": True,
                "warning": BETA_WARNING,
                **recipe,
            }
            logger.info("[catalog] Loaded experimental beta recipe %s", name)
    except ImportError as e:
        logger.warning(f"[catalog] experimental beta recipes unavailable: {e}")

    return experimental


def _build_catalog() -> dict:
    catalog = _build_native_catalog()
    catalog.update(_build_experimental_catalog())

    # ── doc-to-pptx-agent ─────────────────────────────────────────────────────
    code = _load_recipe("doc_to_pptx_agent.py")
    if code:
        catalog["doc-to-pptx-agent"] = {
            "name": "doc-to-pptx-agent",
            "type": "dynamic",
            "description": "Converts PDF or TXT documents into PowerPoint presentations. Extracts real embedded images from PDF; falls back to NIM FLUX for slides without images.",
            "capabilities": [
                "document_to_pptx",
                "pdf_to_presentation",
                "pptx_generation",
                "document_conversion",
            ],
            "install": ["pymupdf", "pdfplumber", "pillow"],
            "input_schema": {
                "file_path": "str  — absolute path to source PDF or TXT",
                "output_path": "str  — where to save the .pptx",
                "slide_count": "int  — target slides, default 8",
                "theme": "str  — e.g. 'dark executive', 'minimal light'",
                "nim_fallback": "bool — NIM images for slides without PDF image, default true",
                "min_img_width": "int  — min px width to accept PDF image, default 200",
                "min_img_height": "int  — min px height to accept PDF image, default 150",
            },
            "output_schema": {
                "pptx_path": "str       — saved .pptx path, or null",
                "slide_count": "int",
                "title": "str",
                "images_extracted": "int       — images pulled from PDF",
                "images_generated": "int       — images from NIM",
                "error": "str|null",
            },
            "poll_interval": 3600,
            "code": code,
        }

    # ── ADD NEW RECIPES HERE ──────────────────────────────────────────────────
    # code = _load_recipe("my_new_agent.py")
    # if code:
    #     catalog["my-new-agent"] = { ...spawn config..., "code": code }
    # # ─────────────────────────────────────────────────────────────────────────

    # ── anomaly-detector ───────────────────────────────────────────────────
    code = _load_recipe("anomaly_detector_agent.py")
    if code:
        catalog["anomaly-detector"] = {
            "name": "anomaly-detector",
            "type": "dynamic",
            "description": "Learns normal patterns from time-series data (HA sensors + Sinergym), detects anomalies in real-time. Statistical, range, rate-of-change, and absence detection.",
            "capabilities": [
                "anomaly_detection",
                "time_series",
                "monitoring",
                "building_analytics",
                "sinergym",
                "energy_monitoring",
                "comfort_monitoring",
                "ml",
            ],
            "install": ["aiomqtt", "numpy"],
            "input_schema": {
                "action": "str  — status|report|train|reset|configure|baselines|entities",
                "baseline_hours": "int  — hours of history for baseline (default: 720 = 30 days)",
                "learning_period_hours": "int  — min hours before detection starts (default: 168 = 1 week)",
                "sensitivity": "float — 0-1, lower=more sensitive (default: 0.3)",
                "entities": "list  — entity IDs to monitor (default: auto-discover)",
            },
            "output_schema": {
                "anomalies_detected": "int",
                "baselines_ready": "int",
                "detection_active": "bool",
                "last_anomaly": "dict|null",
            },
            "poll_interval": 3600,
            "code": code,
        }
        logger.info("[catalog] Loaded anomaly-detector recipe")

    # ── smart-energy ───────────────────────────────────────────────────
    code = _load_recipe("smart_energy_agent.py")
    if code:
        catalog["smart-energy"] = {
            "name": "smart-energy",
            "type": "dynamic",
            "description": "LLM-powered energy brain for smart plugs via Home Assistant (brand-agnostic). "
            "Talk to it in plain English: say 'import my plugs' and it scans HA, shows what "
            "it found with live wattage, and asks which to monitor — no JSON needed. "
            "Monitors live wattage, tracks per-plug kWh and cost (today/week/month), and can "
            "conditionally power down plugs via user-requested rules. Plugs marked 'locked' "
            "(AC, servers, AI rigs) are NEVER turned off — hard-guarded in code.",
            "capabilities": [
                "energy_monitoring",
                "smart_plug",
                "cost_tracking",
                "home_assistant",
                "power_monitoring",
                "tapo",
                "shelly",
            ],
            "install": [],
            "input_schema": {
                "text": "str  — PRIMARY interface: plain-English request. 'import my plugs' "
                "starts conversational onboarding; 'how much did the AC cost today?' "
                "asks a question; 'status' shows an overview.",
                "action": "str  — optional structured command for power users: status|cost|report|"
                "add_plug|list_plugs|remove_plug|add_rule|list_rules|remove_rule|set_rate",
                "plug": "dict — plug config: {name, ha_entity_switch, ha_entity_power, "
                "protection: locked|auto_off_on_idle|manual} (for add_plug)",
                "rule": "dict — rule config, e.g. {type: auto_off_on_idle, plug, "
                "idle_threshold_watts, idle_delay_s} (for add_rule)",
                "rate": "float — €/kWh (for set_rate, default 0.138)",
            },
            "output_schema": {
                "plugs_monitored": "int",
                "active_rules": "int",
                "total_watts": "float",
            },
            "poll_interval": 30,
            "code": code,
        }
        logger.info("[catalog] Loaded smart-energy recipe")

    # ── manual-agent ───────────────────────────────────────────────────
    code = _load_recipe("manual_agent.py")
    if code:
        catalog["manual-agent"] = {
            "name": "manual-agent",
            "type": "dynamic",
            "description": "Searches the internet for device manuals, downloads PDFs, extracts text, and answers questions using the agent's LLM.",
            "capabilities": ["web_search", "pdf_extraction", "qa_assistant", "device_manuals"],
            "install": ["httpx", "pdfplumber", "duckduckgo_search"],
            "input_schema": {
                "action": "str  — load_manual|ask|status|clear",
                "device": "str  — The device model name or query (for load_manual)",
                "question": "str  — The question to ask about the loaded manual (for ask)",
            },
            "output_schema": {
                "success": "bool — True if operation succeeded",
                "device": "str  — Device model name",
                "url": "str  — URL of the downloaded manual PDF",
                "pages": "int  — Number of pages in the PDF",
                "chars": "int  — Character count of extracted text",
                "preview": "str  — Preview snippet of text",
                "answer": "str  — LLM generated answer to your question",
            },
            "poll_interval": 3600,  # Event-driven via direct actions/messages
            "code": code,
        }
        logger.info("[catalog] Loaded manual-agent recipe")

    # ── reachy-mini ──────────────────────────────────────────────────────────
    code = _load_recipe("reachy_mini_agent.py")
    if code:
        catalog["reachy-mini"] = {
            "name": "reachy-mini",
            "type": "dynamic",
            "description": (
                "Controls a Reachy Mini: wake/sleep, head pose, antennas, gaze, "
                "speech, gestures, and optional Home Assistant actions."
            ),
            "docs": (
                "Setup:\n"
                "1. Install the recipe dependencies when prompted, or preinstall: "
                "pip install reachy-mini numpy edge-tts.\n"
                "2. For Reachy Mini Wireless, put the robot and Wactorz host on the "
                "same WiFi network. Stop any Hugging Face app running on the robot.\n"
                "3. For Reachy Mini Lite, start the local daemon first: "
                "reachy-mini-daemon -p <serial_port>.\n"
                "4. Spawn the agent: @catalog spawn reachy-mini.\n"
                "5. If discovery is flaky, pin the Wireless host by publishing "
                '{"robot_host": "192.168.1.42"} to custom/reachy/config, then restart '
                "the agent.\n"
                "\n"
                "Try:\n"
                "- wake up\n"
                "- do a happy gesture\n"
                "- wiggle your antennas\n"
                "- look left\n"
                "- say hello\n"
                "- turn on the light and nod\n"
                "\n"
                "For structured control, send a dict with cmd wake, sleep, pose, "
                "antennas, look_at, emotion, say, volume, ha, bind, unbind, or stop."
            ),
            "capabilities": [
                "robot",
                "reachy",
                "reachy_mini",
                "embodied",
                "motion",
                "head",
                "antennas",
                "gaze",
                "emotion",
                "actuator",
                "expressive",
                "human_robot_interaction",
            ],
            "install": ["reachy-mini", "numpy", "edge-tts"],
            "input_schema": {
                "cmd": "str  — wake|sleep|pose|antennas|look_at|look_pixel|emotion|set_pose|bind|unbind|list_emotions|stop|say|volume|ha",
                "text": "str   — words to speak (cmd=say); TTS via edge-tts through Reachy's speaker",
                "voice": "str   — edge-tts voice (cmd=say); auto-picks by script, e.g. el-GR for Greek",
                "gain_db": "float — per-say file trim in dB (cmd=say), <=0 to make one line quieter",
                "loud": "bool  — cmd=say; default true (compress+limit file to max); false plays raw quiet TTS",
                "preset": "str   — speaking mode (cmd=volume): whisper(70)|normal(85)|louder(93)|presenter(100)",
                "level": "float — 0-100 robot speaker volume (cmd=volume); 100=loudest, 0=quietest (daemon /api/volume/set)",
                "delta": "float — relative volume change in level points (cmd=volume), e.g. +15 / -25",
                "mute": "bool  — cmd=volume; true silences (remembers level), false restores it",
                "request": "str   — natural-language Home Assistant request (cmd=ha); routed through main for device control, home-assistant-agent for automations/info",
                "duration": "float — motion duration in seconds (pose/antennas/look_at)",
                "method": "str  — interpolation: linear|minjerk|ease_in_out|cartoon (default minjerk)",
                "yaw": "float — head yaw, degrees by default",
                "pitch": "float — head pitch, degrees by default",
                "roll": "float — head roll, degrees by default",
                "x": "float — head x (mm) or look_at world x (m)",
                "y": "float — head y (mm) or look_at world y (m)",
                "z": "float — head z (mm) or look_at world z (m)",
                "antennas": "list  — [right, left] angles, degrees by default",
                "left": "float — antenna left (cmd=antennas convenience)",
                "right": "float — antenna right (cmd=antennas convenience)",
                "u": "int   — pixel u for look_pixel",
                "v": "int   — pixel v for look_pixel",
                "name": "str   — emotion clip name (e.g. curious1, success1)",
                "topic": "str   — MQTT topic to bind/unbind",
                "when": "dict  — dotted-path equality matcher for bindings",
                "do": "dict  — payload to dispatch when binding fires",
                "id": "str   — optional correlation id; ack on custom/reachy/cmd_result/{id}",
            },
            "output_schema": {
                "ok": "bool",
                "cmd": "str",
                "duration_s": "float — wall-clock motion time",
                "error": "str|null",
            },
            "poll_interval": 5,
            "code": code,
        }
        logger.info("[catalog] Loaded reachy-mini recipe")

    # ── timeseries-collector ───────────────────────────────────────────────
    code = _load_recipe("timeseries_collector_agent.py")
    if code:
        catalog["timeseries-collector"] = {
            "name": "timeseries-collector",
            "type": "dynamic",
            "description": "Collects device data from MQTT (sensors, detections, HA state changes, Sinergym) and writes it to SQLite time-series tables for historical queries and ML training. Batched writes, auto-pruned by retention window. No LLM.",
            "capabilities": [
                "timeseries",
                "data_collection",
                "sensor_history",
                "ml_data",
                "mqtt_subscriber",
                "monitoring",
            ],
            "install": ["aiomqtt"],
            "input_schema": {
                "action": "str  — stats|prune|flush",
                "topics": "list  — MQTT topic patterns to collect (default: sensors/#, detections, HA state, sinergym)",
                "batch_interval": "float — seconds between SQLite flushes (default: 5.0)",
                "batch_size": "int  — buffered-row hint before flush (default: 200)",
                "retention_days": "int  — auto-prune data older than N days (default: 90)",
                "prune_interval_hours": "float — hours between prune passes (default: 6.0)",
            },
            "output_schema": {
                "total_received": "int  — MQTT messages seen",
                "total_written": "int  — rows written to SQLite",
                "buffer_sizes": "dict — pending rows per buffer",
                "table_rows": "dict — row counts per table",
                "retention_days": "int",
            },
            "poll_interval": 3600,
            "code": code,
        }
        logger.info("[catalog] Loaded timeseries-collector recipe")

    return catalog


# ──────────────────────────────────────────────────────────────────────────────
# CATALOG AGENT
# ──────────────────────────────────────────────────────────────────────────────


class CatalogAgent(Actor):
    """Pre-built agent recipe library.
    Spawns any catalog agent on request by delegating to main's spawn pipeline.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "catalog")
        super().__init__(**kwargs)
        self.protected = True
        self._catalog = _build_catalog()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def on_start(self):
        names = list(self._catalog.keys())
        logger.info(f"[{self.name}] Catalog ready — {len(names)} recipe(s): {names}")
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {
                "type": "log",
                "message": f"Catalog ready: {', '.join(names)}",
                "timestamp": time.time(),
            },
        )

        await self.publish_manifest(
            description=(
                "Pre-built agent recipe library. "
                "Spawns ready-made agents by name without requiring code. "
                f"Available: {', '.join(names)}"
            ),
            capabilities=["spawn_catalog_agent", "list_catalog_agents", "agent_catalog"],
            input_schema={
                "action": "str — 'spawn' | 'list' | 'info'",
                "agent": "str — agent name for spawn/info actions",
            },
            output_schema={"ok": "bool", "message": "str", "agents": "list", "recipe": "dict"},
        )

        # Inject recipe manifests directly into main's _agent_manifests dict
        main: MainActor | None = None
        for _ in range(20):
            main = cast(
                "MainActor | None", self._registry.find_by_name("main") if self._registry else None
            )
            if main and hasattr(main, "_agent_manifests"):
                break
            await asyncio.sleep(0.5)

        for name, recipe in self._catalog.items():
            manifest = {
                "name": name,
                "actor_id": f"catalog.{name}",
                "description": recipe.get("description", ""),
                "capabilities": recipe.get("capabilities", []),
                "input_schema": recipe.get("input_schema", {}),
                "output_schema": recipe.get("output_schema", {}),
                "stability": recipe.get("stability", "stable"),
                "experimental": bool(recipe.get("experimental", False)),
                "warning": recipe.get("warning", ""),
                "publishes": [],
                "spawnable": True,
                "catalog": self.name,
                "timestamp": time.time(),
            }

            if main and hasattr(main, "_agent_manifests"):
                main._agent_manifests[name] = manifest
                logger.info(f"[{self.name}] Injected manifest for '{name}' into main")
            else:
                logger.warning(
                    f"[{self.name}] main not ready — could not inject manifest for '{name}'"
                )

    def _current_task_description(self) -> str:
        return f"catalog ({len(self._catalog)} recipes)"

    # ── Message handling ───────────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        if msg.type != MessageType.TASK:
            return

        payload = msg.payload if msg.payload is not None else {}
        result = await self._handle(payload)

        task_id = (
            payload.get("task") or payload.get("_task_id") if isinstance(payload, dict) else None
        )
        if task_id:
            result["task"] = task_id
            result["_task_id"] = task_id

        target = msg.reply_to or msg.sender_id
        if target:
            await self.send(target, MessageType.RESULT, result)

    async def _handle(self, payload) -> dict:
        if isinstance(payload, dict) and payload.get("action"):
            action = payload["action"].lower().strip()
            if action == "list":
                return self._action_list()
            if action == "info":
                return self._action_info(payload.get("agent", ""))
            if action == "spawn":
                return await self._action_spawn(payload.get("agent", ""), payload)
            return {"ok": False, "message": f"Unknown action '{action}'. Use: spawn | list | info"}

        if isinstance(payload, dict) and "spawn" in payload and isinstance(payload["spawn"], str):
            return await self._action_spawn(payload["spawn"], payload)

        if isinstance(payload, str):
            text = payload.strip()
        elif isinstance(payload, dict):
            text = (
                payload.get("text") or payload.get("message") or payload.get("query") or ""
            ).strip()
        else:
            text = ""

        if text:
            parts = text.split(None, 1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
            if cmd == "list":
                return self._action_list()
            if cmd == "info":
                return self._action_info(arg)
            if cmd == "spawn":
                return await self._action_spawn(arg, {})
            if self._resolve_name(cmd):
                return await self._action_spawn(cmd, {})

        return self._action_list()

    # ── Name resolution ───────────────────────────────────────────────────────

    def _resolve_name(self, raw: str) -> str | None:
        """Map a freeform name to a catalog key.

        Tries in order:
          1. Exact match
          2. Normalised match (spaces/underscores → dashes, lowercase)
          3. Strip a trailing ' agent' / '-agent' suffix and retry
          4. Word-subset match: any catalog key whose slug-words are all present
             in the input (ignoring the word 'agent')
        """
        if not raw:
            return None

        # 1. Exact
        if raw in self._catalog:
            return raw

        # 2. Normalised
        norm = raw.lower().strip().replace("_", "-").replace(" ", "-")
        if norm in self._catalog:
            return norm

        # 3. Strip trailing '-agent' suffix
        stripped = norm.removesuffix("-agent")
        if stripped and stripped in self._catalog:
            return stripped

        # 4. Word-subset: split input into meaningful words (drop 'agent'),
        #    then find the first catalog key whose words are all present.
        stop = {"agent", "the", "a", "an"}
        input_words = {w for w in norm.replace("-", " ").split() if w not in stop}
        if input_words:
            for key in self._catalog:
                key_words = set(key.replace("-", " ").split()) - stop
                if key_words and key_words.issubset(input_words):
                    return key

        return None

    # ── Actions ────────────────────────────────────────────────────────────────

    def _action_list(self) -> dict:
        agents = []
        for name, recipe in self._catalog.items():
            agents.append(
                {
                    "name": name,
                    "description": recipe.get("description", ""),
                    "capabilities": recipe.get("capabilities", []),
                    "stability": recipe.get("stability", "stable"),
                    "experimental": bool(recipe.get("experimental", False)),
                    "warning": recipe.get("warning", ""),
                }
            )
        return {
            "ok": True,
            "message": f"{len(agents)} agent(s) available in catalog",
            "agents": agents,
        }

    def _action_info(self, name: str) -> dict:
        if not name:
            return {"ok": False, "message": "Provide 'agent' name for info action"}
        resolved = self._resolve_name(name)
        recipe = self._catalog.get(resolved) if resolved else None
        if not recipe:
            available = list(self._catalog.keys())
            return {"ok": False, "message": f"'{name}' not in catalog. Available: {available}"}
        safe = {k: v for k, v in recipe.items() if k not in {"code", "factory"}}
        message = f"Recipe for '{resolved}'"
        if recipe.get("experimental"):
            message += (
                f" ({recipe.get('stability', 'beta')}: {recipe.get('warning', BETA_WARNING)})"
            )
        return {"ok": True, "message": message, "recipe": safe}

    async def _action_spawn(self, name: str, payload: dict) -> dict:
        if not name:
            return {"ok": False, "message": "Provide 'agent' name to spawn"}

        resolved = self._resolve_name(name)
        recipe = self._catalog.get(resolved) if resolved else None
        if not resolved or not recipe:
            available = list(self._catalog.keys())
            return {"ok": False, "message": f"'{name}' not in catalog. Available: {available}"}

        if not self._registry:
            return {"ok": False, "message": "No registry available — cannot spawn"}

        existing = self._registry.find_by_name(resolved)
        if existing:
            return {"ok": True, "message": f"'{resolved}' is already running"}

        beta_warning = recipe.get("warning") if recipe.get("experimental") else ""
        if beta_warning:
            await self._mqtt_publish(
                f"agents/{self.actor_id}/alert",
                {
                    "severity": "warning",
                    "message": f"{resolved} is Experimental/Beta. {beta_warning}",
                    "timestamp": time.time(),
                },
            )

        logger.info(f"[{self.name}] Spawning '{resolved}'...")
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": f"Spawning '{resolved}'...", "timestamp": time.time()},
        )

        try:
            main = cast("MainActor | None", self._registry.find_by_name("main"))
            llm_provider = getattr(main, "llm", None) if main else None
            persistence_dir = (
                str(getattr(main, "_persistence_dir", pathlib.Path("./state/main")).parent)
                if main
                else "./state"
            )

            if recipe.get("type") == "native":
                factory = recipe.get("factory")
                if not factory:
                    return {"ok": False, "message": f"Native recipe '{resolved}' has no factory"}
                native_kwargs = {"name": resolved, "persistence_dir": persistence_dir}
                factory_params = inspect.signature(factory).parameters
                from .llm_agent import LLMAgent

                accepts_llm_provider = "llm_provider" in factory_params or issubclass(
                    factory, LLMAgent
                )
                if llm_provider and accepts_llm_provider:
                    native_kwargs["llm_provider"] = llm_provider
                actor = await self.spawn(factory, **native_kwargs)
                if actor:
                    msg = _chat_message_with_beta_warning(
                        f"'{resolved}' spawned and running", beta_warning
                    )
                    logger.info(f"[{self.name}] {msg}")
                    await self._mqtt_publish(
                        f"agents/{self.actor_id}/logs",
                        {"type": "log", "message": msg, "timestamp": time.time()},
                    )
                    return {"ok": True, "message": msg, "agent": resolved}
                return {"ok": False, "message": f"Spawn returned no actor for '{resolved}'"}

            from .dynamic_agent import DynamicAgent

            install = recipe.get("install", [])
            if install:
                # Fast-path: check which packages are already importable.
                # Avoids a 120s installer wait when deps were installed in a
                # previous session — same logic as main._spawn_dynamic_agent.
                import importlib as _importlib

                # Map pip package names to their actual import names where they differ.
                _IMPORT_NAME_MAP = {
                    "scikit-learn": "sklearn",
                    "stable-baselines3": "stable_baselines3",
                    "pillow": "PIL",
                    "pyyaml": "yaml",
                    "pymupdf": "fitz",
                    "beautifulsoup4": "bs4",
                    "python-dateutil": "dateutil",
                    "typing-extensions": "typing_extensions",
                    "opencv-python": "cv2",
                    "scikit-image": "skimage",
                }
                needed = []
                for pkg in install:
                    pip_name = pkg.split("[")[0].lower()
                    import_name = _IMPORT_NAME_MAP.get(pip_name) or pip_name.replace("-", "_")
                    try:
                        _importlib.import_module(import_name)
                    except ImportError:
                        needed.append(pkg)

                if needed:
                    installer = self._registry.find_by_name("installer") if self._registry else None
                    if installer:
                        logger.info(f"[{self.name}] Installing missing deps for '{name}': {needed}")
                        import uuid as _uuid

                        task_id = f"cat_install_{_uuid.uuid4().hex[:8]}"
                        future = asyncio.get_running_loop().create_future()
                        main = cast(
                            "MainActor | None",
                            self._registry.find_by_name("main") if self._registry else None,
                        )
                        if main:
                            main._result_futures[task_id] = future
                        # Send with reply_to=main.actor_id so the installer's RESULT goes
                        # directly to main where the future is registered.
                        install_msg = Message(
                            type=MessageType.TASK,
                            sender_id=self.actor_id,
                            reply_to=main.actor_id if main else self.actor_id,
                            payload={
                                "action": "install",
                                "packages": needed,
                                "task": task_id,
                                "_task_id": task_id,
                            },
                        )
                        await installer.receive(install_msg)
                        try:
                            await asyncio.wait_for(future, timeout=120.0)
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"[{self.name}] Install timeout for '{name}' — proceeding anyway"
                            )
                    else:
                        logger.warning(
                            f"[{self.name}] installer not found — skipping dep install for '{name}'"
                        )
                else:
                    logger.info(
                        f"[{self.name}] All deps for '{resolved}' already installed — skipping installer"
                    )

            actor = await self.spawn(
                DynamicAgent,
                name=resolved,
                code=recipe["code"],
                poll_interval=float(recipe.get("poll_interval", 3600)),
                description=recipe.get("description", ""),
                input_schema=recipe.get("input_schema", {}),
                output_schema=recipe.get("output_schema", {}),
                llm_provider=llm_provider,
                persistence_dir=persistence_dir,
                trusted=True,  # catalog agents are pre-built — skip safety validator
            )

            if actor:
                if main and hasattr(main, "_save_to_spawn_registry"):
                    # Mark as trusted so it bypasses safety validator on restore
                    save_config = dict(recipe)
                    save_config["trusted"] = True
                    main._save_to_spawn_registry(save_config)

                msg = _chat_message_with_beta_warning(
                    f"'{resolved}' spawned and running", beta_warning
                )
                logger.info(f"[{self.name}] {msg}")
                await self._mqtt_publish(
                    f"agents/{self.actor_id}/logs",
                    {"type": "log", "message": msg, "timestamp": time.time()},
                )
                return {"ok": True, "message": msg, "agent": resolved}
            return {"ok": False, "message": f"Spawn returned no actor for '{resolved}'"}

        except Exception as e:
            msg = f"Failed to spawn '{resolved}': {e}"
            logger.error(f"[{self.name}] {msg}")
            return {"ok": False, "message": msg}

    # Public API ─────────────────────────────────────────────────────────────

    def list_recipes(self) -> list[str]:
        return list(self._catalog.keys())

    def get_recipe(self, name: str) -> dict | None:
        return self._catalog.get(name)
