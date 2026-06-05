"""
CatalogAgent — Pre-built Agent Recipe Library
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
  @catalog spawn image-gen-agent
  @catalog spawn sinergym-collector
  @catalog list
  @catalog info sinergym-optimizer

Or via main (natural language):
  "spawn the image generation agent"   → main finds catalog → catalog spawns it
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import logging
import time
from typing import Optional

from ..core.actor import Actor, Message, MessageType

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# RECIPE IMPORTS
# ──────────────────────────────────────────────────────────────────────────────

def _load_recipe(filename: str) -> Optional[str]:
    import importlib.util, pathlib
    path = pathlib.Path(__file__).parent.parent / "catalogue_agents" / filename
    if not path.exists():
        logger.warning(f"[catalog] Recipe file not found: {path}")
        return None
    try:
        spec = importlib.util.spec_from_file_location("_recipe", path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "AGENT_CODE", None)
    except Exception as e:
        logger.warning(f"[catalog] Could not load recipe from {filename}: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# CATALOG
# ──────────────────────────────────────────────────────────────────────────────

def _build_catalog() -> dict:
    catalog = {}

    # ── image-gen-agent ───────────────────────────────────────────────────────
    code = _load_recipe("image_gen_agent.py")
    if code:
        catalog["image-gen-agent"] = {
            "name":         "image-gen-agent",
            "type":         "dynamic",
            "description":  "Generates images from text prompts using NVIDIA NIM FLUX.1-dev. Returns absolute PNG path.",
            "capabilities": ["image_generation", "text_to_image", "nvidia_nim", "flux"],
            "install":      ["requests"],
            "input_schema": {
                "prompt":      "str  — what to generate",
                "output_path": "str  — absolute path to save PNG",
                "width":       "int  — pixels wide, default 1024",
                "height":      "int  — pixels tall, default 576 (16:9)",
                "steps":       "int  — inference steps, default 20",
                "api_key":     "str  — optional, overrides persisted nim_api_key",
            },
            "output_schema": {
                "image_path": "str       — saved PNG path, or null",
                "width":      "int",
                "height":     "int",
                "size_kb":    "int",
                "error":      "str|null",
            },
            "poll_interval": 3600,
            "code":          code,
        }

    # ── doc-to-pptx-agent ─────────────────────────────────────────────────────
    code = _load_recipe("doc_to_pptx_agent.py")
    if code:
        catalog["doc-to-pptx-agent"] = {
            "name":         "doc-to-pptx-agent",
            "type":         "dynamic",
            "description":  "Converts PDF or TXT documents into PowerPoint presentations. Extracts real embedded images from PDF; falls back to NIM FLUX for slides without images.",
            "capabilities": ["document_to_pptx", "pdf_to_presentation", "pptx_generation", "document_conversion"],
            "install":      ["pymupdf", "pdfplumber", "pillow"],
            "input_schema": {
                "file_path":      "str  — absolute path to source PDF or TXT",
                "output_path":    "str  — where to save the .pptx",
                "slide_count":    "int  — target slides, default 8",
                "theme":          "str  — e.g. 'dark executive', 'minimal light'",
                "nim_fallback":   "bool — NIM images for slides without PDF image, default true",
                "min_img_width":  "int  — min px width to accept PDF image, default 200",
                "min_img_height": "int  — min px height to accept PDF image, default 150",
            },
            "output_schema": {
                "pptx_path":        "str       — saved .pptx path, or null",
                "slide_count":      "int",
                "title":            "str",
                "images_extracted": "int       — images pulled from PDF",
                "images_generated": "int       — images from NIM",
                "error":            "str|null",
            },
            "poll_interval": 3600,
            "code":          code,
        }

   
    # # ── discord-notify-agent ──────────────────────────────────────────────────
    # code = _load_recipe("discord_notify_agent.py")
    # if code:
    #     catalog["discord-notify-agent"] = {
    #         "name":         "discord-notify-agent",
    #         "type":         "dynamic",
    #         "description":  "Subscribes to MQTT events and posts notifications to a Discord webhook.",
    #         "capabilities": ["discord", "notifications", "mqtt_subscriber", "webhook", "alerting"],
    #         "install":      ["aiohttp", "aiomqtt"],
    #         "input_schema": {
    #             "mqtt_topic":    "str — MQTT topic to subscribe to",
    #             "message_tpl":   "str — message template, use {data} for payload",
    #             "trigger_key":   "str — optional: only trigger when this key exists",
    #             "trigger_value": "str — optional: only trigger when trigger_key equals this",
    #             "cooldown_s":    "int — seconds between notifications, default 10",
    #             "webhook_url":   "str — Discord webhook URL (overrides persisted value)",
    #         },
    #         "output_schema": {"sent": "int — number of notifications sent"},
    #         "poll_interval": 3600,
    #         "code":          code,
    #     }

    # ── sinergym-collector ────────────────────────────────────────────────────
    code = _load_recipe("sinergym_collector_agent.py")
    if code:
        catalog["sinergym-collector"] = {
            "name":         "sinergym-collector",
            "type":         "dynamic",
            "description":  "Collects Sinergym episode data via MQTT for RL/Bayesian training. Listens on sinergym/env/{env_id}/observation and persists (obs, action, reward) tuples.",
            "capabilities": ["sinergym", "data_collection", "rl_training", "energy_optimization", "building_simulation"],
            "install":      ["aiomqtt", "numpy"],
            "input_schema": {
                "env_id":          "str  — Sinergym env ID, e.g. Eplus-5zone-hot-continuous-v1",
                "obs_topic":       "str  — MQTT topic for observations",
                "target_episodes": "int  — episodes to collect before triggering optimizer, default 10",
                "chunk_size":      "int  — persist every N episodes, default 5",
                "optimizer_name":  "str  — optimizer agent to notify on completion, default sinergym-optimizer",
            },
            "output_schema": {
                "episodes_collected": "int",
                "total_steps":        "int",
                "data_key":           "str — episode_{N} recall keys",
            },
            "poll_interval": 3600,
            "code":          code,
        }
        logger.info("[catalog] Loaded sinergym-collector recipe")

    # ── sinergym-optimizer ────────────────────────────────────────────────────
    code = _load_recipe("sinergym_optimizer_agent.py")
    if code:
        catalog["sinergym-optimizer"] = {
            "name":         "sinergym-optimizer",
            "type":         "dynamic",
            "description":  "Energy optimization agent for Sinergym: trains RL (PPO) or Bayesian (GP) policy from collected episodes, then publishes actions to sinergym/env/{env_id}/action.",
            "capabilities": ["sinergym", "rl", "bayesian_optimization", "energy_optimization", "policy_training", "building_control"],
            "install":      ["stable-baselines3", "scikit-learn", "numpy", "torch", "aiomqtt", "gymnasium"],
            "input_schema": {
                "env_id":          "str  — Sinergym env ID, e.g. Eplus-5zone-hot-continuous-v1",
                "strategy":        "str  — rl | bayesian | rulebased | combined, default rl",
                "collector_name":  "str  — collector agent name, default sinergym-collector",
                "obs_dim":         "int  — observation vector length, default 35",
                "action_dim":      "int  — action vector length, default 2",
                "training_steps":  "int  — RL training timesteps, default 50000",
                "deploy_on_train": "bool — start publishing actions after training, default true",
            },
            "output_schema": {
                "mean_reward": "float",
                "strategy":    "str",
                "phase":       "str — idle | training | deploying",
            },
            "poll_interval": 3600,
            "code":          code,
        }
        logger.info("[catalog] Loaded sinergym-optimizer recipe")

    # ── ADD NEW RECIPES HERE ──────────────────────────────────────────────────
    # code = _load_recipe("my_new_agent.py")
    # if code:
    #     catalog["my-new-agent"] = { ...spawn config..., "code": code }
    # # ─────────────────────────────────────────────────────────────────────────

    # ── anomaly-detector ───────────────────────────────────────────────────
    code = _load_recipe("anomaly_detector_agent.py")
    if code:
        catalog["anomaly-detector"] = {
            "name":         "anomaly-detector",
            "type":         "dynamic",
            "description":  "Learns normal patterns from time-series data (HA sensors + Sinergym), detects anomalies in real-time. Statistical, range, rate-of-change, and absence detection.",
            "capabilities": ["anomaly_detection", "time_series", "monitoring", "building_analytics",
                             "sinergym", "energy_monitoring", "comfort_monitoring", "ml"],
            "install":      ["aiomqtt", "numpy"],
            "input_schema": {
                "action":                "str  — status|report|train|reset|configure|baselines|entities",
                "baseline_hours":        "int  — hours of history for baseline (default: 720 = 30 days)",
                "learning_period_hours": "int  — min hours before detection starts (default: 168 = 1 week)",
                "sensitivity":           "float — 0-1, lower=more sensitive (default: 0.3)",
                "entities":              "list  — entity IDs to monitor (default: auto-discover)",
            },
            "output_schema": {
                "anomalies_detected":  "int",
                "baselines_ready":     "int",
                "detection_active":    "bool",
                "last_anomaly":        "dict|null",
            },
            "poll_interval": 3600,
            "code":          code,
        }
        logger.info("[catalog] Loaded anomaly-detector recipe")

    # ── manual-agent ───────────────────────────────────────────────────
    code = _load_recipe("manual_agent.py")
    if code:
        catalog["manual-agent"] = {
            "name":         "manual-agent",
            "type":         "dynamic",
            "description":  "Searches the internet for device manuals, downloads PDFs, extracts text, and answers questions using the agent's LLM.",
            "capabilities": ["web_search", "pdf_extraction", "qa_assistant", "device_manuals"],
            "install":      ["httpx", "pdfplumber", "duckduckgo_search"],
            "input_schema": {
                "action":   "str  — load_manual|ask|status|clear",
                "device":   "str  — The device model name or query (for load_manual)",
                "question": "str  — The question to ask about the loaded manual (for ask)",
            },
            "output_schema": {
                "success":  "bool — True if operation succeeded",
                "device":   "str  — Device model name",
                "url":      "str  — URL of the downloaded manual PDF",
                "pages":    "int  — Number of pages in the PDF",
                "chars":    "int  — Character count of extracted text",
                "preview":  "str  — Preview snippet of text",
                "answer":   "str  — LLM generated answer to your question",
            },
            "poll_interval": 3600, # Event-driven via direct actions/messages
            "code":          code,
        }
        logger.info("[catalog] Loaded manual-agent recipe")

    return catalog


# ──────────────────────────────────────────────────────────────────────────────
# CATALOG AGENT
# ──────────────────────────────────────────────────────────────────────────────

class CatalogAgent(Actor):
    """
    Pre-built agent recipe library.
    Spawns any catalog agent on request by delegating to main's spawn pipeline.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "catalog")
        super().__init__(**kwargs)
        self.protected = True
        self._catalog  = _build_catalog()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def on_start(self):
        names = list(self._catalog.keys())
        logger.info(f"[{self.name}] Catalog ready — {len(names)} recipe(s): {names}")
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log",
             "message": f"Catalog ready: {', '.join(names)}",
             "timestamp": time.time()},
        )

        await self.publish_manifest(
            description=(
                "Pre-built agent recipe library. "
                "Spawns ready-made agents by name without requiring code. "
                f"Available: {', '.join(names)}"
            ),
            capabilities=["spawn_catalog_agent", "list_catalog_agents", "agent_catalog"],
            input_schema={"action": "str — 'spawn' | 'list' | 'info'",
                          "agent":  "str — agent name for spawn/info actions"},
            output_schema={"ok": "bool", "message": "str",
                           "agents": "list", "recipe": "dict"},
        )

        # Inject recipe manifests directly into main's _agent_manifests dict
        main = None
        for _ in range(20):
            main = self._registry.find_by_name("main") if self._registry else None
            if main and hasattr(main, "_agent_manifests"):
                break
            await asyncio.sleep(0.5)

        for name, recipe in self._catalog.items():
            manifest = {
                "name":          name,
                "actor_id":      f"catalog.{name}",
                "description":   recipe.get("description", ""),
                "capabilities":  recipe.get("capabilities", []),
                "input_schema":  recipe.get("input_schema",  {}),
                "output_schema": recipe.get("output_schema", {}),
                "publishes":     [],
                "spawnable":     True,
                "catalog":       self.name,
                "timestamp":     time.time(),
            }

            if main and hasattr(main, "_agent_manifests"):
                main._agent_manifests[name] = manifest
                logger.info(f"[{self.name}] Injected manifest for '{name}' into main")
            else:
                logger.warning(f"[{self.name}] main not ready — could not inject manifest for '{name}'")

    def _current_task_description(self) -> str:
        return f"catalog ({len(self._catalog)} recipes)"

    # ── Message handling ───────────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        if msg.type != MessageType.TASK:
            return

        payload = msg.payload if msg.payload is not None else {}
        result  = await self._handle(payload)

        task_id = payload.get("task") or payload.get("_task_id") if isinstance(payload, dict) else None
        if task_id:
            result["task"]     = task_id
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
            text = (payload.get("text") or payload.get("message") or payload.get("query") or "").strip()
        else:
            text = ""

        if text:
            parts = text.split(None, 1)
            cmd   = parts[0].lower()
            arg   = parts[1].strip() if len(parts) > 1 else ""
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
        stripped = norm[:-6] if norm.endswith("-agent") else norm
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
            agents.append({
                "name":         name,
                "description":  recipe.get("description", ""),
                "capabilities": recipe.get("capabilities", []),
            })
        return {
            "ok":      True,
            "message": f"{len(agents)} agent(s) available in catalog",
            "agents":  agents,
        }

    def _action_info(self, name: str) -> dict:
        if not name:
            return {"ok": False, "message": "Provide 'agent' name for info action"}
        resolved = self._resolve_name(name)
        recipe = self._catalog.get(resolved) if resolved else None
        if not recipe:
            available = list(self._catalog.keys())
            return {"ok": False, "message": f"'{name}' not in catalog. Available: {available}"}
        safe = {k: v for k, v in recipe.items() if k != "code"}
        return {"ok": True, "message": f"Recipe for '{resolved}'", "recipe": safe}

    async def _action_spawn(self, name: str, payload: dict) -> dict:
        if not name:
            return {"ok": False, "message": "Provide 'agent' name to spawn"}

        resolved = self._resolve_name(name)
        recipe = self._catalog.get(resolved) if resolved else None
        if not recipe:
            available = list(self._catalog.keys())
            return {"ok": False, "message": f"'{name}' not in catalog. Available: {available}"}

        if not self._registry:
            return {"ok": False, "message": "No registry available — cannot spawn"}

        existing = self._registry.find_by_name(resolved)
        if existing:
            return {"ok": True, "message": f"'{resolved}' is already running"}

        logger.info(f"[{self.name}] Spawning '{resolved}'...")
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": f"Spawning '{resolved}'...", "timestamp": time.time()},
        )

        try:
            from .dynamic_agent import DynamicAgent

            install = recipe.get("install", [])
            if install:
                # Fast-path: check which packages are already importable.
                # Avoids a 120s installer wait when deps were installed in a
                # previous session — same logic as main._spawn_dynamic_agent.
                import importlib as _importlib
                # Map pip package names to their actual import names where they differ.
                _IMPORT_NAME_MAP = {
                    "scikit-learn":      "sklearn",
                    "stable-baselines3": "stable_baselines3",
                    "pillow":            "PIL",
                    "pyyaml":            "yaml",
                    "pymupdf":           "fitz",
                    "beautifulsoup4":    "bs4",
                    "python-dateutil":   "dateutil",
                    "typing-extensions": "typing_extensions",
                    "opencv-python":     "cv2",
                    "scikit-image":      "skimage",
                }
                needed = []
                for pkg in install:
                    pip_name    = pkg.split("[")[0].lower()
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
                        future  = asyncio.get_running_loop().create_future()
                        main = self._registry.find_by_name("main") if self._registry else None
                        if main:
                            main._result_futures[task_id] = future
                        # Send with reply_to=main.actor_id so the installer's RESULT goes
                        # directly to main where the future is registered.
                        install_msg = Message(
                            type      = MessageType.TASK,
                            sender_id = self.actor_id,
                            reply_to  = main.actor_id if main else self.actor_id,
                            payload   = {
                                "action":   "install",
                                "packages": needed,
                                "task":     task_id,
                                "_task_id": task_id,
                            },
                        )
                        await installer.receive(install_msg)
                        try:
                            await asyncio.wait_for(future, timeout=120.0)
                        except asyncio.TimeoutError:
                            logger.warning(f"[{self.name}] Install timeout for '{name}' — proceeding anyway")
                    else:
                        logger.warning(f"[{self.name}] installer not found — skipping dep install for '{name}'")
                else:
                    logger.info(f"[{self.name}] All deps for '{resolved}' already installed — skipping installer")

            main = self._registry.find_by_name("main")
            llm_provider    = getattr(main, "llm", None) if main else None
            persistence_dir = str(getattr(main, "_persistence_dir", "./state/main").parent) if main else "./state"

            actor = await self.spawn(
                DynamicAgent,
                name            = resolved,
                code            = recipe["code"],
                poll_interval   = float(recipe.get("poll_interval", 3600)),
                description     = recipe.get("description", ""),
                input_schema    = recipe.get("input_schema", {}),
                output_schema   = recipe.get("output_schema", {}),
                llm_provider    = llm_provider,
                persistence_dir = persistence_dir,
                trusted         = True,   # catalog agents are pre-built — skip safety validator
            )

            if actor:
                if main and hasattr(main, "_save_to_spawn_registry"):
                    # Mark as trusted so it bypasses safety validator on restore
                    save_config = dict(recipe)
                    save_config["trusted"] = True
                    main._save_to_spawn_registry(save_config)

                msg = f"'{resolved}' spawned and running"
                logger.info(f"[{self.name}] {msg}")
                await self._mqtt_publish(
                    f"agents/{self.actor_id}/logs",
                    {"type": "log", "message": msg, "timestamp": time.time()},
                )
                return {"ok": True, "message": msg, "agent": resolved}
            else:
                return {"ok": False, "message": f"Spawn returned no actor for '{resolved}'"}

        except Exception as e:
            msg = f"Failed to spawn '{resolved}': {e}"
            logger.error(f"[{self.name}] {msg}")
            return {"ok": False, "message": msg}

    # ── Public API ─────────────────────────────────────────────────────────────

    def list_recipes(self) -> list[str]:
        return list(self._catalog.keys())

    def get_recipe(self, name: str) -> Optional[dict]:
        return self._catalog.get(name)