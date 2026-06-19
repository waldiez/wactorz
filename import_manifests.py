"""
import_manifests.py — one-shot: read retained agent manifests from MQTT and
write them to Fuseki, printing every step to THIS console.

Run it from the same machine/venv where you normally run `python -m wactorz.fuseki`:

    python import_manifests.py

It does the AgentManifestBridge's job once, by hand, so you can SEE what happens:
  * if MQTT won't connect -> the error prints here (not in a container log)
  * if no retained manifests arrive -> it says "0 manifests" (broker/topic issue)
  * if Fuseki write fails -> the error prints here
  * if it works -> your capabilities are now in urn:wactorz:agents

Env vars (same defaults as wactorz.fuseki):
    MQTT_BROKER     (default: localhost)
    MQTT_PORT       (default: 1883)
    FUSEKI_URL      (default: http://localhost:3030)
    FUSEKI_DATASET  (default: wactorz)
    FUSEKI_USER     (default: admin)
    FUSEKI_PASSWORD (default: empty)
    COLLECT_SECONDS (default: 5)  how long to wait for retained messages
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import aiohttp
import aiomqtt

# Reuse the EXACT serialization + write path the bridge uses.
try:
    from wactorz.fuseki import (
        FusekiClient,
        GRAPH_AGENTS,
        _agent_manifest_body,
        _ttl,
    )
except Exception as exc:  # pragma: no cover
    print(f"!! Could not import wactorz.fuseki ({exc}).")
    print("   Run this from your project root / the venv where wactorz is installed.")
    sys.exit(1)


MQTT_BROKER     = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT       = int(os.environ.get("MQTT_PORT", "1883"))
FUSEKI_URL      = os.environ.get("FUSEKI_URL", "http://localhost:3030")
FUSEKI_DATASET  = os.environ.get("FUSEKI_DATASET", "wactorz")
FUSEKI_USER     = os.environ.get("FUSEKI_USER", "admin")
FUSEKI_PASSWORD = os.environ.get("FUSEKI_PASSWORD", "")
COLLECT_SECONDS = float(os.environ.get("COLLECT_SECONDS", "5"))


async def _collect(client: aiomqtt.Client, out: dict) -> None:
    """Drain retained messages into `out` keyed by actor_id (or name)."""
    async for message in client.messages:
        try:
            manifest = json.loads(bytes(message.payload))
            key = str(manifest.get("actor_id") or manifest.get("name") or "?")
            out[key] = manifest
            caps = manifest.get("capabilities") or []
            print(f"   <- {message.topic}  key={key}  capabilities={caps}")
        except Exception as exc:
            print(f"   !! bad manifest on {message.topic}: {exc}")


async def main() -> None:
    print(f"MQTT   : {MQTT_BROKER}:{MQTT_PORT}")
    print(f"Fuseki : {FUSEKI_URL}/{FUSEKI_DATASET}  (user={FUSEKI_USER or 'none'})")
    print("-" * 60)

    # ── 1. MQTT: subscribe and collect retained manifests ────────────────────
    manifests: dict = {}
    print(f"Connecting to MQTT and listening {COLLECT_SECONDS:.0f}s for retained manifests ...")
    try:
        async with aiomqtt.Client(MQTT_BROKER, MQTT_PORT) as client:
            await client.subscribe("agents/+/manifest")
            try:
                await asyncio.wait_for(_collect(client, manifests), timeout=COLLECT_SECONDS)
            except asyncio.TimeoutError:
                pass  # expected — we stop after COLLECT_SECONDS
    except Exception as exc:
        print(f"!! MQTT connection failed: {type(exc).__name__}: {exc}")
        print("   => The bridge can't reach the broker at this address either.")
        print("      If wactorz runs in a container, 'localhost' points at the")
        print("      container, not the broker — try the broker's service name")
        print("      or host.docker.internal, and set MQTT_BROKER accordingly.")
        return

    print(f"-> collected {len(manifests)} manifest(s)")
    if not manifests:
        print("   => 0 retained manifests seen. Either nothing is retained on THIS")
        print("      broker, or the bridge connects to a different broker than the")
        print("      one your manifests live on. (Confirm with mosquitto_sub against")
        print("      the SAME host:port shown above.)")
        return

    # ── 2. Fuseki: write each manifest the same way the bridge does ───────────
    auth = aiohttp.BasicAuth(FUSEKI_USER, FUSEKI_PASSWORD) if FUSEKI_USER else None
    connector = aiohttp.TCPConnector(ssl=False, force_close=True)
    written = 0
    print("-" * 60)
    print("Writing to Fuseki ...")
    try:
        async with aiohttp.ClientSession(connector=connector) as http:
            fuseki = FusekiClient(FUSEKI_URL, FUSEKI_DATASET, http, auth)
            for key, manifest in manifests.items():
                actor_id = str(manifest.get("actor_id") or manifest.get("name") or key)
                try:
                    body = _agent_manifest_body(manifest)
                    await fuseki.replace_agent_in_graph(GRAPH_AGENTS, actor_id, _ttl(body))
                    written += 1
                    print(f"   ok  {actor_id}")
                except Exception as exc:
                    print(f"   !! {actor_id}: {type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"!! Fuseki session failed: {type(exc).__name__}: {exc}")
        return

    print("-" * 60)
    print(f"Done. Wrote {written}/{len(manifests)} agents into <urn:wactorz:agents>.")
    print("Verify:")
    print("  PREFIX syn: <https://synapse.waldiez.io/ns#>")
    print("  SELECT ?agent ?cap WHERE {")
    print("    GRAPH <urn:wactorz:agents> { ?cap a syn:Action ; syn:claimedBy ?agent . }")
    print("  } LIMIT 200")
    print()
    print("NOTE: if your app re-runs seed_agent_registry (full-graph replace) after")
    print("this, it will wipe these writes again. Run this AFTER the app has seeded,")
    print("or switch the registry seed to a per-agent merge.")


if __name__ == "__main__":
    # On Windows the default Proactor loop doesn't implement add_reader/
    # add_writer, which aiomqtt (paho) needs — you'd get NotImplementedError
    # and a bogus "timed out". Use SelectorEventLoop, exactly like the app's
    # _cli_main() does.
    if sys.platform == "win32":
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
    else:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()