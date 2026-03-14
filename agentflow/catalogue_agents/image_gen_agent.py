"""
DEMO AGENT — image-gen-agent
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Generates images from text prompts using NVIDIA NIM FLUX.1-dev.
Saves a PNG to disk and returns its path.

This is a DEMO agent — do NOT modify the core framework files when refurnishing.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SETUP
─────
Store your NIM API key once before spawning:

    @main remember nim_api_key = nvapi-xxxxxxxxxxxxxxxx

Or pass it per-request in the payload as "api_key".
Get a free key (1000 credits) at: https://build.nvidia.com


SPAWN CONFIG
────────────
Paste this when spawning from main or planner:

{
  "name":        "image-gen-agent",
  "type":        "dynamic",
  "description": "Generates images from text prompts using NVIDIA NIM FLUX.1-dev. Returns absolute PNG path.",
  "capabilities": ["image_generation", "text_to_image", "nvidia_nim", "flux"],
  "input_schema": {
    "prompt":      "str  — what to generate, e.g. 'futuristic city at sunset'",
    "output_path": "str  — where to save the PNG, e.g. /tmp/slide_0.png",
    "width":       "int  — pixels wide, default 1024",
    "height":      "int  — pixels tall, default 576  (16:9 for slides)",
    "steps":       "int  — inference steps 1-50, default 20",
    "api_key":     "str  — optional, overrides persisted nim_api_key"
  },
  "output_schema": {
    "image_path": "str       — absolute path to the saved PNG, or null on failure",
    "width":      "int       — actual image width",
    "height":     "int       — actual image height",
    "size_kb":    "int       — file size in KB",
    "error":      "str|null  — error message if generation failed"
  },
  "poll_interval": 3600,
  "code": "<copy AGENT_CODE string from the bottom of this file>"
}


TASK PAYLOAD EXAMPLES
──────────────────────
Minimal:
  { "prompt": "a calm mountain lake at dawn", "output_path": "/tmp/slide_0.png" }

Full:
  {
    "prompt":      "minimalist flat icon of renewable energy, white background",
    "output_path": "/tmp/slide_3.png",
    "width":       1024,
    "height":      576,
    "steps":       25
  }
"""

# ──────────────────────────────────────────────────────────────────────────────
# AGENT_CODE — copy this string into the "code" field of the spawn config
# ──────────────────────────────────────────────────────────────────────────────

AGENT_CODE = r'''
import asyncio
import base64
import os
import time


async def setup(agent):
    try:
        import requests  # noqa
        await agent.log("image-gen-agent ready — NVIDIA NIM FLUX.1-dev")
    except ImportError:
        await agent.alert("Missing package: requests — pip install requests", "error")


async def handle_task(agent, payload):
    import requests

    prompt      = payload.get("prompt", "").strip()
    output_path = payload.get("output_path", f"/tmp/nim_img_{int(time.time())}.png")
    width       = int(payload.get("width",  1024))
    height      = int(payload.get("height",  576))   # 16:9 — ideal for slides
    steps       = int(payload.get("steps",   20))

    if not prompt:
        return {"image_path": None, "width": width, "height": height,
                "size_kb": 0, "error": "No prompt provided"}

    # Resolve API key: payload arg → persisted store
    api_key = payload.get("api_key") or agent.recall("nim_api_key")
    if not api_key:
        return {
            "image_path": None, "width": width, "height": height, "size_kb": 0,
            "error": (
                "No NIM API key. "
                "Store it with: @main remember nim_api_key = nvapi-..."
            )
        }

    await agent.log(f"Generating: \"{prompt[:80]}{'...' if len(prompt) > 80 else ''}\"")

    url = "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    body = {
        "prompt":              prompt,
        "width":               width,
        "height":              height,
        "num_inference_steps": steps,
        "guidance_scale":      3.5,
        "seed":                int(time.time()) % 99999,
    }

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        artifacts = data.get("artifacts") or []
        if not artifacts:
            return {"image_path": None, "width": width, "height": height,
                    "size_kb": 0, "error": f"Empty artifacts in response: {data}"}

        img_b64 = artifacts[0].get("base64") or artifacts[0].get("image")
        if not img_b64:
            return {"image_path": None, "width": width, "height": height,
                    "size_kb": 0, "error": f"No base64 field in artifact: {artifacts[0]}"}

        # Ensure output directory exists
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        img_bytes = base64.b64decode(img_b64)
        with open(output_path, "wb") as f:
            f.write(img_bytes)

        size_kb = len(img_bytes) // 1024
        await agent.log(f"Saved: {output_path} ({size_kb} KB)")
        return {
            "image_path": output_path,
            "width":      width,
            "height":     height,
            "size_kb":    size_kb,
            "error":      None,
        }

    except requests.exceptions.HTTPError as e:
        msg = f"NIM HTTP {e.response.status_code}: {e.response.text[:300]}"
        await agent.alert(msg, "error")
        return {"image_path": None, "width": width, "height": height, "size_kb": 0, "error": msg}

    except Exception as e:
        msg = f"Image generation failed: {e}"
        await agent.alert(msg, "error")
        return {"image_path": None, "width": width, "height": height, "size_kb": 0, "error": msg}


async def process(agent):
    # Task-driven only — no polling loop needed
    await asyncio.sleep(3600)
'''
