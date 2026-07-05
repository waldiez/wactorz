"""Tests for the SWID subsystem: generation, registry, service, resolver."""

from __future__ import annotations

import json
import unittest
from contextlib import nullcontext

# Several sibling test modules replace ``sys.modules["aiohttp"]`` with a stub at
# import time, so the real ``aiohttp.test_utils`` may be unavailable when the
# whole suite runs. Guard the import and skip only the HTTP routing tests in
# that case -- the resolver logic itself is covered by TestResolver (which calls
# ``resolve`` directly) regardless.
try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    _AIOHTTP_REAL = True
except Exception:  # aiohttp stubbed by a sibling test
    _AIOHTTP_REAL = False

from wactorz.core import swid
from wactorz.core.swid import (
    FusekiSwidRegistry,
    InMemoryRegistry,
    Record,
    SwidCollisionError,
    SwidService,
    aiohttp_routes,
    build_did_document,
    resolve,
    status_for,
)


class TestGeneration(unittest.TestCase):
    def test_device_swid_is_deterministic(self):
        a = swid.device_swid("home", "dev-1", name="Thermostat")
        b = swid.device_swid("home", "dev-1", name="Thermostat")
        self.assertEqual(str(a), str(b))
        # Pilot scheme: honest ``swid:`` prefix, NOT the official ``did:swid:``.
        self.assertTrue(str(a).startswith("swid:device:home:thermostat-"))

    def test_device_swid_is_room_independent(self):
        # Fingerprint is over device_id only -> the room the device sits in
        # never appears, so moving rooms cannot change the SWID.
        a = swid.device_swid("home", "dev-1", name="Thermostat")
        b = swid.device_swid("home", "dev-1", name="Thermostat")
        self.assertEqual(str(a), str(b))

    def test_display_name_change_does_not_move_fingerprint(self):
        a = swid.device_swid("home", "dev-1", name="Old")
        b = swid.device_swid("home", "dev-1", name="New")
        self.assertEqual(a.local_id.rsplit("-", 1)[1], b.local_id.rsplit("-", 1)[1])

    def test_namespace_scopes_fingerprint(self):
        a = swid.device_swid("site-a", "shared", name="x")
        b = swid.device_swid("site-b", "shared", name="x")
        self.assertNotEqual(str(a), str(b))

    def test_all_class_helpers(self):
        for fn, cls in (
            (swid.space_swid, "space"),
            (swid.agent_swid, "agent"),
            (swid.user_swid, "user"),
        ):
            s = fn("home", "k", name="n")
            self.assertEqual(s.entity_class, cls)
            self.assertTrue(swid.is_valid_swid(str(s)))

    def test_parse_accepts_both_pilot_and_legacy_prefixes(self):
        # Pilot ``swid:`` scheme and grandfathered ``did:swid:`` incumbents both
        # validate; dotted legacy segments pass too.
        self.assertTrue(swid.is_valid_swid("swid:device:home:thermostat-ab12cd34"))
        self.assertTrue(swid.is_valid_swid("did:swid:home:kitchen:name.v2-abc123"))
        legacy = swid.legacy_home_swid("dev-1", name="Lamp", area="Hall")
        self.assertTrue(legacy.startswith("did:swid:home:"))  # byte-for-byte incumbent
        self.assertTrue(swid.is_valid_swid(legacy))

    def test_rejects_real_conformant_did_swid_scid(self):
        # A real SWF-STD-5 ``did:swid:z<scid>`` is a single segment, so our
        # structured parser rejects it -- by design, so the two schemes coexist.
        self.assertFalse(
            swid.is_valid_swid("did:swid:zQmQoeG7u6XBtdXoek5p3aPoTjaSRemHAKrMcY2Hcjpe3jv")
        )

    def test_new_generator_rejects_unknown_class(self):
        with self.assertRaises(swid.InvalidSwidError):
            swid.generate_swid("robot", "home", "k")


class TestDocument(unittest.TestCase):
    def test_document_fields(self):
        doc = build_did_document(
            "swid:device:home:lamp-abc123",
            resolver_base="http://r",
            also_known_as=["ha:light.lamp"],
            created="2026-07-05T00:00:00Z",
        )
        self.assertEqual(doc["id"], "swid:device:home:lamp-abc123")
        self.assertEqual(doc["controller"], doc["id"])
        self.assertEqual(doc["service"][0]["type"], "HSMLProfile")
        self.assertIn(doc["id"], doc["service"][0]["serviceEndpoint"])
        self.assertEqual(doc["alsoKnownAs"], ["ha:light.lamp"])
        self.assertEqual(doc["created"], "2026-07-05T00:00:00Z")


class TestServiceAndRegistry(unittest.IsolatedAsyncioTestCase):
    async def test_onboard_is_idempotent(self):
        reg = InMemoryRegistry()
        svc = SwidService(reg, resolver_base="http://r")
        a = await svc.onboard_device("home", "dev-1", name="Thermostat")
        b = await svc.onboard_device("home", "dev-1", name="Thermostat")
        self.assertEqual(a, b)
        self.assertEqual(len(reg._by_swid), 1)

    async def test_area_is_context_not_identity(self):
        reg = InMemoryRegistry()
        svc = SwidService(reg)
        rec = await svc.onboard_device("home", "dev-1", name="Lamp", area_iri="haarea:hall")
        self.assertNotIn("hall", rec.swid)  # room not baked into the id
        self.assertEqual(rec.document["alsoKnownAs"], ["haarea:hall"])

    async def test_distinct_devices_distinct_swids(self):
        reg = InMemoryRegistry()
        svc = SwidService(reg)
        a = await svc.onboard_device("home", "dev-1")
        b = await svc.onboard_device("home", "dev-2")
        self.assertNotEqual(a.swid, b.swid)

    async def test_registry_rejects_rebinding(self):
        reg = InMemoryRegistry()
        s = "swid:device:home:x-abcdef12"
        await reg.put(Record(s, "key-1", {"id": s}))
        with self.assertRaises(SwidCollisionError):
            await reg.put(Record(s, "key-2", {"id": s}))


class TestResolver(unittest.IsolatedAsyncioTestCase):
    async def _seed(self):
        reg = InMemoryRegistry()
        rec = await SwidService(reg, resolver_base="http://r").onboard_device(
            "home", "dev-1", name="Thermostat"
        )
        return reg, rec

    async def test_resolve_roundtrip(self):
        reg, rec = await self._seed()
        result = await resolve(rec.swid, reg)
        self.assertIsNone(result["didResolutionMetadata"].get("error"))
        self.assertEqual(result["didDocument"]["id"], rec.swid)

    async def test_resolve_not_found(self):
        reg, _ = await self._seed()
        result = await resolve("swid:device:home:ghost-00000000", reg)
        self.assertEqual(result["didResolutionMetadata"]["error"], "notFound")

    async def test_resolve_invalid(self):
        reg, _ = await self._seed()
        result = await resolve("did:web:example.com", reg)
        self.assertEqual(result["didResolutionMetadata"]["error"], "invalidDid")

    async def test_status_for_maps_result_to_http_code(self):
        # Shared mapping used by both aiohttp_routes and the monitor handler;
        # runs even when the HTTP routing tests are skipped (aiohttp stubbed).
        reg, rec = await self._seed()
        self.assertEqual(status_for(await resolve(rec.swid, reg)), 200)
        self.assertEqual(status_for(await resolve("swid:device:home:ghost-00000000", reg)), 404)
        self.assertEqual(status_for(await resolve("did:web:example.com", reg)), 400)


class _FakeFuseki:
    """In-memory stand-in for FusekiClient's async SPARQL surface."""

    def __init__(self) -> None:
        self.updates: list[str] = []
        self.store: dict[str, Record] = {}

    async def sparql_update(self, query: str) -> None:
        self.updates.append(query)

    async def sparql_select(self, query: str) -> list[dict]:
        for s, rec in self.store.items():
            if f"<{s}>" in query:
                return [
                    {
                        "swid": s,
                        "naturalKey": rec.natural_key,
                        "document": json.dumps(rec.document),
                        "metadata": json.dumps(rec.metadata),
                    }
                ]
        return []


class TestFusekiRegistry(unittest.IsolatedAsyncioTestCase):
    async def test_put_emits_insert(self):
        fake = _FakeFuseki()
        reg = FusekiSwidRegistry(fake)
        s = "swid:device:home:x-abcdef12"
        await reg.put(Record(s, "key-1", {"id": s}))
        self.assertEqual(len(fake.updates), 1)
        self.assertIn("INSERT DATA", fake.updates[0])
        self.assertIn(s, fake.updates[0])

    async def test_get_parses_row(self):
        fake = _FakeFuseki()
        reg = FusekiSwidRegistry(fake)
        s = "swid:device:home:x-abcdef12"
        fake.store[s] = Record(s, "key-1", {"id": s}, {"note": "n"})
        got = await reg.get(s)
        self.assertIsNotNone(got)
        self.assertEqual(got.natural_key, "key-1")
        self.assertEqual(got.metadata, {"note": "n"})

    async def test_put_idempotent_no_second_insert(self):
        fake = _FakeFuseki()
        reg = FusekiSwidRegistry(fake)
        s = "swid:device:home:x-abcdef12"
        rec = Record(s, "key-1", {"id": s})
        fake.store[s] = rec  # already persisted
        await reg.put(rec)
        self.assertEqual(len(fake.updates), 0)


@unittest.skipUnless(_AIOHTTP_REAL, "aiohttp stubbed by a sibling test module")
class TestResolverHTTP(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reg = InMemoryRegistry()
        self.rec = await SwidService(reg, resolver_base="http://r").onboard_device(
            "home", "dev-1", name="Thermostat"
        )
        app = web.Application()
        app.add_routes(aiohttp_routes(lambda: nullcontext(reg)))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_resolve_ok(self):
        resp = await self.client.get(f"/1.0/identifiers/{self.rec.swid}")
        self.assertEqual(resp.status, 200)
        body = await resp.json(content_type=None)
        self.assertEqual(body["didDocument"]["id"], self.rec.swid)

    async def test_profile_ok(self):
        resp = await self.client.get(f"/1.0/identifiers/{self.rec.swid}/profile")
        self.assertEqual(resp.status, 200)
        body = await resp.json(content_type=None)
        self.assertEqual(body["id"], self.rec.swid)

    async def test_not_found(self):
        resp = await self.client.get("/1.0/identifiers/swid:device:home:ghost-00000000")
        self.assertEqual(resp.status, 404)

    async def test_invalid(self):
        resp = await self.client.get("/1.0/identifiers/did:web:example.com")
        self.assertEqual(resp.status, 400)


if __name__ == "__main__":
    unittest.main()
