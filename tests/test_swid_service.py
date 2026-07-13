"""Tests for SwidMinter — idempotent DID minting with encrypted key custody.

Kept small on purpose: every enabled mint pays a scrypt key-derivation
(~0.1-0.5 s), so the suite mints once and reuses it.
"""

# pylint: disable=missing-function-docstring

from pathlib import Path

import swid

from wactorz.core.swid.service import SwidMinter

PASSPHRASE = "test-passphrase"


async def test_mint_and_idempotency_and_resolution(tmp_path: Path) -> None:
    minter = SwidMinter(tmp_path, PASSPHRASE, "https://hstp.example")

    first = await minter.ensure_did("agent", "home", "actor-1", name="Main")
    assert first.created is True
    assert first.did is not None and first.did.startswith("did:swid:z")
    assert first.handle.startswith("swid:agent:home:main-")

    # Same inputs → same DID, no new mint (index skip path).
    again = await minter.ensure_did("agent", "home", "actor-1", name="Main")
    assert again.created is False
    assert again.did == first.did

    # The key is retrievable from the encrypted store.
    assert minter.keystore
    kp = minter.keystore.get(first.did)
    assert kp.public_key_multibase().startswith("z6Mk")

    # The DID resolves from the minter's registry and carries the handle.
    result = await swid.resolve_async(first.did, minter.registry)
    assert result.did_document is not None
    assert result.did_document["id"] == first.did
    assert result.did_document["alsoKnownAs"] == [first.handle]
    assert result.did_document["service"][0]["serviceEndpoint"] == (
        f"https://hstp.example/{first.handle}"
    )


async def test_index_survives_restart(tmp_path: Path) -> None:
    minter = SwidMinter(tmp_path, PASSPHRASE, "https://hstp.example")
    first = await minter.ensure_did("device", "home", "dev-1", name="Lamp")

    fresh = SwidMinter(tmp_path, PASSPHRASE, "https://hstp.example")
    again = await fresh.ensure_did("device", "home", "dev-1", name="Lamp")
    assert again.created is False
    assert again.did == first.did


async def test_disabled_minter_returns_handle_only(tmp_path: Path) -> None:
    data_dir = tmp_path / "swid"
    minter = SwidMinter(data_dir, "", "https://hstp.example")
    assert minter.enabled is False

    res = await minter.ensure_did("device", "home", "dev-1", name="Lamp")
    assert res.did is None
    assert res.created is False
    assert res.handle.startswith("swid:device:home:lamp-")
    # Disabled mode never touches disk.
    assert not data_dir.exists()
