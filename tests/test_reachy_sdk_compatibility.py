"""Exercise the optional SDK client against the audited HTTP dependencies.

Run with the Reachy extra installed. A fresh process avoids SDK stubs that the
hardware-free catalogue tests install and checks the actual package imports.
"""

import importlib.metadata
import subprocess
import sys

import pytest

CLIENT_CHECK = """
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from reachy_mini import ReachyMini
from reachy_mini.daemon.app.middleware import MaxBodySizeMiddleware
from reachy_mini.utils import create_head_pose

assert ReachyMini.__module__ == "reachy_mini.reachy_mini"
assert create_head_pose(yaw=15).shape == (4, 4)

app = FastAPI()
app.add_middleware(MaxBodySizeMiddleware, max_body_size=32, paths=("/upload",))

@app.post("/upload")
async def upload(request: Request):
    return {"size": len(await request.body())}

with TestClient(app) as client:
    response = client.post("/upload", content=b"hello")
    assert response.status_code == 200
    assert response.json() == {"size": 5}
    assert client.post("/upload", content=b"x" * 33).status_code == 413
"""


def test_real_reachy_client_and_http_middleware_with_patched_starlette() -> None:
    try:
        importlib.metadata.version("reachy-mini")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("Reachy SDK compatibility requires the opt-in reachy extra")

    result = subprocess.run(
        [sys.executable, "-c", CLIENT_CHECK],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
