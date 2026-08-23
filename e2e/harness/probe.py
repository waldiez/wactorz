"""The three ways this suite talks to a running system: REST, WebSocket, MQTT.

All three are synchronous. The scenarios are synchronous, Playwright's page
objects are synchronous, and a suite that mixed an event loop into that would
spend its complexity budget on plumbing rather than on what it checks. The cost
is that nothing here streams concurrently with a browser - and nothing needs to,
because the browser is a separate process.

Clients are the ordinary ones a user would reach for: `urllib` over HTTP, the
`websockets` synchronous client, `paho` for MQTT. Deliberately not the
application's own client code - a probe built from the thing under test agrees
with it about a broken contract.
"""

from __future__ import annotations

import contextlib
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from websockets.sync.client import connect

from . import broker


class HttpError(AssertionError):
    """A response the caller expected to succeed and did not."""

    def __init__(self, method: str, url: str, status: int, body: str) -> None:
        super().__init__(f"{method} {url} -> {status}: {body[:400]}")
        self.status = status
        self.body = body


@dataclass
class Response:
    status: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.body)


class Rest:
    """The REST API of one backend.

    Every method returns the parsed body and raises on a non-2xx, except `raw`,
    which hands the response back untouched. The split matters: most scenarios
    are asserting on what came back, but a handful are asserting on the status
    itself - a refusal is the behaviour under test in `a06` and `chat_target`,
    and a client that raises on it makes that awkward to say.
    """

    def __init__(self, base_url: str, *, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    # ── The two primitives ──────────────────────────────────────────────────

    def raw(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
    ) -> Response:
        """One request. Never raises for a status - only for a transport failure."""
        url = f"{self.base_url}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method=method
        )  # - http(s) only, built here
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.api_key:
            request.add_header("X-API-Key", self.api_key)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return Response(
                    status=response.status,
                    body=response.read().decode("utf-8", errors="replace"),
                    headers={k.lower(): v for k, v in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            return Response(
                status=exc.code,
                body=exc.read().decode("utf-8", errors="replace"),
                headers={k.lower(): v for k, v in exc.headers.items()},
            )

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        """One request that is expected to succeed, parsed."""
        response = self.raw(method, path, **kwargs)
        if not response.ok:
            raise HttpError(method, path, response.status, response.body)
        if not response.body.strip():
            return None
        return response.json()

    # ── Reachability ────────────────────────────────────────────────────────

    def ok(self, path: str = "/health", timeout: float = 2.0) -> bool:
        """Whether this path answers with a 2xx. For waits, so it never raises."""
        try:
            return self.raw("GET", path, timeout=timeout).ok
        except OSError:
            return False

    # ── The surface the scenarios use ───────────────────────────────────────

    def agents(self) -> list[dict[str, Any]]:
        """Every actor the system knows about, as the dashboard is told about them."""
        payload = self.call("GET", "/api/actors")
        if isinstance(payload, dict):
            return list(payload.get("actors") or payload.get("agents") or [])
        return list(payload or [])

    def agent(self, name: str) -> dict[str, Any] | None:
        """One actor by name or id, or None.

        None rather than raising, so it can
        be waited on directly: an agent that does not exist yet is the normal
        state on the way to it existing.
        """
        for entry in self.agents():
            if name in (entry.get("name"), entry.get("id"), entry.get("actor_id")):
                return entry
        return None

    def state_of(self, name: str) -> str | None:
        entry = self.agent(name)
        if entry is None:
            return None
        value = entry.get("state") or entry.get("status")
        return str(value) if value is not None else None

    def nodes(self) -> list[dict[str, Any]]:
        """The remote nodes the system has heard from.

        Read from the dashboard's own snapshot rather than a REST route, because
        there is no REST route for them - the node list reaches the browser over
        the socket and nowhere else. A scenario asserting "the node is listed" is
        asserting about what the dashboard is told, so this is the honest source.
        """
        return list(snapshot(self.base_url).get("nodes") or [])

    def node_names(self) -> set[str]:
        """Every node in the list, by the name the runner was started with."""
        return {str(n.get("node") or n.get("name")) for n in self.nodes() if isinstance(n, dict)}

    def cost(self) -> dict[str, Any]:
        return self.call("GET", "/api/cost")

    def feed(self) -> list[dict[str, Any]]:
        payload = self.call("GET", "/api/feed")
        if isinstance(payload, dict):
            return list(payload.get("events") or payload.get("feed") or [])
        return list(payload or [])

    def chat(self, message: str, *, target: str = "", timeout: float = 120.0) -> dict[str, Any]:
        """Send a chat turn and return the whole reply.

        Blocking, and given a long deadline: a real model on the demo profile is
        allowed to take its time, and the scenario asserting on the reply has
        nothing to say until it arrives.
        """
        body: dict[str, Any] = {"message": message}
        if target:
            body["target"] = target
        return self.call("POST", "/api/chat", body=body, timeout=timeout)

    def command(self, agent: str, action: str) -> Response:
        """Command to call: pause / resume / start / stop, as the dashboard's buttons send them.

        The raw response: refusing to pause `main` is a scenario's assertion, and
        it is a status code.
        """
        return self.raw("POST", f"/api/actors/{urllib.parse.quote(agent)}/{action}")

    def stop(self, agent: str) -> None:
        """Stop an agent, which only the socket can do.

        There is no `POST /actors/{id}/stop`: start, pause and resume have REST
        routes and stop does not, so the dashboard's stop button sends a socket
        frame. This goes the same way, because a harness that reached for a route
        the product does not have would be testing an API nobody uses.

        Nothing is returned, because nothing is sent back. A refused command
        broadcasts no patch at all, so "did it work" is a question about the
        agent's state afterwards - which is what the scenarios wait on.
        """
        self.socket_command(agent, "stop")

    def actor_id(self, agent: str) -> str:
        """The uuid the dashboard knows this agent by.

        Every card carries it, and it is what the socket expects. Looked up by
        name here so scenarios can keep saying names.
        """
        entry = self.agent(agent)
        if entry is None:
            raise AssertionError(f"no agent named {agent!r} to address")
        return str(entry.get("id") or entry.get("actor_id") or agent)

    def socket_command(self, agent: str, command: str) -> None:
        """One lifecycle command over the dashboard socket, sent as the page sends it.

        By id, not by name. The command *runs* either way - the dispatcher
        resolves both - but the server then records the new state under the id it
        was given, so a command addressed by name executes and is never reflected.
        With the broker up the next heartbeat papers over the difference; with the
        broker down nothing does, and the agent stays reported as running forever.
        """
        with websocket(self.base_url) as sock:
            sock.send({"type": "command", "agent_id": self.actor_id(agent), "command": command})
            # Read until the server broadcasts the state it now believes in. This
            # is not a wait for the command to have taken effect - a refusal
            # broadcasts nothing and simply times out here, which is fine, since
            # the caller asserts on state either way. It is a wait for the frame
            # to have been *processed*, so closing the socket cannot discard it.
            with contextlib.suppress(Exception):
                sock.next_of_type("patch", timeout=10.0, limit=50)

    def delete(self, agent: str) -> Response:
        return self.raw("DELETE", f"/api/actors/{urllib.parse.quote(agent)}")

    def metrics(self, agent: str) -> dict[str, Any]:
        return self.call("GET", f"/api/actors/{urllib.parse.quote(agent)}/metrics")

    def capture(self, agent: str, *fields: str) -> dict[str, Any]:
        """The named counters, right now, for comparing against later.

        The alternative a scenario reaches for is a hardcoded number, which
        asserts what the value was on the machine the scenario was written on.

        Draws from the agent's own metrics and from system spend together, so a
        scenario can capture `message_count` and `cost_usd` in one call without
        knowing which endpoint reports which. A name neither of them reports is
        an error naming what they do, because a silently-absent counter compares
        equal to itself forever.
        """
        metrics = self.metrics(agent)
        spend = self.cost()
        source: dict[str, Any] = {
            "message_count": metrics.get("messages_processed"),
            "messages_processed": metrics.get("messages_processed"),
            "agent_cost_usd": metrics.get("cost_usd"),
            "cost_usd": spend.get("spend_usd"),
            "spend_usd": spend.get("spend_usd"),
        }
        missing = [name for name in fields if name not in source]
        if missing:
            available = ", ".join(sorted(source))
            raise AssertionError(
                f"cannot capture {', '.join(missing)} for {agent!r} - "
                f"what is available is: {available}"
            )
        return {name: source[name] for name in fields}


class Socket:
    """The dashboard's WebSocket, as the browser opens it.

    Frames are read on demand rather than in a background thread: a scenario
    waiting for a frame is waiting for it, and a queue filled by a thread would
    add a second source of timing to a suite whose whole rule is that there is
    only one.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def send(self, payload: dict[str, Any]) -> None:
        self._connection.send(json.dumps(payload))

    def next_frame(self, timeout: float = 30.0) -> dict[str, Any]:
        return json.loads(self._connection.recv(timeout=timeout))

    def next_of_type(
        self, wanted: str, *, timeout: float = 30.0, limit: int = 200
    ) -> dict[str, Any]:
        """The next frame of this type, ignoring the traffic in between.

        The socket carries snapshots and patches continuously, so a scenario
        waiting for one kind of frame has to read past the others. `limit` keeps
        a stream of the wrong frames from looking like a hang.
        """
        seen: list[str] = []
        for _ in range(limit):
            frame = self.next_frame(timeout=timeout)
            if frame.get("type") == wanted:
                return frame
            seen.append(str(frame.get("type")))
        raise AssertionError(
            f"no {wanted!r} frame in {limit} frames; what arrived was: {', '.join(seen[:20])}"
        )


def snapshot(base_url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """The dashboard's `full_snapshot` state, opened and closed in one call.

    The socket sends it unprompted on connect, so this is a read rather than a
    subscription - the right shape for a condition being polled, where holding a
    connection open across the poll would be a second thing that can fail.
    """
    with websocket(base_url, timeout=timeout) as sock:
        frame = sock.next_of_type("full_snapshot", timeout=timeout)
    state = frame.get("state")
    return state if isinstance(state, dict) else {}


@contextlib.contextmanager
def websocket(base_url: str, *, timeout: float = 15.0) -> Generator[Socket]:
    """Open the dashboard socket for the duration of a `with` block."""
    url = base_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/ws"
    context = ssl.create_default_context() if url.startswith("wss://") else None
    connection = connect(url, open_timeout=timeout, ssl=context)
    try:
        yield Socket(connection)
    finally:
        connection.close()


class Broker:
    """A plain MQTT client, for watching what the system publishes.

    The suite's way of asking "did that actually cross the broker" rather than
    "did the API say it had". They are different claims, and the second one is
    the one that stayed true through a regression where nothing was published.
    """

    def __init__(self) -> None:
        self._client = mqtt.Client(CallbackAPIVersion.VERSION2)
        if broker.USERNAME:
            self._client.username_pw_set(broker.USERNAME, broker.PASSWORD)
        self.messages: list[tuple[str, str]] = []
        self._client.on_message = lambda _c, _u, msg: self.messages.append(
            (msg.topic, msg.payload.decode("utf-8", errors="replace"))
        )

    def __enter__(self) -> Broker:
        self._client.connect(broker.HOST, broker.PORT, keepalive=30)
        self._client.loop_start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._client.loop_stop()
        with contextlib.suppress(Exception):
            self._client.disconnect()

    def subscribe(self, topic: str) -> None:
        self._client.subscribe(topic, qos=1)

    def publish(self, topic: str, payload: str, *, retain: bool = False) -> None:
        self._client.publish(topic, payload, qos=1, retain=retain).wait_for_publish(timeout=10)

    def matching(self, needle: str) -> list[tuple[str, str]]:
        """Every message received so far whose topic or payload contains `needle`."""
        return [(t, p) for t, p in list(self.messages) if needle in t or needle in p]
