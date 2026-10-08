"""Asked for an agent, main starts one; it gets a card, and it answers for itself."""

import json
import time

from harness import backend, browser, waiting
from harness.probe import Broker
from harness.run import Run

from wactorz.core.actor import derive_actor_id


def test_main_starts_the_agent_it_was_asked_for(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.say("please start a greeter", to="main")

    # What the model wrote, the block that asks for the agent included, and
    # then main's own account of what it did with it.
    dashboard.expect_like(
        "main",
        r"I'll set that up\.\n<spawn>\n\{.*\}\n</spawn>\n\n"
        r"ℹ️ Spawned 'greeter' — will auto-restore on restart",
    )
    dashboard.wait_for_card("greeter")
    waiting.becomes_and_stays(
        lambda: app.rest.state_of("greeter") == "running",
        what="greeter to be running, and to stay so",
    )


def test_the_new_agent_answers_for_itself(dashboard: browser.Dashboard) -> None:
    dashboard.say("good morning", to="greeter")

    dashboard.expect("greeter", "Good morning to you too.")


def test_main_knows_nothing_of_that_conversation(dashboard: browser.Dashboard) -> None:
    # Each agent has a thread of its own: what was said to one is not shown as
    # said to another.
    assert all(said.sender == "greeter" for said in dashboard.read_thread_of("greeter").said())
    assert "good morning" not in dashboard.read_thread_of("main").sent()


def test_an_agent_that_ends_itself_leaves_the_dashboard_once(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    # The ending a planner and a one-off actuator have: the agent unregisters,
    # stops and withdraws its manifest. Its card must go once and stay gone; one
    # that goes and comes back in between is the blink this guards against.
    dashboard.say("start a finisher here", to="main")
    dashboard.expect_like(
        "main",
        r"Starting it\.\n<spawn>\n\{.*\}\n</spawn>\n\n"
        r"ℹ️ Spawned 'finisher' — will auto-restore on restart",
    )
    dashboard.watch_card("finisher")

    waiting.until(
        lambda: "removed" in dashboard.card_comings_and_goings("finisher"),
        what="the finisher's card to go once it has ended itself",
        timeout=60,
    )
    # Past the REST reconcile and the page's tombstone expiry, either of which
    # could put a card back.
    waiting.holds_for(
        lambda: dashboard.card_comings_and_goings("finisher")[-1] == "removed",
        what="the finisher's card staying gone",
        window=35,
        interval=1,
    )
    seen = dashboard.card_comings_and_goings("finisher")
    assert seen.count("removed") == 1, seen
    waiting.until(
        lambda: app.rest.state_of("finisher") is None,
        what="the server to have forgotten the finisher",
    )


def test_another_servers_agent_on_the_same_broker_keeps_its_card(
    dashboard: browser.Dashboard, run: Run
) -> None:
    # Two installs sharing a broker hear each other's agents. This server never
    # lists the other's in its REST answer, and its heartbeats carry no node, so
    # the page took it for a local agent the server had forgotten: removed on
    # every reconcile, back on the next heartbeat. A card that comes and goes.
    name = "neighbour"
    actor_id = derive_actor_id(name)

    def heartbeat(broker: Broker) -> None:
        payload = {
            "actor_id": actor_id,
            "name": name,
            "timestamp": time.time(),
            "state": "running",
            "memory_mb": 0.1,
            "task": "Publishes hello message every 5 seconds",
            "protected": False,
            "essential": False,
            "node": None,
        }
        broker.publish(f"agents/{actor_id}/heartbeat", json.dumps(payload))

    with Broker(run) as broker:
        heartbeat(broker)
        dashboard.wait_for_card(name)
        dashboard.watch_card(name)
        # Three of the page's reconciles with the server's list, heartbeating
        # every five seconds as an agent does.
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            heartbeat(broker)
            time.sleep(5)
        assert dashboard.card_comings_and_goings(name) == ["present"]

        # Its ending, as any agent's: the manifest withdrawn. The card goes, once.
        broker.publish(f"agents/{actor_id}/manifest", "", retain=True)
        waiting.until(
            lambda: dashboard.card_comings_and_goings(name)[-1:] == ["removed"],
            what="the neighbour's card to go once its manifest is withdrawn",
        )
    assert dashboard.card_comings_and_goings(name) == ["present", "removed"]
