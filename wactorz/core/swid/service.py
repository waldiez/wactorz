"""SwidService: the idempotent onboarding facade (async).

This is what the Device Onboarding Flow calls when a device is discovered:

    record = await service.onboard_device(namespace, device_id, name=..., area_iri=...)

It ties deterministic generation (:mod:`.identifier`), the DID-document builder
(:mod:`.document`) and a :class:`Registry` together, and guarantees that
re-onboarding the same entity returns the existing record unchanged -- the key
property for a discovery loop that re-fires on every Home Assistant restart.
"""

from typing import Any, final

from .document import build_did_document
from .identifier import DEFAULT_FINGERPRINT_LEN, generate_swid
from .registry import Record, Registry, SwidCollisionError


@final
class SwidService:
    """Mint-or-return SWIDs and their DID documents, idempotently."""

    def __init__(self, registry: Registry, *, resolver_base: str = "") -> None:
        self._registry = registry
        self._resolver_base = resolver_base

    async def onboard(
        self,
        entity_class: str,
        namespace: str,
        natural_key: str,
        *,
        name: str | None = None,
        controller: str | None = None,
        verification_method: list[dict[str, Any]] | None = None,  # pyright: ignore[reportExplicitAny]
        also_known_as: list[str] | None = None,
        metadata: dict[str, Any] | None = None,  # pyright: ignore[reportExplicitAny]
        fingerprint_len: int = DEFAULT_FINGERPRINT_LEN,
    ) -> Record:
        """Return the existing or newly created :class:`Record` for an entity.

        Idempotent: identical inputs always yield the same SWID, so a repeat
        call returns the stored record. Raises :class:`SwidCollisionError` only
        on a genuine fingerprint collision (retry with a larger
        ``fingerprint_len``).
        """
        swid_str = str(
            generate_swid(
                entity_class,
                namespace,
                natural_key,
                name=name,
                fingerprint_len=fingerprint_len,
            )
        )

        existing = await self._registry.get(swid_str)
        if existing is not None:
            if existing.natural_key != natural_key:
                msg = (
                    f"{swid_str} already bound to {existing.natural_key!r}; "
                    f"re-mint {natural_key!r} with a larger fingerprint_len"
                )
                raise SwidCollisionError(msg)
            return existing

        document = build_did_document(
            swid_str,
            resolver_base=self._resolver_base,
            controller=controller,
            verification_method=verification_method,
            also_known_as=also_known_as,
        )
        record = Record(
            swid=swid_str,
            natural_key=natural_key,
            document=document,
            metadata=metadata or {},
        )
        await self._registry.put(record)
        return record

    async def onboard_device(
        self,
        namespace: str,
        device_id: str,
        *,
        name: str | None = None,
        area_iri: str | None = None,
        metadata: dict[str, Any] | None = None,  # pyright: ignore[reportExplicitAny]
    ) -> Record:
        """Onboard a Home Assistant device.

        ``area_iri`` (the ``ha:`` / ``haarea:`` graph node for the current room)
        is recorded as ``alsoKnownAs`` context, never baked into the SWID -- so
        moving the device between rooms does not change its identifier.
        """
        return await self.onboard(
            "device",
            namespace,
            device_id,
            name=name,
            also_known_as=[area_iri] if area_iri else None,
            metadata=metadata,
        )
