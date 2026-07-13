"""Tests for the Spatial Web identity linkage in the Fuseki graph builders.

Covers the pure RDF body builders (did/handle triples present when minted,
absent otherwise) and the bridge's best-effort ``_mint`` helper with a stub
minter — enabled, disabled (did=None), and failing. No network involved.
"""

# pylint: disable=missing-function-docstring,protected-access

from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web

import wactorz.monitor_server as ms
from wactorz.core import contract
from wactorz.ext.fuseki import TTL_PREFIXES, HAFusekiBridge, area_body, device_body

DID = "did:swid:zQmcn8EtYXq3CETZxfom5FJzHJYy2BBchWMGbAB5NnvyKpX"
HANDLE = "swid:device:home:kitchen-light-ab12cd34"

AREA = {"area_id": "kitchen", "name": "Kitchen"}
STATE = {
    "entity_id": "light.kitchen",
    "state": "on",
    "attributes": {"friendly_name": "Kitchen Light"},
}


class _StubMinter:
    """ensure_did stub returning a fixed MintResult-shaped object."""

    def __init__(self, did: str | None = DID, fail: bool = False):
        self._did = did
        self._fail = fail
        self.calls: list[tuple[str, str, str]] = []

    # pylint: disable=unused-argument
    async def ensure_did(
        self,
        entity_class,
        namespace,
        natural_key,
        *,
        name=None,
    ):  # pyright: ignore[reportUnusedParameter]
        self.calls.append((entity_class, namespace, natural_key))
        if self._fail:
            raise RuntimeError("keystore unavailable")

        class _Res:
            did = self._did
            handle = HANDLE

        return _Res()


def _bridge(minter=None) -> HAFusekiBridge:
    return HAFusekiBridge(
        ha_url="http://ha.local:8123",
        ha_token="token",
        fuseki_url="http://fuseki:3030",
        fuseki_dataset="wactorz",
        swid_minter=minter,
        swid_namespace="home",
    )


def test_swidns_prefix_declared():
    assert "@prefix swidns:" in TTL_PREFIXES


def test_area_body_includes_did_and_handle_when_given():
    body = area_body(AREA, did=DID, handle=HANDLE)
    assert f'swidns:did "{DID}"' in body
    assert f'swidns:handle "{HANDLE}"' in body


def test_area_body_omits_identity_when_not_minted():
    body = area_body(AREA)
    assert "swidns:" not in body


def test_device_body_includes_did_and_handle_when_given():
    body = device_body("light.kitchen", STATE, did=DID, handle=HANDLE)
    assert f'swidns:did "{DID}"' in body
    assert f'swidns:handle "{HANDLE}"' in body


def test_device_body_omits_identity_when_not_minted():
    body = device_body("light.kitchen", STATE)
    assert "swidns:" not in body


async def test_bridge_mint_returns_did_and_handle():
    minter = _StubMinter()
    did, handle = await _bridge(minter)._mint("device", "light.kitchen", "Kitchen Light")
    assert (did, handle) == (DID, HANDLE)
    assert minter.calls == [("device", "home", "light.kitchen")]


async def test_bridge_mint_without_minter_is_noop():
    assert await _bridge(None)._mint("device", "light.kitchen", None) == (None, None)


async def test_bridge_mint_survives_minter_failure():
    # A broken keystore must not take the graph seed down with it.
    assert await _bridge(_StubMinter(fail=True))._mint("space", "kitchen", "Kitchen") == (
        None,
        None,
    )


async def test_bridge_mint_disabled_minter_passes_none_did_through():
    did, handle = await _bridge(_StubMinter(did=None))._mint("space", "kitchen", "Kitchen")
    assert did is None
    assert handle == HANDLE  # handle still flows (label-only mode)


# ── agent DID → graph linkage hook (monitor_server) ──────────────────────────

_FUSEKI_CFG = SimpleNamespace(
    fuseki_url="http://fuseki:3030",
    fuseki_dataset="wactorz",
    fuseki_user="admin",
    fuseki_password="admin",
)


async def test_link_hook_links_only_minted_agent_dids(monkeypatch):
    app = web.Application()
    app[contract.AGENT_IDENTITY] = {
        "main": SimpleNamespace(did=DID, handle="swid:agent:home:main-aa11"),
        "handles-only": SimpleNamespace(did=None, handle="swid:agent:home:x-bb22"),
    }
    monkeypatch.setattr("wactorz.config.CONFIG", _FUSEKI_CFG)
    fake = AsyncMock()
    monkeypatch.setattr("wactorz.ext.fuseki.bridge.link_agent_dids", fake)

    await ms._link_agent_dids_to_graph(app)

    fake.assert_awaited_once()
    links = fake.await_args.args[0]  # pyright: ignore[reportOptionalMemberAccess]
    # did=None agent is excluded; only the minted one is linked.
    assert links == {"main": (DID, "swid:agent:home:main-aa11")}


async def test_link_hook_noop_without_fuseki(monkeypatch):
    app = web.Application()
    app[contract.AGENT_IDENTITY] = {"main": SimpleNamespace(did=DID, handle="h")}
    monkeypatch.setattr("wactorz.config.CONFIG", SimpleNamespace(fuseki_url=""))
    fake = AsyncMock()
    monkeypatch.setattr("wactorz.ext.fuseki.bridge.link_agent_dids", fake)

    await ms._link_agent_dids_to_graph(app)

    fake.assert_not_awaited()


async def test_link_hook_noop_when_no_dids_minted(monkeypatch):
    app = web.Application()
    app[contract.AGENT_IDENTITY] = {"x": SimpleNamespace(did=None, handle="h")}
    monkeypatch.setattr("wactorz.config.CONFIG", _FUSEKI_CFG)
    fake = AsyncMock()
    monkeypatch.setattr("wactorz.ext.fuseki.bridge.link_agent_dids", fake)

    await ms._link_agent_dids_to_graph(app)

    fake.assert_not_awaited()
