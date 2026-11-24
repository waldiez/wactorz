# SPDX-License-Identifier: Apache-2.0
"""A persistent MQTT actuator resumes after its Home Assistant socket disappears."""

import json
import logging
import uuid
from pathlib import Path
from typing import Any

import pytest
import websockets

from wactorz.agents import home_assistant_actuator_agent as module
from wactorz.agents.home_assistant_actuator_agent import (
    ActuatorAction,
    ActuatorConfig,
    HomeAssistantActuatorAgent,
)
from wactorz.core.mqtt import mqtt_client

from .conftest import until

pytestmark = [pytest.mark.real_mqtt_client, pytest.mark.timeout(180)]


class _HomeAssistant:
    """Authenticate local clients and acknowledge service calls without operating devices."""

    def __init__(self) -> None:
        self.connections = 0
        self.calls: list[dict[str, Any]] = []

    async def handler(self, socket: Any) -> None:
        try:
            await socket.send(json.dumps({"type": "auth_required"}))
            await socket.recv()
            await socket.send(json.dumps({"type": "auth_ok"}))
            self.connections += 1
            async for raw in socket:
                command = json.loads(raw)
                if command["type"] == "call_service":
                    self.calls.append(command)
                await socket.send(
                    json.dumps(
                        {"type": "result", "id": command["id"], "success": True, "result": None}
                    )
                )
        except websockets.ConnectionClosed:
            pass


@pytest.mark.parametrize("abrupt", [False, True], ids=["clean-close", "connection-lost"])
async def test_mqtt_triggers_act_after_repeated_home_assistant_outages(
    broker: tuple[str, int], tmp_path: Path, caplog: pytest.LogCaptureFixture, abrupt: bool
) -> None:
    ha = _HomeAssistant()
    server = await websockets.serve(ha.handler, "127.0.0.1", 0)
    port = next(iter(server.sockets)).getsockname()[1]
    host, mqtt_port = broker
    suffix = uuid.uuid4().hex
    topic = f"custom/test/ha-reconnect/{suffix}"
    agent = HomeAssistantActuatorAgent(
        config=ActuatorConfig(
            automation_id=suffix,
            description="A service call acknowledged by the local HA test server.",
            mqtt_topics=[topic],
            actions=[
                ActuatorAction("light", "turn_on", "light.test", {"brightness": "$payload.level"})
            ],
            cooldown_seconds=0,
        ),
        persistence_dir=str(tmp_path),
    )
    agent.ha_ws_url, agent.ha_token = f"ws://127.0.0.1:{port}/api/websocket", "test-token"
    agent._mqtt_broker, agent._mqtt_port = host, mqtt_port
    caplog.set_level(logging.INFO, logger=module.__name__)
    try:
        async with mqtt_client(host, mqtt_port) as publisher:
            agent._mqtt_client = publisher
            try:
                await agent.start()
                await until(agent._ws_ready.is_set, "actuator connected to HA")
                await until(
                    lambda: any(
                        record.getMessage().startswith(f"[{agent.name}] subscribed to ")
                        for record in caplog.records
                    ),
                    "actuator subscribed to its MQTT trigger",
                )
                await publisher.publish(topic, json.dumps({"level": 1}), qos=1)
                await until(lambda: agent._actuations_count == 1, "initial actuation completed")

                for completed in (1, 2):
                    old_client = agent._ha
                    if abrupt:
                        for connection in list(server.connections):
                            connection.transport.abort()
                    server.close()
                    await server.wait_closed()
                    await until(
                        lambda: agent._ha is None and not agent._ws_ready.is_set(),
                        "dead HA client forgotten before retry",
                    )
                    server = await websockets.serve(ha.handler, "127.0.0.1", port)
                    await until(
                        lambda: ha.connections == completed + 1 and agent._ws_ready.is_set(),
                        "actuator reauthenticated after the outage",
                    )
                    assert agent._ha is not old_client
                    assert len(ha.calls) == completed
                    await publisher.publish(topic, json.dumps({"level": completed + 1}), qos=1)
                    await until(
                        lambda: agent._actuations_count == completed + 1,
                        "next MQTT actuation completed on the new connection",
                    )
                assert [call["service_data"] for call in ha.calls] == [
                    {"entity_id": "light.test", "brightness": level} for level in (1, 2, 3)
                ]
                assert agent.metrics.tasks_failed == 0
            finally:
                await agent.stop()
            assert agent._tasks == []
            assert agent._ha is None
            assert not agent._ws_ready.is_set()
    finally:
        server.close()
        await server.wait_closed()
