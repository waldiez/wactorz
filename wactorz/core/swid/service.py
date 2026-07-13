"""SwidMinter — mints real ``did:swid`` identities for wactorz entities.

Custody is monitor-side: the minter holds each entity's Ed25519 key in an
encrypted file keystore (scrypt + AES-256-GCM) on behalf of devices/spaces/
agents, registers the genesis CEL in the file registry, and remembers the
handle→DID mapping in a small JSON index so minting is idempotent.

With an empty passphrase the minter is *disabled*: ``ensure_did`` still returns
the readable handle (so labels keep working) but ``did=None`` and nothing
touches disk. Resolution of previously minted DIDs is unaffected.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import swid as swid_lib
from swid.keystore import EncryptedFileKeyStore
from swid.keystore.util import atomic_write

from ..handles import make_handle
from .registry import FileSWIDRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MintResult:
    """Outcome of ``ensure_did``: the handle always; the DID when minting is on."""

    did: str | None
    handle: str
    created: bool


class SwidMinter:  # pylint: disable=too-many-instance-attributes
    """Idempotent DID minting keyed by readable handle."""

    def __init__(self, data_dir: Path, passphrase: str, hstp_base: str) -> None:
        self._data_dir = Path(data_dir)
        self._hstp_base = hstp_base.rstrip("/")
        self.enabled = bool(passphrase)
        self._keystore: EncryptedFileKeyStore | None = None
        if self.enabled:
            # EncryptedFileKeyStore raises ValueError on an empty passphrase, so
            # it is only constructed when minting is enabled.
            self._keystore = EncryptedFileKeyStore(self._data_dir / "keys", passphrase=passphrase)
        # Lazy: a disabled minter must not create directories or touch disk.
        self._registry_instance: FileSWIDRegistry | None = None
        self._index_path = self._data_dir / "index.json"
        self._index: dict[str, str] | None = None  # lazy-loaded {handle: did}
        self._lock = asyncio.Lock()
        self._warned_disabled = False

    @property
    def registry(self) -> FileSWIDRegistry:
        """The CEL registry the minter writes to (serve resolution from it)."""
        if self._registry_instance is None:
            self._registry_instance = FileSWIDRegistry(self._data_dir)
        return self._registry_instance

    @property
    def keystore(self) -> EncryptedFileKeyStore | None:
        """Get the Keystore if a passphrase is set."""
        return self._keystore

    async def ensure_did(
        self,
        entity_class: str,
        namespace: str,
        natural_key: str,
        *,
        name: str | None = None,
    ) -> MintResult:
        """Return the entity's DID, minting one on first sight of its handle.

        Idempotent: the handle→DID index is checked first (no scrypt on the
        skip path). When minting is disabled, returns ``did=None``.
        """
        handle = make_handle(entity_class, namespace, natural_key, name=name)
        async with self._lock:
            index = await self._load_index()
            existing = index.get(handle)
            if existing is not None:
                return MintResult(did=existing, handle=handle, created=False)
            if not self.enabled or self._keystore is None:
                if not self._warned_disabled:
                    self._warned_disabled = True
                    logger.warning(
                        "[swid] minting disabled (SWID_KEYSTORE_PASSPHRASE empty) — "
                        "entities get handles only, no DIDs"
                    )
                return MintResult(did=None, handle=handle, created=False)

            res = await swid_lib.create_async(
                hstp_endpoint=f"{self._hstp_base}/{handle}",
                also_known_as=[handle],
            )
            # Persist order matters: key first, then log, then index. A crash
            # in between leaves an orphan key or an un-indexed DID (harmless — a
            # re-run mints afresh); the reverse would register a DID whose key
            # is lost, i.e. an uncontrollable identity.
            await asyncio.to_thread(self._keystore.store, res.did, res.keypair)
            await self.registry.register([res.cel_genesis])
            index[handle] = res.did
            await self._save_index(index)
            return MintResult(did=res.did, handle=handle, created=True)

    async def _load_index(self) -> dict[str, str]:
        if self._index is None:
            self._index = await asyncio.to_thread(self._read_index)
        return self._index

    def _read_index(self) -> dict[str, str]:
        if self._index_path.exists():
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        return {}

    async def _save_index(self, index: dict[str, str]) -> None:
        self._index = index
        data = json.dumps(index, indent=0, sort_keys=True).encode("utf-8")
        await asyncio.to_thread(atomic_write, self._index_path, data)
