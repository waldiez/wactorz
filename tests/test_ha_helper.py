import hashlib
import unittest
from unittest.mock import patch

from wactorz.core.integrations.home_assistant import ha_helper


class _FakeHAWebSocketClient:
    instances = []

    def __init__(self, ws_url: str, token: str):
        self.ws_url = ws_url
        self.token = token
        self.calls = []
        self.responses = dict(_FakeHAWebSocketClient.responses)
        self.exceptions = dict(_FakeHAWebSocketClient.exceptions)
        _FakeHAWebSocketClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def call(self, command: str):
        self.calls.append(command)
        if command in self.exceptions:
            raise self.exceptions[command]
        return self.responses.get(command)


_FakeHAWebSocketClient.responses = {}
_FakeHAWebSocketClient.exceptions = {}


class _FakeResponse:
    def __init__(self, status=200, json_data=None, text_data="", headers=None, json_exc=None):
        self.status = status
        self._json_data = json_data
        self._text_data = text_data
        self.headers = headers or {"Content-Type": "application/json"}
        self._json_exc = json_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def json(self):
        if self._json_exc:
            raise self._json_exc
        return self._json_data

    async def text(self):
        return self._text_data


class _FakeClientSession:
    instances = []
    get_results = []
    post_results = []
    delete_results = []

    def __init__(self):
        self.get_calls = []
        self.post_calls = []
        self.delete_calls = []
        _FakeClientSession.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        result = _FakeClientSession.get_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        result = _FakeClientSession.post_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def delete(self, url, **kwargs):
        self.delete_calls.append((url, kwargs))
        result = _FakeClientSession.delete_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeClientError(Exception):
    pass


def _reset_fakes():
    _FakeHAWebSocketClient.instances = []
    _FakeHAWebSocketClient.responses = {}
    _FakeHAWebSocketClient.exceptions = {}
    _FakeClientSession.instances = []
    _FakeClientSession.get_results = []
    _FakeClientSession.post_results = []
    _FakeClientSession.delete_results = []


def _fixtures():
    floors = [{"floor_id": "floor1", "name": "Ground Floor", "icon": "mdi:home"}]
    areas = [
        {"area_id": "kitchen", "name": "Kitchen", "floor_id": "floor1", "aliases": ["Cooking"]},
        {"area_id": "living", "name": "Living Room", "aliases": ["Lounge"]},
    ]
    devices = [
        {
            "id": "dev-light",
            "name": "Ceiling Light",
            "name_by_user": "Kitchen Light",
            "area_id": "kitchen",
            "manufacturer": "Acme",
            "model": "L1",
            "labels": [],
            "disabled_by": None,
            "raw_field": "kept",
        },
        {
            "id": "dev-temp",
            "name": "Temperature Sensor",
            "name_by_user": None,
            "area_id": "living",
            "manufacturer": None,
            "model": "T1",
            "labels": ["climate"],
            "disabled_by": "user",
        },
    ]
    entities = [
        {
            "entity_id": "sensor.temperature",
            "unique_id": "temp-1",
            "platform": "mqtt",
            "device_id": "dev-temp",
            "area_id": None,
            "original_name": "Original Temp",
            "name": "Registry Temp",
            "hidden_by": None,
            "disabled_by": None,
            "entity_category": None,
            "icon": "mdi:thermometer",
        },
        {
            "entity_id": "light.kitchen",
            "unique_id": "light-1",
            "platform": "hue",
            "device_id": "dev-light",
            "area_id": "living",
            "original_name": "Original Light",
            "name": None,
            "hidden_by": "user",
            "disabled_by": None,
            "entity_category": "config",
        },
        {
            "entity_id": "sensor.hassio_cpu",
            "unique_id": "hassio-1",
            "platform": "hassio",
            "device_id": "dev-temp",
            "area_id": None,
            "original_name": "HA CPU",
            "name": None,
        },
        {
            "entity_id": "switch.orphan",
            "unique_id": "orphan-1",
            "platform": "mqtt",
            "device_id": None,
            "area_id": "kitchen",
            "original_name": "Orphan",
            "name": None,
        },
    ]
    states = [
        {
            "entity_id": "sensor.temperature",
            "state": "21.5",
            "attributes": {
                "friendly_name": "Living Temperature",
                "unit_of_measurement": "C",
                "device_class": "temperature",
                "icon": "mdi:thermometer",
                "entity_picture": "/pic.png",
                "uninteresting": "kept-by-simplified",
            },
        },
        {
            "entity_id": "light.kitchen",
            "state": "on",
            "attributes": {
                "friendly_name": "Kitchen Ceiling",
                "brightness": 128,
                "supported_features": 1,
            },
        },
        {
            "entity_id": "sensor.hassio_cpu",
            "state": "5",
            "attributes": {"friendly_name": "Supervisor CPU"},
        },
    ]
    return floors, areas, devices, entities, states


class HomeAssistantHelperPureTest(unittest.TestCase):
    def test_normalize_swid_segment(self):
        self.assertEqual(ha_helper._normalize_swid_segment(" Kitchen_Light!! "), "kitchen-light")
        self.assertEqual(ha_helper._normalize_swid_segment("A__  B---C"), "a-b-c")
        self.assertEqual(ha_helper._normalize_swid_segment("Name.v2"), "name.v2")
        self.assertEqual(ha_helper._normalize_swid_segment(" !!! "), "")

    def test_generate_swid_is_deterministic_and_uses_fallbacks(self):
        expected_hash = hashlib.sha256(b"device-123").hexdigest()[:6]

        swid = ha_helper.generate_swid("device-123", name="Kitchen Light!", area="Main_Floor")

        self.assertEqual(swid, f"did:swid:home:main-floor:kitchen-light-{expected_hash}")
        self.assertEqual(
            ha_helper.generate_swid("device-123", name="Kitchen Light!", area="Main_Floor"),
            swid,
        )
        fallback_hash = hashlib.sha256(b"blank").hexdigest()[:6]
        self.assertEqual(
            ha_helper.generate_swid("blank", name="!!!", area="   "),
            f"did:swid:home:unassigned:device-{fallback_hash}",
        )

    def test_normalize_ha_ws_url(self):
        cases = {
            "": "",
            "  ": "",
            "http://ha.local:8123": "ws://ha.local:8123/api/websocket",
            "https://ha.local": "wss://ha.local/api/websocket",
            "ws://ha.local": "ws://ha.local/api/websocket",
            "wss://ha.local/": "wss://ha.local/api/websocket",
            "ws://ha.local/api/websocket": "ws://ha.local/api/websocket",
            "https://ha.local/custom": "wss://ha.local/custom/api/websocket",
            "mqtt://ha.local": "mqtt://ha.local",
            "ha.local:8123": "ha.local:8123",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(ha_helper.normalize_ha_ws_url(value), expected)

    def test_normalize_ha_base_url(self):
        cases = {
            "": "",
            "http://ha.local:8123/": "http://ha.local:8123",
            "https://ha.local/api/websocket": "https://ha.local",
            "ws://ha.local/api/websocket": "http://ha.local",
            "wss://ha.local/custom/api/websocket": "https://ha.local/custom",
            "mqtt://ha.local": "mqtt://ha.local",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(ha_helper.normalize_ha_base_url(value), expected)


class HomeAssistantHelperWebSocketTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset_fakes()
        self.ws_patch = patch(
            "wactorz.core.integrations.home_assistant.ha_helper.HAWebSocketClient",
            _FakeHAWebSocketClient,
        )
        self.ws_patch.start()
        self.addCleanup(self.ws_patch.stop)

    def _set_fixture_responses(self):
        floors, areas, devices, entities, states = _fixtures()
        _FakeHAWebSocketClient.responses = {
            "config/floor_registry/list": floors,
            "config/area_registry/list": areas,
            "config/device_registry/list": devices,
            "config/entity_registry/list": entities,
            "get_states": states,
            "config/entity_registry/list_for_display": {"entities": [{"ei": "light.kitchen"}]},
            "homeassistant/expose_entity/list": {
                "exposed_entities": {
                    "light.kitchen": {"conversation": True},
                    "sensor.temperature": {"conversation": True},
                    "switch.hidden": {"conversation": False},
                }
            },
            "config_entries/get": [{"entry_id": "entry-1", "domain": "mqtt"}],
        }
        return floors, areas, devices, entities, states

    async def test_fetch_devices_entities_with_location_without_states(self):
        _floors, _areas, _devices, _entities, _states = self._set_fixture_responses()

        result = await ha_helper.fetch_devices_entities_with_location(
            "http://ha.local:8123",
            "token",
        )

        client = _FakeHAWebSocketClient.instances[0]
        self.assertEqual(client.ws_url, "ws://ha.local:8123/api/websocket")
        self.assertEqual(
            client.calls,
            [
                "config/area_registry/list",
                "config/device_registry/list",
                "config/entity_registry/list",
            ],
        )
        self.assertEqual([device["name"] for device in result], ["Kitchen Light", "Temperature Sensor"])
        kitchen = result[0]
        self.assertEqual(kitchen["area"], "Kitchen")
        self.assertEqual(kitchen["entities"][0]["entity_id"], "light.kitchen")
        self.assertEqual(kitchen["entities"][0]["area"], "Living Room")
        self.assertNotIn("state", kitchen["entities"][0])
        self.assertTrue(kitchen["swid"].startswith("did:swid:home:kitchen:kitchen-light-"))
        self.assertEqual(len(result[1]["entities"]), 2)
        self.assertNotIn("switch.orphan", {e["entity_id"] for d in result for e in d["entities"]})

    async def test_fetch_devices_entities_with_location_with_states(self):
        self._set_fixture_responses()

        result = await ha_helper.fetch_devices_entities_with_location(
            "ws://ha.local",
            "token",
            include_states=True,
        )

        client = _FakeHAWebSocketClient.instances[0]
        self.assertEqual(client.calls[-1], "get_states")
        entities = {e["entity_id"]: e for d in result for e in d["entities"]}
        self.assertEqual(entities["light.kitchen"]["state"], "on")
        self.assertEqual(entities["light.kitchen"]["attributes"]["brightness"], 128)

    async def test_get_floors_safe_success_and_failure(self):
        self._set_fixture_responses()
        client = _FakeHAWebSocketClient("ws://ha.local/api/websocket", "token")
        self.assertEqual(await ha_helper._get_floors_safe(client), _fixtures()[0])

        _FakeHAWebSocketClient.exceptions = {"config/floor_registry/list": RuntimeError("old HA")}
        failing_client = _FakeHAWebSocketClient("ws://ha.local/api/websocket", "token")
        self.assertEqual(await ha_helper._get_floors_safe(failing_client), [])

    async def test_get_full_ha_data_preserves_raw_data_and_empty_fallbacks(self):
        floors, areas, devices, entities, states = self._set_fixture_responses()

        result = await ha_helper.get_full_ha_data("http://ha.local:8123", "token")

        self.assertEqual(result["floors"], floors)
        self.assertEqual(result["areas"], areas)
        self.assertEqual(result["devices"], devices)
        self.assertEqual(result["entities"], entities)
        self.assertEqual(result["states"], states)

        _reset_fakes()
        _FakeHAWebSocketClient.responses = {
            "config/floor_registry/list": None,
            "config/area_registry/list": None,
            "config/device_registry/list": None,
            "config/entity_registry/list": None,
            "get_states": None,
        }
        result = await ha_helper.get_full_ha_data("http://ha.local:8123", "token")
        self.assertEqual(result, {"floors": [], "areas": [], "devices": [], "entities": [], "states": []})

    async def test_get_simplified_ha_data_shapes_prompt_friendly_payload(self):
        self._set_fixture_responses()

        result = await ha_helper.get_simplified_ha_data("http://ha.local:8123", "token")

        self.assertEqual(set(result), {"floors", "areas", "devices", "entities"})
        self.assertNotIn("states", result)
        self.assertEqual(result["floors"], [{"floor_id": "floor1", "name": "Ground Floor"}])
        self.assertEqual(result["areas"], [{"area_id": "kitchen", "name": "Kitchen"}, {"area_id": "living", "name": "Living Room"}])
        self.assertEqual(
            result["devices"],
            [
                {
                    "id": "dev-light",
                    "name": "Ceiling Light",
                    "name_by_user": "Kitchen Light",
                    "area_id": "kitchen",
                    "manufacturer": "Acme",
                    "model": "L1",
                },
                {
                    "id": "dev-temp",
                    "name": "Temperature Sensor",
                    "area_id": "living",
                    "model": "T1",
                    "labels": ["climate"],
                    "disabled_by": "user",
                },
            ],
        )
        entities = {item["entity_id"]: item for item in result["entities"]}
        self.assertEqual(set(entities), {"sensor.temperature", "light.kitchen", "switch.orphan"})
        self.assertEqual(entities["sensor.temperature"]["name"], "Living Temperature")
        self.assertEqual(entities["sensor.temperature"]["area_id"], "living")
        self.assertEqual(entities["sensor.temperature"]["state"], "21.5")
        self.assertEqual(entities["sensor.temperature"]["unit_of_measurement"], "C")
        self.assertEqual(entities["sensor.temperature"]["uninteresting"], "kept-by-simplified")
        self.assertNotIn("friendly_name", entities["sensor.temperature"])
        self.assertNotIn("icon", entities["sensor.temperature"])
        self.assertNotIn("entity_picture", entities["sensor.temperature"])
        self.assertEqual(entities["light.kitchen"]["domain"], "light")
        self.assertEqual(entities["light.kitchen"]["area_id"], "living")

    async def test_simple_registry_helpers_call_expected_commands(self):
        floors, areas, devices, entities, states = self._set_fixture_responses()

        self.assertEqual(await ha_helper.get_floors("http://ha.local:8123", "token"), floors)
        self.assertEqual(await ha_helper.get_areas("http://ha.local:8123", "token"), areas)
        self.assertEqual(await ha_helper.get_entities("http://ha.local:8123", "token"), entities)
        self.assertEqual(await ha_helper.get_entities_for_display("http://ha.local:8123", "token"), {"entities": [{"ei": "light.kitchen"}]})
        self.assertEqual(
            await ha_helper.get_exposed_entities("http://ha.local:8123", "token"),
            {
                "exposed_entities": {
                    "light.kitchen": {"conversation": True},
                    "sensor.temperature": {"conversation": True},
                    "switch.hidden": {"conversation": False},
                }
            },
        )
        self.assertEqual(await ha_helper.get_states("http://ha.local:8123", "token"), states)
        self.assertEqual(await ha_helper.get_config_entries("http://ha.local:8123", "token"), [{"entry_id": "entry-1", "domain": "mqtt"}])

        command_by_call = [client.calls[0] for client in _FakeHAWebSocketClient.instances]
        self.assertIn("config/floor_registry/list", command_by_call)
        self.assertIn("config/area_registry/list", command_by_call)
        self.assertIn("config/entity_registry/list", command_by_call)
        self.assertIn("config/entity_registry/list_for_display", command_by_call)
        self.assertIn("homeassistant/expose_entity/list", command_by_call)
        self.assertIn("get_states", command_by_call)
        self.assertIn("config_entries/get", command_by_call)

        _FakeHAWebSocketClient.exceptions = {"config/floor_registry/list": RuntimeError("old HA")}
        self.assertEqual(await ha_helper.get_floors("http://ha.local:8123", "token"), [])
        _FakeHAWebSocketClient.exceptions = {"config_entries/get": RuntimeError("old HA")}
        self.assertEqual(await ha_helper.get_config_entries("http://ha.local:8123", "token"), [])

    async def test_device_and_entity_simple_helpers(self):
        _floors, _areas, devices, _entities, _states = self._set_fixture_responses()

        full_devices = await ha_helper.get_devices("http://ha.local:8123", "token")

        self.assertIs(full_devices, devices)
        self.assertIn("swid", full_devices[0])
        self.assertTrue(full_devices[0]["swid"].startswith("did:swid:home:kitchen:kitchen-light-"))

        _FakeHAWebSocketClient.instances = []
        simple_devices = await ha_helper.get_devices_simple("http://ha.local:8123", "token")
        self.assertEqual(
            simple_devices,
            [
                {
                    "device_id": "dev-light",
                    "name": "Kitchen Light",
                    "swid": full_devices[0]["swid"],
                    "manufacturer": "Acme",
                    "model": "L1",
                },
                {
                    "device_id": "dev-temp",
                    "name": "Temperature Sensor",
                    "swid": full_devices[1]["swid"],
                    "manufacturer": None,
                    "model": "T1",
                },
            ],
        )
        self.assertEqual(
            await ha_helper.get_entities_simple("http://ha.local:8123", "token"),
            [
                {
                    "entity_id": "sensor.temperature",
                    "unique_id": "temp-1",
                    "platform": "mqtt",
                    "original_name": "Original Temp",
                    "name": "Registry Temp",
                },
                {
                    "entity_id": "light.kitchen",
                    "unique_id": "light-1",
                    "platform": "hue",
                    "original_name": "Original Light",
                    "name": None,
                },
                {
                    "entity_id": "sensor.hassio_cpu",
                    "unique_id": "hassio-1",
                    "platform": "hassio",
                    "original_name": "HA CPU",
                    "name": None,
                },
                {
                    "entity_id": "switch.orphan",
                    "unique_id": "orphan-1",
                    "platform": "mqtt",
                    "original_name": "Orphan",
                    "name": None,
                },
            ],
        )


class HomeAssistantHelperAutomationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset_fakes()
        self.ws_patch = patch(
            "wactorz.core.integrations.home_assistant.ha_helper.HAWebSocketClient",
            _FakeHAWebSocketClient,
        )
        self.session_patch = patch(
            "wactorz.core.integrations.home_assistant.ha_helper.aiohttp.ClientSession",
            _FakeClientSession,
            create=True,
        )
        self.client_error_patch = patch(
            "wactorz.core.integrations.home_assistant.ha_helper.aiohttp.ClientError",
            _FakeClientError,
            create=True,
        )
        self.ws_patch.start()
        self.session_patch.start()
        self.client_error_patch.start()
        self.addCleanup(self.ws_patch.stop)
        self.addCleanup(self.session_patch.stop)
        self.addCleanup(self.client_error_patch.stop)

    async def test_fetch_automation_config_success_and_fallbacks(self):
        session = _FakeClientSession()
        _FakeClientSession.get_results = [_FakeResponse(json_data={"id": "auto-1", "alias": "Lights"})]
        result = await ha_helper._fetch_automation_config("http://ha.local", "auto-1", "token", session)
        self.assertEqual(result, {"id": "auto-1", "alias": "Lights"})
        self.assertEqual(
            session.get_calls[0],
            (
                "http://ha.local/api/config/automation/config/auto-1",
                {
                    "headers": {
                        "Authorization": "Bearer token",
                        "Content-Type": "application/json",
                    }
                },
            ),
        )

        for fake_result in [
            _FakeResponse(status=404, json_data={"error": "missing"}),
            _FakeResponse(json_data=["not", "a", "dict"]),
            _FakeResponse(json_exc=ValueError("bad json")),
            _FakeClientError("network"),
        ]:
            with self.subTest(fake_result=type(fake_result).__name__):
                _FakeClientSession.get_results = [fake_result]
                self.assertIsNone(
                    await ha_helper._fetch_automation_config("http://ha.local", "auto-1", "token", session)
                )

    async def test_post_automation_config_success_json_text_and_error(self):
        _FakeClientSession.post_results = [
            _FakeResponse(status=201, json_data={"ok": True}),
            _FakeResponse(status=200, text_data="ok", headers={"Content-Type": "text/plain"}),
            _FakeResponse(status=400, json_data={"error": "bad"}),
        ]

        result = await ha_helper._post_automation_config(
            "ws://ha.local/api/websocket",
            "token",
            "auto-1",
            {
                "name": " Evening Lights ",
                "description": "  Nice ",
                "trigger": [{"platform": "state"}],
                "condition": [],
                "action": [{"service": "light.turn_on"}],
                "mode": "",
            },
        )

        self.assertEqual(result, {"automation_id": "auto-1", "status": 201, "result": {"ok": True}})
        first_session = _FakeClientSession.instances[0]
        self.assertEqual(first_session.post_calls[0][0], "http://ha.local/api/config/automation/config/auto-1")
        self.assertEqual(
            first_session.post_calls[0][1],
            {
                "headers": {
                    "Authorization": "Bearer token",
                    "Content-Type": "application/json",
                },
                "json": {
                    "alias": "Evening Lights",
                    "description": "Nice",
                    "trigger": [{"platform": "state"}],
                    "condition": [],
                    "action": [{"service": "light.turn_on"}],
                    "mode": "single",
                },
            },
        )

        result = await ha_helper._post_automation_config(
            "http://ha.local",
            "token",
            "auto-2",
            {},
            default_alias="Updated automation",
        )
        self.assertEqual(result, {"automation_id": "auto-2", "status": 200, "result": "ok"})

        with self.assertRaisesRegex(RuntimeError, "REST automation POST failed \\(400\\)"):
            await ha_helper._post_automation_config("http://ha.local", "token", "auto-3", {})

    async def test_create_automation_via_rest_and_websocket_delegate_to_rest_posting(self):
        _FakeClientSession.post_results = [
            _FakeResponse(status=200, json_data={"ok": True}),
            _FakeResponse(status=200, json_data={"ok": True}),
        ]

        with patch("wactorz.core.integrations.home_assistant.ha_helper.time.time", return_value=12345):
            result = await ha_helper.create_automation_via_rest(
                "http://ha.local",
                "token",
                {"name": "Morning Lights!", "action": [{"service": "light.turn_on"}]},
            )
            delegated = await ha_helper.create_automation_via_websocket(
                "ws://ha.local/api/websocket",
                "token",
                {"name": "Other Lights", "action": []},
            )

        self.assertEqual(result["automation_id"], "morning_lights_12345")
        self.assertEqual(delegated["automation_id"], "other_lights_12345")
        self.assertEqual(
            _FakeClientSession.instances[0].post_calls[0][0],
            "http://ha.local/api/config/automation/config/morning_lights_12345",
        )
        self.assertEqual(
            _FakeClientSession.instances[1].post_calls[0][0],
            "http://ha.local/api/config/automation/config/other_lights_12345",
        )

    async def test_get_automations_fetches_configs_for_valid_automation_states(self):
        _FakeHAWebSocketClient.responses = {
            "get_states": [
                {"entity_id": "automation.morning", "attributes": {"id": "auto-1"}},
                {"entity_id": "automation.malformed", "attributes": {}},
                {"entity_id": "light.kitchen", "attributes": {"id": "not-auto"}},
                "not a state",
                {"entity_id": "automation.evening", "attributes": {"id": "auto-2"}},
            ]
        }
        _FakeClientSession.get_results = [
            _FakeResponse(json_data={"id": "auto-1", "alias": "Morning"}),
            _FakeResponse(status=404, json_data={"error": "missing"}),
        ]

        result = await ha_helper.get_automations("https://ha.local", "token")

        self.assertEqual(result, [{"id": "auto-1", "alias": "Morning"}])
        self.assertEqual(_FakeHAWebSocketClient.instances[0].ws_url, "wss://ha.local/api/websocket")
        session = _FakeClientSession.instances[0]
        self.assertEqual(
            [call[0] for call in session.get_calls],
            [
                "https://ha.local/api/config/automation/config/auto-1",
                "https://ha.local/api/config/automation/config/auto-2",
            ],
        )

    async def test_update_and_delete_automation(self):
        _FakeClientSession.post_results = [_FakeResponse(status=200, json_data={"ok": True})]
        _FakeClientSession.delete_results = [
            _FakeResponse(status=200, json_data={"ok": True}),
            _FakeResponse(status=404, json_data={"error": "missing"}),
        ]

        result = await ha_helper.update_automation("http://ha.local", "token", "auto-1", {})
        self.assertEqual(result, {"automation_id": "auto-1", "status": 200, "result": {"ok": True}})
        self.assertEqual(
            _FakeClientSession.instances[0].post_calls[0][1]["json"]["alias"],
            "Updated automation",
        )

        self.assertTrue(await ha_helper.delete_automation("http://ha.local", "token", "auto-1"))
        self.assertFalse(await ha_helper.delete_automation("http://ha.local", "token", "auto-missing"))
        delete_call = _FakeClientSession.instances[1].delete_calls[0]
        self.assertEqual(delete_call[0], "http://ha.local/api/config/automation/config/auto-1")
        self.assertEqual(
            delete_call[1]["headers"],
            {
                "Authorization": "Bearer token",
                "Content-Type": "application/json",
            },
        )


class HomeAssistantHelperLiveContextTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset_fakes()
        self.ws_patch = patch(
            "wactorz.core.integrations.home_assistant.ha_helper.HAWebSocketClient",
            _FakeHAWebSocketClient,
        )
        self.ws_patch.start()
        self.addCleanup(self.ws_patch.stop)

    def _set_live_context_responses(self, exposed=None):
        _floors, areas, _devices, entities, states = _fixtures()
        _FakeHAWebSocketClient.responses = {
            "get_states": [
                *states,
                {
                    "entity_id": "switch.hidden",
                    "state": "off",
                    "attributes": {"friendly_name": "Hidden Switch"},
                },
                {"entity_id": "invalid", "state": "on", "attributes": {"friendly_name": "Invalid"}},
                {"state": "on", "attributes": {"friendly_name": "Missing Entity"}},
            ],
            "config/area_registry/list": areas,
            "config/entity_registry/list": entities + [
                {"entity_id": "switch.hidden", "area_id": "kitchen"},
            ],
            "homeassistant/expose_entity/list": exposed
            if exposed is not None
            else {
                "exposed_entities": {
                    "light.kitchen": {"conversation": True},
                    "sensor.temperature": {"conversation": True},
                    "switch.hidden": {"conversation": False},
                }
            },
        }

    async def test_get_live_context_filters_to_exposed_sorted_entities(self):
        self._set_live_context_responses()

        result = await ha_helper.get_live_context("http://ha.local:8123", "token")

        self.assertTrue(result["success"])
        self.assertEqual([item["entity_id"] for item in result["entities"]], ["light.kitchen", "sensor.temperature"])
        self.assertEqual(result["entities"][0]["name"], "Kitchen Ceiling")
        self.assertEqual(result["entities"][0]["area"], "Living Room")
        self.assertEqual(result["entities"][0]["attributes"], {"brightness": 128})
        self.assertEqual(
            result["entities"][1]["attributes"],
            {"unit_of_measurement": "C", "device_class": "temperature"},
        )

    async def test_get_live_context_accepts_direct_exposure_mapping_and_filters(self):
        self._set_live_context_responses(
            exposed={
                "light.kitchen": {"assist": True},
                "sensor.temperature": {"assist": True},
            }
        )

        by_name = await ha_helper.get_live_context("http://ha.local:8123", "token", name="ceiling")
        self.assertEqual([item["entity_id"] for item in by_name["entities"]], ["light.kitchen"])

        by_domain = await ha_helper.get_live_context("http://ha.local:8123", "token", domain="sensor")
        self.assertEqual([item["entity_id"] for item in by_domain["entities"]], ["sensor.temperature"])

        by_domain_list = await ha_helper.get_live_context(
            "http://ha.local:8123",
            "token",
            domain=["light", "switch"],
        )
        self.assertEqual([item["entity_id"] for item in by_domain_list["entities"]], ["light.kitchen"])

        by_area = await ha_helper.get_live_context("http://ha.local:8123", "token", area="Living Room")
        self.assertEqual([item["entity_id"] for item in by_area["entities"]], ["light.kitchen"])

        by_alias = await ha_helper.get_live_context("http://ha.local:8123", "token", area="Lounge")
        self.assertEqual([item["entity_id"] for item in by_alias["entities"]], ["light.kitchen"])

        combined = await ha_helper.get_live_context(
            "http://ha.local:8123",
            "token",
            name="ceiling",
            domain=["light"],
            area="Living Room",
        )
        self.assertEqual([item["entity_id"] for item in combined["entities"]], ["light.kitchen"])

    async def test_get_live_context_failure_results(self):
        self._set_live_context_responses()

        unknown_area = await ha_helper.get_live_context("http://ha.local:8123", "token", area="Garage")
        self.assertEqual(unknown_area, {"success": False, "error": "Area 'Garage' does not exist"})

        no_match = await ha_helper.get_live_context("http://ha.local:8123", "token", name="attic")
        self.assertEqual(no_match, {"success": False, "error": "No entities matched the provided filter"})

        self._set_live_context_responses(exposed={"switch.hidden": {"conversation": False}})
        no_entities = await ha_helper.get_live_context("http://ha.local:8123", "token")
        self.assertEqual(no_entities, {"success": False, "error": "No entities found"})


if __name__ == "__main__":
    unittest.main()
