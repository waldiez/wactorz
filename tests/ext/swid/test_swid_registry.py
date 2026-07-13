"""Tests for FileSWIDRegistry — file-backed AsyncSWIDRegistry.

Mirrors the library's AsyncInMemoryRegistry contract: ValueError on empty log /
duplicate register, KeyError for unknown DIDs, and (the point of this
implementation) persistence across instances over the same directory.
"""

# pylint: disable=missing-function-docstring
from pathlib import Path

import pytest
import swid

from wactorz.ext.swid import FileSWIDRegistry


def _genesis():
    res = swid.create(hstp_endpoint="https://hstp.example/e/test")
    return res.did, res.cel_genesis


async def test_register_and_get_log_roundtrip(tmp_path: Path) -> None:
    reg = FileSWIDRegistry(tmp_path)
    did, genesis = _genesis()
    assert await reg.register([genesis]) == did
    assert await reg.get_log(did) == [genesis]


async def test_register_empty_log_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        await FileSWIDRegistry(tmp_path).register([])


async def test_register_duplicate_raises(tmp_path: Path) -> None:
    reg = FileSWIDRegistry(tmp_path)
    _, genesis = _genesis()
    await reg.register([genesis])
    with pytest.raises(ValueError, match="already registered"):
        await reg.register([genesis])


async def test_unknown_did_raises_keyerror(tmp_path: Path) -> None:
    reg = FileSWIDRegistry(tmp_path)
    with pytest.raises(KeyError):
        await reg.get_log("did:swid:zQmcn8EtYXq3CETZxfom5FJzHJYy2BBchWMGbAB5NnvyKpX")
    with pytest.raises(KeyError):
        await reg.append("did:swid:zQmcn8EtYXq3CETZxfom5FJzHJYy2BBchWMGbAB5NnvyKpX", {})


async def test_append_then_get_log_shows_event(tmp_path: Path) -> None:
    reg = FileSWIDRegistry(tmp_path)
    did, genesis = _genesis()
    await reg.register([genesis])
    event = {"type": "test-event"}
    await reg.append(did, event)
    assert await reg.get_log(did) == [genesis, event]


async def test_persists_across_instances(tmp_path: Path) -> None:
    did, genesis = _genesis()
    await FileSWIDRegistry(tmp_path).register([genesis])
    # A brand-new instance over the same directory still has the log.
    fresh = FileSWIDRegistry(tmp_path)
    assert await fresh.get_log(did) == [genesis]
    await fresh.close()


async def test_registered_did_resolves(tmp_path: Path) -> None:
    reg = FileSWIDRegistry(tmp_path)
    did, genesis = _genesis()
    await reg.register([genesis])
    result = await swid.resolve_async(did, reg)
    assert result.did_document is not None
    assert result.did_document["id"] == did
