"""What a node adds to any agent it runs, whatever the agent's own class.

A node runs two kinds of agent: generated code (`NodeAgent`, a
`DynamicAgent`), and a library agent -- a function declared with
``@wactorz.agent`` or an `Actor` subclass -- built from a package installed on
the node. Both need the same things from the node, and none of them from their
author, who need not know where the agent will run:

- **Where its memory lives.** JSON under the node's state directory, so it
  survives a reboot and can be shipped to another machine by a migration; a
  model, an array or bytes is kept as a blob beside the file.
- **How it is commanded.** A stop or a delete goes through the node, which owns
  the agent's registry entry, its state file and its retained topics.
- **How it is asked to do something.** A task arrives on an MQTT topic rather
  than in a mailbox, and the reply goes back the same way.

`NodeHosted` is put in front of the agent's own class, so its methods win over
the ones an agent on main uses for the same things. `host_on` binds it to the
node before it starts.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..agents.function_agent import FunctionAgent
from ..core import agent_reset
from ..core.actor import Actor, ReplyError, ask_through, forbidden
from ..core.state_codec import decode_state
from .state import JsonState, agent_state, flush_states

if TYPE_CHECKING:
    from .runner import NodeRunner

    # Typing-only base: it states what the mixin reads of the actor it is put
    # in front of, and is gone at runtime, where the actor's own class follows.
    _Base = Actor
else:
    _Base = object

logger = logging.getLogger(__name__)


class NodeHosted(_Base):
    """The node's part of an agent running on it; see the module docstring."""

    _runner: "NodeRunner"
    _config: dict[str, Any]
    _state_file: JsonState

    def host_on(self, runner: "NodeRunner", config: dict[str, Any], state_dir: str | Path) -> None:
        """Make this agent the node's: its broker, its memory, its identity.

        Called once, after the agent is built and before it starts, since the
        agent's own constructor knows nothing of nodes. Takes the state it starts
        with, from a migration or from disk, so its first ``recall`` finds it.
        """
        self._runner = runner
        self._config = config
        #: What `agent.node` reports, and what every heartbeat carries.
        self._node = runner.node_name
        self._mqtt_broker = runner.broker
        self._mqtt_port = runner.port
        #: The runner's publish queue, standing in for an aiomqtt client.
        self._mqtt_client = runner.publisher
        self._state_file = agent_state(Path(state_dir), self.name)
        self._apply_initial_state(config)

    # ── State ─────────────────────────────────────────────────────────────────

    def _apply_initial_state(self, config: dict) -> None:
        """Take the state this agent starts with, from a migration or from disk.

        Two cases have to be told apart. A plain start or restart has no
        ``_initial_state``: the agent ran here before, and whatever is on disk
        is its memory. A migration arrival has one, and it is authoritative --
        the source was running this agent moments ago, so any file here is from
        an older incarnation that lived here before it moved away, and letting
        it win would bring back memory the agent no longer has.
        """
        initial = config.pop("_initial_state", None)
        if not (initial and isinstance(initial, dict)):
            self._persistent_state = self._state_file.load()
            return
        if self._state_file.path.exists():
            logger.info(
                "[%s] Migration: overwriting the stale state file at %s with %s key(s) "
                "shipped from the source",
                self.name,
                self._state_file.path,
                len(initial),
            )
            self._state_file.delete()
        # As it travelled: a date or a set in its marked JSON form.
        self._persistent_state = decode_state(initial)
        self._state_file.save(self._persistent_state)
        logger.info(
            "[%s] Restored %s state key(s) from migration: %s",
            self.name,
            len(initial),
            list(initial),
        )

    async def _load_persistent_state(self) -> None:
        """Already read by `host_on`, from the node's JSON file."""

    async def _save_persistent_state(self) -> None:
        """Called on stop: once it returns, the file holds what the agent remembers.

        Not left to the moment a save otherwise waits. A migration ships the
        state and keeps this file to roll back to, and a stop is rare enough
        to wait for the disk.
        """
        self._state_file.save(self._persistent_state, changed=())
        flush_states()

    def persist(self, key: str, value: Any) -> None:
        self._persistent_state[key] = value
        self._state_file.save(self._persistent_state, changed=(key,))

    def recall(self, key: str, default: Any = None) -> Any:
        return self._persistent_state.get(key, default)

    def _own_state(self) -> dict[str, Any]:
        """Everything in the node's state file is the agent's own."""
        return dict(self._persistent_state)

    def _replace_own_state(self, old: dict[str, Any], new: dict[str, Any]) -> None:
        self._persistent_state = dict(new)
        self._state_file.save(self._persistent_state)

    def delete_state(self) -> bool:
        """Remove this agent's memory for good. True when there was any.

        In-memory state goes first, so a save still in flight -- from a
        supervisor restart, or the agent's own stop -- cannot write the file
        back after it is gone.
        """
        self._persistent_state = {}
        removed = self._state_file.delete()
        # The actor's own directory, made at construction for the pickle store
        # an agent on a node does not use. `rmdir`, so anything unexpectedly
        # inside it survives to be looked at.
        try:
            self._persistence_dir.rmdir()
        except OSError:
            logger.debug("[%s] Left %s in place", self.name, self._persistence_dir)
        return removed

    def reset(self, scope: str) -> None:
        """Clear one of `agent_reset.LIVE_SCOPES` from this agent: in memory, and in its file.

        The node keeps the only copy of an agent's memory here, so a reset main
        makes of its own records leaves this one standing unless the node makes
        it too. A reset of the whole state removes the file, as a delete does; the
        others write the file again without what they cleared.
        """
        if scope == "state":
            self.delete_state()
            return
        agent_reset.reset_live(self, scope)
        self._state_file.save(self._persistent_state)

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def node(self) -> str:
        return self._node

    async def _send_manifest(self, manifest: dict[str, Any]) -> None:
        """Send ``manifest`` naming this node, and that a task reaches it on the node's own topic.

        Whatever kind of agent this is, so a node that resolves an address from
        the manifests it hears (`wactorz.core.agent_directory`) knows where every
        agent of a node of this release runs, and how to send to it.
        """
        await super()._send_manifest({**manifest, "node": self._node, "tasks_by_node": True})

    # ── Tasks, which arrive on a topic rather than in a mailbox ───────────────

    async def run_task(self, payload: Any) -> Any:
        """Answer a task that arrived on this agent's task topic.

        A function agent's function is called with it, as a task in its mailbox
        would be; any other actor is sent it as a task, through its own
        mailbox, and its reply returned. A value that is not a dict comes back
        as ``{"result": value}``, as an answer from main would.
        """
        try:
            if isinstance(self, FunctionAgent):
                result = await self.call(payload)
            else:
                result = await ask_through(self, self.name, payload)
        except ReplyError as exc:
            return {"error": str(exc), "agent": self.name}
        except Exception as exc:
            logger.exception("[%s] a task from its topic failed", self.name)
            return {"error": str(exc), "agent": self.name}
        return result if isinstance(result, dict) else {"result": result}

    def deliver_reply(self, reply_topic: str, data: Any) -> bool:
        """Resolve whatever call was waiting on ``reply_topic``. True if one was."""
        future = self._result_futures.get(reply_topic)
        if future is not None and not future.done():
            future.set_result(data)
            return True
        return False

    # ── Commands, which main may send straight to the agent ───────────────────

    async def apply_command(self, command: str) -> bool:
        """Carry out a command, through the node that is running this agent.

        A stop or a delete may arrive on `agents/<id>/commands` without passing
        through the node's control plane. Done by the agent alone, a stop would
        leave it in the node's registry -- still in its heartbeat, still refusing
        a respawn without `replace` -- and a delete would leave its state file
        and retained topics behind, which is what makes a deleted agent come
        back. Both are routed to the node, which owns that bookkeeping.
        """
        if command in ("stop", "delete") and not forbidden(
            command, protected=self.protected, essential=self.essential
        ):
            await self._runner.stop_agent(self.name, delete=command == "delete")
            return True
        return await super().apply_command(command)
