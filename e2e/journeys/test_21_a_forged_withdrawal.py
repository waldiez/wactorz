# SPDX-License-Identifier: Apache-2.0
"""A withdrawal nobody running sent does not take an agent off the dashboard.

An agent that ends withdraws its manifest: an empty retained payload on its
manifest topic, which anything that can publish under ``agents/`` can send. Main
weighs one by where the agent runs, and refuses it for an agent running in its
own process -- main itself included -- putting back the manifest the empty
payload took off the broker. The dashboard follows main: main's card stays, on
the page and in what the server sends a page that opens now.

The forgery is published with the server's own broker account, which may write
anywhere, as a node's account may under ``agents/``.
"""

import json

from harness import backend, browser, waiting
from harness.probe import Broker, snapshot, websocket
from harness.run import Run

#: How long main has to put its manifest back on the broker.
RESTORED_WITHIN_S = 30.0

#: A log line published after the forgery: once the server has relayed it, it
#: has weighed the forgery too, since it reads one connection in order.
MARKER = "e2e: after the forged withdrawal"


def _manifests(broker: Broker, topic: str) -> list[str]:
    return [payload for got, payload in list(broker.messages) if got == topic]


def test_a_forged_withdrawal_of_main_leaves_its_card_and_its_manifest(
    dashboard: browser.Dashboard, app: backend.Backend, run: Run
) -> None:
    main_id = app.rest.actor_id("main")
    topic = f"agents/{main_id}/manifest"
    logs = f"agents/{main_id}/logs"
    dashboard.wait_for_card("main")
    dashboard.watch_card("main")
    with websocket(app.url, api_key=run.api_key) as sock, Broker(run) as broker:
        broker.subscribe(topic)
        waiting.until(lambda: any(_manifests(broker, topic)), what="main's retained manifest")
        broker.messages.clear()

        broker.publish(topic, "", retain=True)
        broker.publish(logs, json.dumps({"type": "log", "message": MARKER}))

        deleted: list[str] = []
        for _ in range(400):
            frame = sock.next_frame(timeout=30.0)
            if frame.get("type") == "delete_agent":
                deleted.append(str(frame.get("agent_id")))
            if frame.get("type") == "server_event" and frame.get("topic") == logs:
                break
        else:
            raise AssertionError("the marker never came back over the socket")
        assert main_id not in deleted

        waiting.until(
            lambda: any(_manifests(broker, topic)),
            what="main putting its manifest back on the broker",
            timeout=RESTORED_WITHIN_S,
        )

    agents = snapshot(app.url, api_key=run.api_key).get("agents", [])
    assert any(agent.get("agent_id") == main_id for agent in agents)
    assert dashboard.card_comings_and_goings("main") == ["present"]
