"""Tests for the did:swid resolver routes (DIF DID Resolution binding).

Mint through a SwidMinter over a tmp dir, then resolve over HTTP: 200 with the
document for a registered DID, 400 for a malformed DID, 404 for a well-formed
but unknown one.
"""

# pylint: disable=missing-function-docstring,import-outside-toplevel

from pathlib import Path
from typing import TYPE_CHECKING

from wactorz.ext.swid import FileSWIDRegistry, SwidMinter, swid_routes

if TYPE_CHECKING:
    from aiohttp.test_utils import TestClient
    from aiohttp.web import Application, Request

PASSPHRASE = "test-passphrase"


async def _client(tmp_path: Path) -> tuple["TestClient[Request, Application]", str | None, str]:
    """App over tmp_path with one minted agent DID; returns (client, did, handle)."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    minter = SwidMinter(tmp_path, PASSPHRASE, "https://hstp.example")
    res = await minter.ensure_did("agent", "home", "actor-1", name="Main")
    app = web.Application()
    app.add_routes(swid_routes(FileSWIDRegistry(tmp_path), minter))
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, res.did, res.handle


async def test_resolves_minted_did(tmp_path: Path) -> None:
    client, did, handle = await _client(tmp_path)
    try:
        resp = await client.get(f"/1.0/identifiers/{did}")
        assert resp.status == 200
        body = await resp.json()
        assert body["didDocument"]["id"] == did
        assert body["didDocument"]["alsoKnownAs"] == [handle]
        assert body["didResolutionMetadata"].get("error") is None
    finally:
        await client.close()


async def test_malformed_did_is_400(tmp_path: Path) -> None:
    client, _, _ = await _client(tmp_path)
    try:
        resp = await client.get("/1.0/identifiers/did:web:example.com")
        assert resp.status == 400
        body = await resp.json()
        assert body["didResolutionMetadata"]["error"] == "invalidDid"
        assert body["didDocument"] is None
    finally:
        await client.close()


async def test_unknown_did_is_404(tmp_path: Path) -> None:
    client, _, _ = await _client(tmp_path)
    try:
        ghost = "did:swid:zQmcn8EtYXq3CETZxfom5FJzHJYy2BBchWMGbAB5NnvyKpX"
        resp = await client.get(f"/1.0/identifiers/{ghost}")
        assert resp.status == 404
        body = await resp.json()
        assert body["didResolutionMetadata"]["error"] == "notFound"
    finally:
        await client.close()


async def test_identities_endpoint_lists_minted(tmp_path: Path) -> None:
    client, did, handle = await _client(tmp_path)
    try:
        resp = await client.get("/api/swid/identities")
        assert resp.status == 200
        body = await resp.json()
        assert body["enabled"] is True
        assert body["identities"] == [{"handle": handle, "did": did, "entityClass": "agent"}]
    finally:
        await client.close()


async def test_identities_reconciles_late_spawned_agent(tmp_path: Path) -> None:
    # An agent spawned after startup (not in AGENT_IDENTITY) is minted on read,
    # so the Identity view shows it on refresh without a restart.
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from wactorz.ext import contract

    minter = SwidMinter(tmp_path, PASSPHRASE, "https://hstp.example")
    app = web.Application()
    app.add_routes(swid_routes(FileSWIDRegistry(tmp_path), minter))
    actor = SimpleNamespace(actor_id="weather", name="Weather Agent")
    app[contract.ACTOR_REGISTRY] = SimpleNamespace(all_actors=lambda: [actor])
    app[contract.AGENT_IDENTITY] = {}
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/api/swid/identities")
        assert resp.status == 200
        body = await resp.json()
        handles = [i["handle"] for i in body["identities"]]
        assert any(h.startswith("swid:agent:home:weather-") for h in handles)
        # cached for the /api/actors card too
        assert "weather" in app[contract.AGENT_IDENTITY]
    finally:
        await client.close()
