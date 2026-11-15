# SPDX-License-Identifier: Apache-2.0
"""On Windows, aiohttp sends files by its own writes, never through ``loop.sendfile``.

The selector loop Wactorz runs on there has no native sendfile, and asyncio's
fallback sends wrong bytes to a reader slower than the disk: the dashboard's
bundle then fails to parse on some loads. The package's bootstrap turns it off
before aiohttp is imported.
"""

import os
import subprocess
import sys

import pytest

from wactorz._bootstrap import avoid_loop_sendfile


def test_it_asks_aiohttp_for_its_own_writes() -> None:
    environ: dict[str, str] = {}
    avoid_loop_sendfile(environ)
    assert environ == {"AIOHTTP_NOSENDFILE": "1"}


def test_an_operators_own_value_is_kept() -> None:
    environ = {"AIOHTTP_NOSENDFILE": ""}
    avoid_loop_sendfile(environ)
    assert environ == {"AIOHTTP_NOSENDFILE": ""}


@pytest.mark.skipif(
    sys.platform != "win32", reason="the selector loop's sendfile fallback is Windows-only"
)
def test_importing_wactorz_turns_it_off_before_aiohttp_reads_it() -> None:
    # A process of its own: this one imported aiohttp long ago.
    code = "import wactorz.cli, aiohttp.web_fileresponse as f; print(f.NOSENDFILE)"
    env = {k: v for k, v in os.environ.items() if k != "AIOHTTP_NOSENDFILE"}
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=True,
    )
    assert out.stdout.strip() == "True"
