# SPDX-License-Identifier: Apache-2.0
"""Wactorz used the way a library user's script uses it, in an interpreter of its own.

The rest of this suite builds main inside the test process, whose imports are
whatever pytest loaded first. A user's script starts from ``import wactorz``
alone, so this runs one in a fresh interpreter: declare an agent, start the
system with :func:`wactorz.serve` beside the script's own code, ask the agent
with :func:`wactorz.ask`, and stop. Any import the package makes on the way, in
whatever order, is part of what is tested.
"""

import os
import subprocess
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path

#: Generous: the script connects to a real broker and waits for its agent.
SCRIPT_TIMEOUT_S = 120

#: What a library user writes. The broker, the state directory and the agent's
#: name come in on the command line.
_SCRIPT = """
import asyncio
import contextlib
import sys

import wactorz

HOST, PORT, STATE, NAME = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]


@wactorz.agent(name=NAME)
def doubler(payload):
    return {"twice": payload["n"] * 2}


async def main():
    system = asyncio.create_task(
        wactorz.serve(
            [doubler], minimal=True, web=False, mqtt_broker=HOST, mqtt_port=PORT, state_dir=STATE
        )
    )
    try:
        for _ in range(1200):
            running = wactorz.system()
            if running is not None and running.registry.find_by_name(NAME) is not None:
                break
            if system.done():
                system.result()
            await asyncio.sleep(0.05)
        print(await wactorz.ask(NAME, {"n": 21}, timeout=30))
    finally:
        system.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await system


asyncio.run(main())
"""


def test_a_script_serves_an_agent_and_asks_it(broker: tuple[str, int], tmp_path: Path) -> None:
    host, port = broker
    # Names of their own: the broker is shared with the rest of the suite and
    # outlives the test.
    suffix = uuid.uuid4().hex[:8]
    script = tmp_path / "script.py"
    script.write_text(_SCRIPT, encoding="utf-8")
    env = _without_wactorz_settings(os.environ)
    env.update({"WACTORZ_NAMESPACE": f"lib-{suffix}", "PYTHONIOENCODING": "utf-8"})
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::RuntimeWarning",
            str(script),
            host,
            str(port),
            str(tmp_path / "state"),
            f"doubler-{suffix}",
        ],
        capture_output=True,
        text=True,
        timeout=SCRIPT_TIMEOUT_S,
        cwd=tmp_path,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-3000:]
    assert "'twice': 42" in result.stdout, result.stdout[-2000:]


def _without_wactorz_settings(environ: Mapping[str, str]) -> dict[str, str]:
    """The test's environment less what configures Wactorz, so the script sets its own."""
    return {
        key: value
        for key, value in environ.items()
        if not key.startswith(("WACTORZ_", "MQTT_", "DEPLOY_", "LLM_"))
    }
