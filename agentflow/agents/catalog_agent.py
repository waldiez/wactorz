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
  @catalog spawn doc-to-pptx-agent
  @catalog list
  @catalog info image-gen-agent

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
# Load AGENT_CODE strings from demo_agent files.
# If a file is missing the recipe is simply excluded — no crash.
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
# Each entry is a full spawn config dict — exactly what main._handle_spawn()
# expects, minus the "code" field which is injected at load time from the file.
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

    # ── ADD NEW RECIPES HERE ──────────────────────────────────────────────────
    # Pattern:
    #   code = _load_recipe("my_new_agent.py")
    #   if code:
    #       catalog["my-new-agent"] = { ...spawn config..., "code": code }
    # ─────────────────────────────────────────────────────────────────────────

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
            capabilities=[
                "spawn_catalog_agent",
                "list_catalog_agents",
                "agent_catalog",
            ],
            input_schema={
                "action": "str — 'spawn' | 'list' | 'info'",
                "agent":  "str — agent name for spawn/info actions",
            },
            output_schema={
                "ok":        "bool",
                "message":   "str",
                "agents":    "list — for 'list' action",
                "recipe":    "dict — for 'info' action (without code)",
            },
        )

    def _current_task_description(self) -> str:
        return f"catalog ({len(self._catalog)} recipes)"

    # ── Message handling ───────────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        if msg.type != MessageType.TASK:
            return

        payload = msg.payload if msg.payload is not None else {}
        result  = await self._handle(payload)

        # Echo task_id so caller futures resolve
        task_id = payload.get("task") or payload.get("_task_id") if isinstance(payload, dict) else None
        if task_id:
            result["task"]     = task_id
            result["_task_id"] = task_id

        target = msg.reply_to or msg.sender_id
        if target:
            await self.send(target, MessageType.RESULT, result)

    async def _handle(self, payload) -> dict:
        # Normalise to text first, then parse.
        # Payloads arrive in three forms:
        #   "spawn doc-to-pptx-agent"           ← raw string
        #   {"text": "spawn doc-to-pptx-agent"} ← delegate_task() wrapping
        #   {"action": "spawn", "agent": "..."}  ← structured dict

        # ── Structured dict with explicit action key ───────────────────────
        if isinstance(payload, dict) and payload.get("action"):
            action = payload["action"].lower().strip()
            if action == "list":
                return self._action_list()
            if action == "info":
                return self._action_info(payload.get("agent", ""))
            if action == "spawn":
                return await self._action_spawn(payload.get("agent", ""), payload)
            return {"ok": False, "message": f"Unknown action '{action}'. Use: spawn | list | info"}

        # ── Convenience dict shortcuts ─────────────────────────────────────
        if isinstance(payload, dict) and "spawn" in payload and isinstance(payload["spawn"], str):
            return await self._action_spawn(payload["spawn"], payload)

        # ── Extract text from any remaining form ───────────────────────────
        if isinstance(payload, str):
            text = payload.strip()
        elif isinstance(payload, dict):
            text = (payload.get("text") or payload.get("message") or payload.get("query") or "").strip()
        else:
            text = ""

        # ── Parse "verb agent-name" ────────────────────────────────────────
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
            # Bare agent name with no verb → treat as spawn
            if cmd in self._catalog:
                return await self._action_spawn(cmd, {})

        # ── Nothing parseable → helpful default ───────────────────────────
        return self._action_list()

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
        recipe = self._catalog.get(name)
        if not recipe:
            available = list(self._catalog.keys())
            return {"ok": False, "message": f"'{name}' not in catalog. Available: {available}"}
        # Return recipe without the full code string (too large for a response)
        safe = {k: v for k, v in recipe.items() if k != "code"}
        return {"ok": True, "message": f"Recipe for '{name}'", "recipe": safe}

    async def _action_spawn(self, name: str, payload: dict) -> dict:
        if not name:
            return {"ok": False, "message": "Provide 'agent' name to spawn"}

        recipe = self._catalog.get(name)
        if not recipe:
            available = list(self._catalog.keys())
            return {"ok": False, "message": f"'{name}' not in catalog. Available: {available}"}

        if not self._registry:
            return {"ok": False, "message": "No registry available — cannot spawn"}

        # If already running, return success immediately
        existing = self._registry.find_by_name(name)
        if existing:
            return {"ok": True, "message": f"'{name}' is already running"}

        logger.info(f"[{self.name}] Spawning '{name}'...")
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": f"Spawning '{name}'...", "timestamp": time.time()},
        )

        try:
            from .dynamic_agent import DynamicAgent

            # Find main to get its llm_provider and persistence_dir
            main = self._registry.find_by_name("main")
            llm_provider    = getattr(main, "llm", None) if main else None
            persistence_dir = str(getattr(main, "_persistence_dir", "./state/main").parent) if main else "./state"

            actor = await self.spawn(
                DynamicAgent,
                name            = name,
                code            = recipe["code"],
                poll_interval   = float(recipe.get("poll_interval", 3600)),
                description     = recipe.get("description", ""),
                input_schema    = recipe.get("input_schema", {}),
                output_schema   = recipe.get("output_schema", {}),
                llm_provider    = llm_provider,
                persistence_dir = persistence_dir,
            )

            if actor:
                # Save to main's spawn registry so it survives restarts
                if main and hasattr(main, "_save_to_spawn_registry"):
                    main._save_to_spawn_registry(recipe)

                msg = f"'{name}' spawned and running"
                logger.info(f"[{self.name}] {msg}")
                await self._mqtt_publish(
                    f"agents/{self.actor_id}/logs",
                    {"type": "log", "message": msg, "timestamp": time.time()},
                )
                return {"ok": True, "message": msg, "agent": name}
            else:
                return {"ok": False, "message": f"Spawn returned no actor for '{name}'"}

        except Exception as e:
            msg = f"Failed to spawn '{name}': {e}"
            logger.error(f"[{self.name}] {msg}")
            return {"ok": False, "message": msg}

    # ── Public API for other agents ────────────────────────────────────────────

    def list_recipes(self) -> list[str]:
        """Return names of all available recipes."""
        return list(self._catalog.keys())

    def get_recipe(self, name: str) -> Optional[dict]:
        """Return full recipe dict (including code) or None."""
        return self._catalog.get(name)