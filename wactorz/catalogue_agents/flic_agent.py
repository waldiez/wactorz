"""FlicAgent — Flic buttons as MQTT triggers.

Pairs buttons over Bluetooth from chat (`@flic pair`, `@flic scan`,...) and turns
each press into a topic of its own, so a button is wired to an action the same
way any other trigger is.

A pairing key is a secret, so the buttons file is written at 0600 beside the
agent's state rather than through `persist()`, whose store is not secret.
"""

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import re
import sys
import time
from collections.abc import Collection, Iterable
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..core.actor import Actor, Message, MessageType
from ..core.atomic_io import write_private_json

if TYPE_CHECKING:
    from pyflic_ble import FlicClient  # pyright: ignore[reportMissingImports]


LOG = logging.getLogger(__name__)
TOPIC_ROOT = "custom/flic"
GESTURES = ("click", "double_click", "hold")

#: What a Flic 2/Duo advertises, and a Twist next to it: what a scan looks for.
FLIC_SERVICE_UUID = "00420000-8f59-4420-870d-84f3b617e493"
TWIST_SERVICE_UUID = "00c90000-2cbd-4f2a-a725-5ccd960ffb7d"
SCAN_TIMEOUT_S = 15.0
CONNECT_TIMEOUT_S = 45.0
PAIRING_TIMEOUT_S = 120.0
#: How long `pair` keeps looking for a button to enter pairing mode, once it has
#: told the person to hold one down. One scan covers the whole wait.
PAIR_WAIT_S = 45.0
PAIR_INSTRUCTIONS = (
    "Hold the Flic button down for about 7 seconds to put it in pairing mode. "
    f"Looking for it for the next {PAIR_WAIT_S:.0f} seconds..."
)
STOP_TIMEOUT_S = 20.0
#: How long to wait before the one retry of a scan BlueZ would not start.
SCAN_RETRY_PAUSE_S = 2.0
#: How long to look for one known button before starting its session, and how
#: often to look again for a button that is out of range, backing off to the cap.
FIND_TIMEOUT_S = 10.0
FIND_INTERVAL_S = 30.0
FIND_MAX_INTERVAL_S = 300.0
#: The kind a connection change travels under on the press queue, beside the
#: gestures. Not a gesture, so it can never be mistaken for a press.
CONNECTION_EVENT = "_connection"
MISSING_LIB = (
    "pyflic-ble is not installed — install it with `pip install wactorz[flic]`."
    if sys.version_info >= (3, 12)
    else (
        "Flic buttons need Python 3.12 or newer; this interpreter is "
        f"{sys.version_info.major}.{sys.version_info.minor}, which pyflic-ble does not support."
    )
)

_FILLER = {"a", "an", "the", "my", "button", "as", "to", "called", "named", "please", "it"}

#: Words that separate a rename's old name from its new one, so either can be
#: more than one word: "rename flic-1 to Lamp Flic".
_RENAME_SEPARATORS = {"to", "as", "into"}


# https://github.com/50ButtonsEach/pyflic-ble#usage
@dataclass(frozen=True)
class FlicButton:
    """One paired button: how to reach it, and what proves it is ours.

    Frozen because a pairing is not edited — a button that changes is paired
    again, and a rename replaces the record. `pairing_key` is what a later
    connection verifies with, and is the reason the file holding these is
    private.

    Only what survives a restart is here. The `BLEDevice` a connection wants is
    found by scanning, so it is never stored.

    `name` is whatever the person called the button, spaces and capitals kept.
    Two derived identities sit beside it: `slug`, the name as chat addresses
    it, and `key`, which names the button in its topics and never changes.
    """

    name: str
    address: str
    pairing_id: int
    pairing_key: bytes
    serial_number: str
    sig_bits: int
    device_type: Literal["flic2", "duo", "twist"] = "flic2"
    #: The battery reading taken at pairing, raw as the button reports it: an
    #: ADC value for a Flic 2 or Duo, millivolts for a Twist. `battery_volts`
    #: turns it into something a person can read.
    battery: int = 0
    button_uuid: str = ""
    firmware_version: int = 0
    twist_push_mode: Literal["default", "continuous", "selector"] = "default"

    @property
    def key(self) -> str:
        """The segment that identifies this button in its topics, for good.

        The serial number, printed on the button and unique to it, so a rename
        never moves a topic that something has been wired to. The address
        stands in for a record that has no serial.
        """
        return slug(self.serial_number) or slug(self.address)

    @property
    def slug(self) -> str:
        """The name as chat addresses it: "Lamp Flic" answers to `lamp-flic`.

        Derived rather than stored, so it cannot drift from the name.
        """
        return slug(self.name)

    def to_dict(self) -> dict[str, Any]:
        """The button as JSON-safe fields, for the buttons file.

        JSON has no bytes and a pairing key is not text, so the key crosses as
        base64 and `from_dict` turns it back.
        """
        mapping = asdict(self)
        mapping["pairing_key"] = base64.b64encode(self.pairing_key).decode("ascii")
        return mapping

    @classmethod
    def from_dict(cls, mapping: dict[str, Any]) -> "FlicButton":
        """A button read back from the buttons file.

        Raises whatever the dataclass raises when the mapping is not one — the
        caller decides whether a single unreadable record is worth reporting.
        """
        fields = dict(mapping)
        key = fields.get("pairing_key", "")
        if isinstance(key, str):
            fields["pairing_key"] = base64.b64decode(key, validate=True)
        return cls(**fields)


class FlicAgentCommand(str, Enum):
    """What the agent can be asked to do.

    An enum rather than free strings so a command that no longer exists fails
    where it is named, not inside the handler that never receives it.
    """

    HELP = "help"
    SCAN = "scan"
    PAIR = "pair"
    LIST = "list"
    RENAME = "rename"
    LISTEN = "listen"
    STOP = "stop"
    STATUS = "status"
    FORGET = "forget"
    LATE = "late"


#: Commands that change the buttons, their clients or the stored settings,
#: which take turns.
CHANGES_BUTTONS = frozenset(
    {
        FlicAgentCommand.PAIR,
        FlicAgentCommand.RENAME,
        FlicAgentCommand.FORGET,
        FlicAgentCommand.LISTEN,
        FlicAgentCommand.STOP,
        FlicAgentCommand.LATE,
    }
)

#: The words that reach each command. A press should do the same thing however
#: it was asked for, so the parse is a table rather than a model.
COMMAND_WORDS: dict[str, FlicAgentCommand] = {
    "help": FlicAgentCommand.HELP,
    "commands": FlicAgentCommand.HELP,
    "scan": FlicAgentCommand.SCAN,
    "search": FlicAgentCommand.SCAN,
    "discover": FlicAgentCommand.SCAN,
    "find": FlicAgentCommand.SCAN,
    "pair": FlicAgentCommand.PAIR,
    "add": FlicAgentCommand.PAIR,
    "list": FlicAgentCommand.LIST,
    "buttons": FlicAgentCommand.LIST,
    "paired": FlicAgentCommand.LIST,
    "rename": FlicAgentCommand.RENAME,
    "forget": FlicAgentCommand.FORGET,
    "remove": FlicAgentCommand.FORGET,
    "unpair": FlicAgentCommand.FORGET,
    "delete": FlicAgentCommand.FORGET,
    "listen": FlicAgentCommand.LISTEN,
    "resume": FlicAgentCommand.LISTEN,
    "start": FlicAgentCommand.LISTEN,
    "stop": FlicAgentCommand.STOP,
    "pause": FlicAgentCommand.STOP,
    "status": FlicAgentCommand.STATUS,
    "late": FlicAgentCommand.LATE,
}

#: The words that turn late presses on or off, after "late".
_SWITCH_WORDS = {
    "on": "on",
    "yes": "on",
    "true": "on",
    "enable": "on",
    "off": "off",
    "no": "off",
    "false": "off",
    "disable": "off",
}

HELP_TEXT = """Flic buttons:
  scan                  buttons in range, and which are already paired
  pair [name]           hold a button down for 7s, then pair it
  list                  paired buttons, with battery and last press
  rename <old> to <new> rename a button; its topics stay the same
  forget <name>         unpair a button and take back its topics
  listen / stop         start or stop listening, keeping the pairings
  late presses on|off   publish presses made while a button was disconnected
  status                what is installed, paired and connected

Every press is published on custom/flic/<serial>/<click|double_click|hold>,
so renaming a button never breaks what is wired to it. `list` shows each
button's topic.

After a restart, press each button once: a disconnected Flic sleeps until it
is pressed. That press only wakes it, unless late presses are on: then it is
published when the button connects, up to a minute late, marked "late"."""


class FlicAgent(Actor):
    """Pairs Flic buttons and publishes their presses.

    Every command is parsed deterministically, so a button does the same thing
    each time and asking costs nothing.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "flic")
        # Every spawn passes one; this agent parses commands and never asks a model.
        kwargs.pop("llm_provider", None)
        super().__init__(**kwargs)
        #: Whether a press the button stored while disconnected, and delivers
        #: on reconnecting, is published. Set by `late presses on|off` and kept
        #: in the buttons file. Off by default: such a press arrives up to a
        #: minute late, and a lamp switching then is usually worse than a
        #: press that did nothing.
        self.late_presses = False
        self._buttons_json = self._persistence_dir / "buttons.json"
        self._known_buttons: list[FlicButton] = []
        self._clients: dict[str, FlicClient] = {}
        self._last_press: dict[str, float] = {}
        self._listening = False
        self._checked_lib = False
        self._client_cls: type[FlicClient] | None = None
        #: Turns a stored twist mode into the library's enum. Plain `str` until
        #: the library is found, which is what the library compares against.
        self._twist_mode: Any = str
        self._presses: asyncio.Queue[tuple[str, str, float, dict[str, Any]]] = asyncio.Queue()
        self._pump: asyncio.Task[None] | None = None
        #: Looks for buttons whose session is down and hands the library a fresh
        #: device for each; see `_find_missing`.
        self._finder: asyncio.Task[None] | None = None
        #: BlueZ keeps one discovery per client. A scan started while another is
        #: stopping gets "No discovery started" when it stops its own, so every
        #: scan and lookup here takes turns.
        self._scan_lock = asyncio.Lock()
        #: Whether the background search's last scan failed, so the failure is
        #: logged when it starts and when it ends rather than every round.
        self._scan_failing = False
        #: Chat reaches `chat()` directly rather than through the mailbox, so
        #: two commands can run at once. Those that change the set of buttons
        #: or clients take turns, or two pairings pick the same button.
        self._command_lock = asyncio.Lock()
        #: The command holding `_command_lock`, so one that has to wait for it
        #: can say what it is waiting for.
        self._busy_with: FlicAgentCommand | None = None
        #: The latest battery voltage each button reported on connecting.
        self._battery_volts: dict[str, float] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Set by `stop`, and kept in the buttons file, so a restart does not
        #: start listening to buttons someone asked to be left alone.
        self._paused = False
        #: Kinds of event seen that this agent does not publish, so each is
        #: mentioned once rather than every time one arrives.
        self._unpublished: set[str] = set()

    async def on_start(self) -> None:
        """Load the paired buttons, start listening to them, and announce.

        Nothing here waits on the radio. `Actor.start` runs this before the
        message loop and heartbeat exist, and connecting button by button can
        take minutes when some are away. The buttons are handed to the
        background search instead, which connects each as it is found.
        """
        stored = await self._restore()
        self._known_buttons = flic_buttons_from_store(stored)
        self._paused = not listening_from_store(stored)
        self.late_presses = late_presses_from_store(stored)
        self._loop = asyncio.get_running_loop()
        if self._have_lib():
            self._pump = asyncio.create_task(self._publish_presses())
            if self._known_buttons and not self._paused:
                self._watch_all()
        else:
            LOG.warning("[%s] %s", self.name, MISSING_LIB)
        await self._announce()

    async def on_stop(self) -> None:
        """Put every button down before the agent goes."""
        # Shutting down is not someone asking to stop: the next start listens.
        await self._stop(remember=False)
        pump, self._pump = self._pump, None
        if pump:
            pump.cancel()

    async def on_delete(self) -> None:
        """Remove what a delete should not leave behind: retained state, and the keys.

        Deleting an agent promises to leave no trace of it, and the store purge
        that follows knows nothing of the buttons file. Stopping first, because
        stopping publishes "disconnected", which would otherwise be the last word
        on each button's retained topic.
        """
        async with self._command_lock:
            await self._stop(remember=False)
            for button in self._known_buttons:
                await self._clear_retained(button)
            self._known_buttons = []
            self._battery_volts.clear()
            try:
                await asyncio.to_thread(self._buttons_json.unlink, missing_ok=True)
            except OSError:
                LOG.warning(
                    "[%s] Could not delete %s", self.name, self._buttons_json, exc_info=True
                )

    def _watch_all(self) -> None:
        """Listen to every paired button, leaving the connecting to the search."""
        self._listening = True
        for button in self._known_buttons:
            if button.key not in self._clients:
                self._clients[button.key] = self._new_client(button, None)
        self._ensure_finder()

    async def _announce(self) -> None:
        """Publish what this agent answers to and what its buttons publish.

        Called again whenever pairing, renaming or forgetting changes the set of
        topics. The manifest is retained, so one left alone keeps offering the
        planner topics no button publishes.
        """
        ready = self._have_lib()
        await self.publish_manifest(
            description=(
                "Pairs Flic buttons over Bluetooth and publishes each press as its "
                "own MQTT topic, so a physical button can trigger anything that can "
                "be wired to a topic."
                + (button_directory(self._known_buttons) if ready else "")
                + ("" if ready else f" Not usable as installed: {MISSING_LIB}")
            ),
            publishes=gesture_topics(self._known_buttons) if ready else [],
            capabilities=[
                "flic",
                "button",
                "bluetooth",
                "ble",
                "physical_trigger",
                "event_source",
            ],
            input_schema={
                "action": (
                    "str - help | scan | pair | list | rename | listen | stop | status "
                    "| forget | late"
                ),
                "name": "str - button to act on, or the name to give a new pairing",
                "new_name": "str - replacement name, for rename",
                "value": "str - on | off, for late",
            },
            output_schema={
                "button": "str - the button that was pressed",
                "serial": "str - its serial number",
                "gesture": "str - click | double_click | hold",
                "at": "float - unix epoch when the host received the press",
                "late": (
                    "bool - present, and true, when the press was made while the "
                    "button was disconnected and arrived when it reconnected"
                ),
            },
        )

    async def chat(self, message: str) -> str:
        """Answer a person addressing the agent as `@flic …`."""
        command, arguments = parse_command(message)
        return await self._handle_cmd(command, **arguments)

    async def handle_message(self, msg: Message) -> None:
        """Handle a message no default handler claimed.

        START, STOP, STATUS_REQUEST and HEARTBEAT never arrive here: `Actor`
        routes those to its own handlers and only falls through to this for the
        rest.
        """
        if msg.type != MessageType.TASK:
            return

        raw = msg.payload
        arguments: dict[str, str] = {}
        understood = True
        if isinstance(raw, dict):
            action = str(raw.get("action") or "").strip()
            if action:
                understood = action.lower() in COMMAND_WORDS
                command = COMMAND_WORDS.get(action.lower(), FlicAgentCommand.HELP)
                arguments = {
                    key: str(raw[key])
                    for key in ("name", "new_name", "value")
                    if raw.get(key) is not None
                }
            else:
                text = raw.get("text") or raw.get("content") or raw.get("query") or ""
                command, arguments = parse_command(str(text))
        else:
            command, arguments = parse_command(str(raw or ""))

        result: dict[str, Any] = {
            # An action nobody knows gets the help text back, and says so: a
            # caller told "ok" would take the help for the thing it asked.
            "ok": understood,
            "action": command.value,
            "result": await self._handle_cmd(command, **arguments),
        }
        if isinstance(raw, dict):
            for key in ("_task_id", "task"):
                if key in raw:
                    result[key] = raw[key]
        target = msg.reply_to or msg.sender_id
        if target:
            await self.send(target, MessageType.RESULT, result)

    def _current_task_description(self) -> str:
        state = "listening" if self._listening else "not listening"
        return f"flic ({count_of(len(self._known_buttons), 'button')} paired, {state})"

    async def _handle_cmd(self, cmd: FlicAgentCommand, **kwargs: Any) -> str:
        """Run one parsed command and return what to say about it."""
        if not self._have_lib() and cmd not in (FlicAgentCommand.HELP, FlicAgentCommand.STATUS):
            return MISSING_LIB
        if cmd in CHANGES_BUTTONS:
            busy = self._busy_with
            if busy is not None and self._command_lock.locked():
                # A pairing can hold the lock for most of a minute; without a
                # word, the command asked for looks as if it was never heard.
                await self.notify_user(
                    f"Waiting for '{busy.value}' to finish, then doing '{cmd.value}'."
                )
            async with self._command_lock:
                self._busy_with = cmd
                try:
                    return await self._run_cmd(cmd, **kwargs)
                finally:
                    self._busy_with = None
        return await self._run_cmd(cmd, **kwargs)

    async def _run_cmd(self, cmd: FlicAgentCommand, **kwargs: Any) -> str:
        """Dispatch one command; `_handle_cmd` decides whether it takes the lock."""
        if cmd == FlicAgentCommand.SCAN:
            return await self._scan()
        if cmd == FlicAgentCommand.PAIR:
            return await self._pair(str(kwargs.get("name") or ""))
        if cmd == FlicAgentCommand.LIST:
            return self._list()
        if cmd == FlicAgentCommand.RENAME:
            return await self._rename(
                str(kwargs.get("name") or ""), str(kwargs.get("new_name") or "")
            )
        if cmd == FlicAgentCommand.FORGET:
            return await self._forget(str(kwargs.get("name") or ""))
        if cmd == FlicAgentCommand.LISTEN:
            return await self._listen()
        if cmd == FlicAgentCommand.STOP:
            return await self._stop()
        if cmd == FlicAgentCommand.STATUS:
            return self._status()
        if cmd == FlicAgentCommand.LATE:
            return await self._late(str(kwargs.get("value") or ""))
        return HELP_TEXT

    async def _scan(self) -> str:
        """Buttons in range, marking which are already paired here."""
        try:
            found = await self._discover()
        except TimeoutError:
            return "The scan timed out. Is Bluetooth on?"
        except Exception as exc:
            LOG.exception("[%s] Scan failed", self.name)
            return f"The scan failed: {exc}"
        if not found:
            return "No Flic buttons in range."

        paired = {button.address.lower(): button.name for button in self._known_buttons}
        lines = []
        for device in found:
            address = str(getattr(device, "address", "")).lower()
            known = paired.get(address)
            lines.append(f"  {address} — {known}" if known else f"  {address} — not paired")
        return "Flic buttons in range:\n" + "\n".join(lines)

    async def _pair(self, name: str = "") -> str:
        """Pair a button being held down, store it and start listening to it.

        Says first what to do with the button, because a new button only shows
        up while it is held in pairing mode, and asking is usually what reminds
        someone to pick it up. Then watches until one appears or `PAIR_WAIT_S` runs out,
        so the order of holding and asking does not matter. The watch is one
        scan rather than a scan repeated, because each scan is switched on and
        off again on a controller that may be holding other buttons' sessions,
        and some controllers fail at switching off under that load.
        """
        await self.notify_user(PAIR_INSTRUCTIONS)
        paired = {button.address.lower() for button in self._known_buttons}
        try:
            async with self._scan_lock:
                found = await watch_for_buttons(PAIR_WAIT_S, paired)
        except Exception as exc:
            LOG.exception("[%s] Scan before pairing failed", self.name)
            return f"Could not scan for buttons: {exc}"
        candidates = [
            device for device in found if str(getattr(device, "address", "")).lower() not in paired
        ]
        if not candidates:
            seen = {str(getattr(device, "address", "")).lower() for device in found}
            already = [b.name for b in self._known_buttons if b.address.lower() in seen]
            if already:
                # The watch hears any Flic that advertises: one held down, and
                # one that is disconnected and looking to reconnect. Either
                # way, "nothing came" would send the person looking for a
                # fault, and "in pairing mode" may not be true.
                return (
                    f"No new button showed up; only {name_list(already)}, "
                    f"{'which is' if len(already) == 1 else 'which are'} already paired here. "
                    "Hold down the new button for about 7 seconds and ask again, "
                    "or forget a paired one first to pair it again."
                )
            return (
                f"No button came into pairing mode within {PAIR_WAIT_S:.0f} seconds. "
                "Hold it down for about 7 seconds and ask again."
            )

        # Whichever answered the scan first. More than one new button is worth
        # saying out loud, because the reply names what was paired and the
        # other one is still waiting.
        device = candidates[0]
        button = await self._pair_device(device, name)
        if button is None:
            return "Pairing failed. Hold the button down for about 7 seconds and try again."

        also = len(candidates) - 1

        self._known_buttons.append(button)
        self._paused = False
        await self._remember()
        self._listening = True
        # The device the scan just found: the session needs one, and looking
        # again would only find the same button.
        await self._start_button(button, device)
        await self._announce()
        reply = (
            f"Paired '{button.name}' ({button.serial_number}). "
            f"Its presses are on {TOPIC_ROOT}/{button.key}/<gesture>."
        )
        if also == 1:
            reply += " Another new button showed up too; ask again to pair it."
        elif also:
            reply += f" {also} more new buttons showed up too; ask again for each."
        return reply

    def _list(self) -> str:
        """Paired buttons, with battery and when each was last pressed."""
        if not self._known_buttons:
            return "No buttons paired yet. Say 'pair' while holding one down."
        lines = []
        for button in self._known_buttons:
            client = self._clients.get(button.key)
            connected = bool(client is not None and getattr(client, "is_connected", False))
            last = self._last_press.get(button.key)
            when = f"{time.time() - last:.0f}s ago" if last else "not since start"
            lines.append(
                f"  {button.name} — {TOPIC_ROOT}/{button.key}, "
                f"{'connected' if connected else 'disconnected'}, "
                f"battery {self._battery_text(button)}, last press {when}"
            )
        return "Paired buttons:\n" + "\n".join(lines)

    async def _rename(self, name: str, new_name: str) -> str:
        """Rename a button. Its topics are keyed by serial, so they stay put."""
        if not name and len(self._known_buttons) == 1:
            # "rename it to …" with one button paired can only mean that one.
            name = self._known_buttons[0].name
        if not name or not new_name:
            return "Say which button to rename and what to call it: rename <old> to <new>."
        button = self._button(name)
        if button is None:
            return f"No button called '{name}'. Say 'list' to see them."
        wanted = unique_name(new_name, {b.slug for b in self._known_buttons} - {button.slug})

        renamed = replace(button, name=wanted)
        self._known_buttons = [renamed if b.key == button.key else b for b in self._known_buttons]
        await self._remember()
        # The topics are unchanged, but the manifest names the buttons, and the
        # planner reads names when it is asked for "the kitchen button".
        await self._announce()
        return (
            f"'{button.name}' is now '{wanted}'. "
            f"Its presses stay on {TOPIC_ROOT}/{button.key}/<gesture>."
        )

    async def _forget(self, name: str = "") -> str:
        """Stop a button, delete its keys and take back its retained state."""
        if not name and len(self._known_buttons) == 1:
            button = self._known_buttons[0]
        else:
            button = self._button(name)
        if button is None:
            return f"No button called '{name}'. Say 'list' to see them."

        await self._stop_button(button)
        await self._clear_retained(button)
        self._known_buttons = [b for b in self._known_buttons if b.key != button.key]
        self._last_press.pop(button.key, None)
        self._battery_volts.pop(button.key, None)
        await self._remember()
        await self._announce()
        return f"Forgot '{button.name}'. Pair it again whenever you like."

    async def _listen(self) -> str:
        """Start listening to every paired button."""
        if not self._known_buttons:
            return "No buttons paired yet. Say 'pair' while holding one down."
        self._listening = True
        if self._paused:
            self._paused = False
            await self._remember()
        connected: list[str] = []
        waiting: list[str] = []
        for button in self._known_buttons:
            (connected if await self._start_button(button) else waiting).append(button.name)
        if waiting:
            # Asking to listen is asking for the buttons now: the search looks
            # at once rather than whenever its back-off next comes round.
            self._restart_finder()
        return listening_reply(connected, waiting, self.late_presses)

    async def _stop(self, remember: bool = True) -> str:
        """Stop listening, keeping the pairings.

        `remember` is for a person asking: the choice is written down so a
        restart keeps it. Shutting down and deleting pass False.
        """
        self._listening = False
        if remember:
            self._paused = True
            await self._remember()
        finder, self._finder = self._finder, None
        if finder:
            finder.cancel()
        listened = [button for button in self._known_buttons if button.key in self._clients]
        # Every client at once, a client for a button no longer known included.
        # A controller that has stopped answering makes each close wait out its
        # whole timeout, and one after another those add up to a shutdown that
        # seems to hang.
        await asyncio.gather(*(self._close(key) for key in list(self._clients)))
        for button in listened:
            await self._publish_state(button, connected=False)
        return "Stopped listening. The pairings are kept."

    async def _late(self, value: str) -> str:
        """Turn late presses on or off, or say which they are."""
        wanted = _SWITCH_WORDS.get(value.strip().lower())
        if wanted is not None:
            self.late_presses = wanted == "on"
            await self._remember()
        if self.late_presses:
            return (
                "Late presses are on: a press made while a button was disconnected is "
                'published when it reconnects, up to a minute late, marked "late": true.'
            )
        return (
            "Late presses are off: a press made while a button was disconnected only "
            "wakes it. Say 'late presses on' to publish those too."
        )

    def _status(self) -> str:
        """Whether the library is present, and what is paired and connected."""
        if not self._have_lib():
            return MISSING_LIB
        waiting = [
            button.name
            for button in self._known_buttons
            if not getattr(self._clients.get(button.key), "is_connected", False)
        ]
        return status_reply(
            len(self._known_buttons),
            len(self._known_buttons) - len(waiting),
            self._listening,
            waiting,
            self.late_presses,
        )

    def _button(self, name: str) -> FlicButton | None:
        """The paired button a person means, by name or serial, or None.

        Compared as slugs, so "lamp flic", "Lamp Flic" and "lamp-flic" are one
        button, and its serial number finds it whatever it is called.
        """
        wanted = slug(name)
        if not wanted:
            return None
        for button in self._known_buttons:
            if wanted in (button.slug, button.key):
                return button
        return None

    def _by_key(self, key: str) -> FlicButton | None:
        """The paired button with that key, or None once it has been forgotten."""
        return next((button for button in self._known_buttons if button.key == key), None)

    async def _pair_device(self, device: Any, name: str) -> FlicButton | None:
        """Verify a pairing with a button in range and return what it gave back."""
        client_cls = self._client_cls
        if client_cls is None:
            return None
        address = str(getattr(device, "address", ""))
        client = client_cls(address=address, ble_device=device)
        try:
            await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT_S)
            (
                pairing_id,
                pairing_key,
                serial_number,
                battery,
                sig_bits,
                button_uuid,
                firmware_version,
            ) = await asyncio.wait_for(client.full_verify_pairing(), timeout=PAIRING_TIMEOUT_S)
        except Exception:
            LOG.exception("[%s] Pairing with %s failed", self.name, address)
            return None
        finally:
            await self._disconnect(client)

        taken = {button.slug for button in self._known_buttons}
        return FlicButton(
            name=unique_name(name or f"Flic {len(self._known_buttons) + 1}", taken),
            address=address,
            pairing_id=int(pairing_id),
            pairing_key=bytes(pairing_key),
            serial_number=str(serial_number),
            sig_bits=int(sig_bits),
            device_type=device_type_of(str(serial_number)),
            battery=int(battery),
            button_uuid=as_hex(button_uuid),
            firmware_version=int(firmware_version),
        )

    async def _start_button(self, button: FlicButton, device: Any = None) -> bool:
        """Connect to one button and listen to it, reporting whether it is connected now.

        The library connects only to a `BLEDevice` it has been handed; it never
        looks one up from an address. So the device is found here — or taken
        from the scan that paired the button — before the session starts.

        A button that cannot be reached yet keeps its client. With a device, the
        library's own reconnect loop retries it; without one, `_find_missing`
        keeps looking and hands the device over once the button is in range. One
        button out of range therefore never stops the others, and comes back
        without anyone asking.
        """
        if self._client_cls is None:
            return False
        if button.key in self._clients:
            return bool(getattr(self._clients[button.key], "is_connected", False))
        if device is None:
            device = await self._locate(button.address)
        client = self._new_client(button, device)
        self._clients[button.key] = client
        if device is None:
            LOG.info("[%s] '%s' is not in range; still looking for it", self.name, button.name)
            self._ensure_finder()
            return False
        try:
            await asyncio.wait_for(client.start(), timeout=CONNECT_TIMEOUT_S)
        except Exception:
            LOG.warning(
                "[%s] Could not reach '%s'; retrying in the background",
                self.name,
                button.name,
                exc_info=True,
            )
            # A failed start leaves no retry behind it. Handing the device back
            # is the library's way of starting its reconnect loop.
            client.set_ble_device(device)
            self._ensure_finder()
            return False
        await self._publish_state(button, connected=True)
        return True

    def _new_client(self, button: FlicButton, device: Any) -> "FlicClient":
        """A client for one button, wired to this agent but not yet started."""
        client_cls = self._client_cls
        if client_cls is None:
            raise RuntimeError("pyflic-ble is not loaded; callers check _have_lib first")
        client = client_cls(
            address=button.address,
            ble_device=device,
            pairing_id=button.pairing_id,
            pairing_key=button.pairing_key,
            serial_number=button.serial_number,
            sig_bits=button.sig_bits,
            # Stored with the pairing, so a Twist keeps the mode it was set to.
            push_twist_mode=self._twist_mode(button.twist_push_mode),
        )
        client.on_button_event = self._make_press_handler(button.key)
        client.register_state_callback(self._make_state_handler(button.key))
        return client

    async def _discover(self) -> list[Any]:
        """Flic buttons in range, one scan at a time."""
        async with self._scan_lock:
            return await discover_buttons(SCAN_TIMEOUT_S)

    async def _locate(self, address: str) -> Any:
        """The `BLEDevice` for one known button, or None when it cannot be found."""
        try:
            return await self._look_up(address)
        except Exception:
            LOG.debug("[%s] Looking for %s failed", self.name, address, exc_info=True)
            return None

    async def _look_up(self, address: str) -> Any:
        """The `BLEDevice` for one known button, or None when it is not in range.

        Raises when the scan itself cannot run — no adapter, or a controller
        refusing — which says nothing about where the button is.
        """
        async with self._scan_lock:
            return await find_button(address, FIND_TIMEOUT_S)

    def _ensure_finder(self, hold_off: bool = False) -> None:
        """Start looking for unconnected buttons, unless something already is.

        `hold_off` delays the first lookup by one interval; see
        `_ensure_finder_after_drop`.
        """
        if self._listening and (self._finder is None or self._finder.done()):
            self._finder = asyncio.create_task(self._find_missing(hold_off))

    def _restart_finder(self) -> None:
        """Start the search over: an immediate look, and its back-off from the start."""
        finder, self._finder = self._finder, None
        if finder:
            finder.cancel()
        self._ensure_finder()

    def _ensure_finder_after_drop(self) -> None:
        """Start looking for a button that has just dropped, after giving it time.

        A button that drops still has the device the library reconnects with,
        and the library's own retries usually bring it back within seconds. A
        scan started meanwhile competes with them, and on some controllers
        starting one knocks down the connection being made. The search is for
        a button the library cannot reach by itself, so it waits one interval
        before its first look.
        """
        self._ensure_finder(hold_off=True)

    async def _find_missing(self, hold_off: bool = False) -> None:
        """Hand a fresh device to every listening client that is not connected.

        Home Assistant does this from every advertisement it hears; here it is a
        lookup by address, backing off while a button stays away. A device that
        went stale while the button was gone is replaced the same way. Ends when
        every button is connected, and starts again when one drops.

        A scan that fails is not a button away. When the adapter is gone the
        search keeps its shortest interval rather than backing off, so the
        buttons come back soon after the adapter does instead of after the
        longest wait.
        """
        if hold_off:
            await asyncio.sleep(FIND_INTERVAL_S)
        delay = FIND_INTERVAL_S
        while self._listening:
            waiting = [
                (key, client)
                for key, client in self._clients.items()
                if not getattr(client, "is_connected", False)
            ]
            if not waiting:
                return
            if await self._hand_over_devices(waiting):
                await asyncio.sleep(delay)
                delay = min(delay * 2, FIND_MAX_INTERVAL_S)
            else:
                await asyncio.sleep(FIND_INTERVAL_S)
                delay = FIND_INTERVAL_S

    async def _hand_over_devices(self, waiting: list[tuple[str, Any]]) -> bool:
        """Give each waiting client the device a lookup finds. False if Bluetooth failed.

        The first failed scan ends the round: every other lookup would fail
        the same way. The failure is logged once when it starts and once when
        it ends, rather than on every round in between.
        """
        for key, client in waiting:
            button = self._by_key(key)
            if button is None:
                continue
            try:
                device = await self._look_up(button.address)
            except Exception as exc:
                if not self._scan_failing:
                    self._scan_failing = True
                    LOG.warning(
                        "[%s] Bluetooth scanning is failing (%s); trying again every %.0fs",
                        self.name,
                        exc,
                        FIND_INTERVAL_S,
                    )
                return False
            if device is not None and self._clients.get(key) is client:
                client.set_ble_device(device)
        if self._scan_failing:
            self._scan_failing = False
            LOG.info("[%s] Bluetooth scanning works again", self.name)
        return True

    async def _stop_button(self, button: FlicButton) -> None:
        """Stop listening to one button, leaving its pairing alone."""
        if button.key in self._clients:
            await self._close(button.key)
            await self._publish_state(button, connected=False)

    async def _close(self, key: str) -> None:
        """Drop a client, whether or not it manages to say goodbye."""
        client = self._clients.pop(key, None)
        if client is None:
            return
        try:
            await asyncio.wait_for(client.stop(), timeout=STOP_TIMEOUT_S)
        except Exception:
            # `stop()` can stall while disconnecting, and a shutdown that waits
            # on a radio is a shutdown that does not finish. The client is
            # dropped either way.
            LOG.warning("[%s] '%s' did not stop cleanly", self.name, key, exc_info=True)

    @staticmethod
    async def _disconnect(client: Any) -> None:
        """Hang up on a client used for one exchange."""
        try:
            await asyncio.wait_for(client.disconnect(), timeout=STOP_TIMEOUT_S)
        except Exception:
            LOG.debug("Disconnect did not complete cleanly", exc_info=True)

    def _make_press_handler(self, key: str):
        """A callback for one button's presses.

        The library calls this synchronously while handling a BLE notification,
        so it stamps the press with the host clock — the button's own timestamp
        counts Bluetooth ticks from a clock nobody else shares — and hands it to
        the loop. Publishing here would put the broker inside a notification.
        """

        def handler(kind: str, data: dict[str, Any]) -> None:
            at = time.time()
            loop = self._loop
            if loop is None:
                return
            loop.call_soon_threadsafe(self._presses.put_nowait, (key, kind, at, dict(data)))

        return handler

    def _make_state_handler(self, key: str):
        """A callback for one button's connection coming and going.

        The library reconnects by itself, so the first start is not the only
        moment a button becomes reachable. Handed to the loop like a press, and
        a drop also sets the finder looking, after the library has had its turn,
        in case the device went stale.
        """

        def handler(state: Any) -> None:
            loop = self._loop
            if loop is None:
                return
            connected = bool(getattr(state, "connected", False))
            change: dict[str, Any] = {"connected": connected}
            volts = getattr(state, "battery_voltage", None)
            if isinstance(volts, (int, float)):
                change["battery_voltage"] = float(volts)
            loop.call_soon_threadsafe(
                self._presses.put_nowait, (key, CONNECTION_EVENT, time.time(), change)
            )
            if not connected:
                loop.call_soon_threadsafe(self._ensure_finder_after_drop)

        return handler

    async def _publish_presses(self) -> None:
        """Publish presses as they are handed over, until the agent stops."""
        while True:
            key, kind, at, data = await self._presses.get()
            try:
                if kind == CONNECTION_EVENT:
                    volts = data.get("battery_voltage")
                    if volts is not None:
                        self._battery_volts[key] = volts
                    await self._publish_connection(key, bool(data.get("connected")))
                    continue
                await self._publish_press(key, kind, at, data)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("[%s] Could not publish a press from '%s'", self.name, key)

    async def _publish_press(self, key: str, kind: str, at: float, data: dict[str, Any]) -> None:
        """Publish one press, if it is one anybody wired anything to."""
        if kind not in GESTURES:
            self._note_unpublished(kind)
            return
        late = bool(data.get("was_queued"))
        if late and not self.late_presses:
            return
        button = self._by_key(key)
        if button is None:
            return
        self._last_press[button.key] = at
        payload: dict[str, Any] = {
            "button": button.name,
            "serial": button.serial_number,
            "gesture": kind,
            "at": at,
        }
        # A Duo has two halves and reports both as the same gesture, so the one
        # that was pressed is only in the event. A single-button device sends
        # no index and the field stays out of its payload.
        index = data.get("button_index")
        if index is not None:
            payload["button_index"] = index
        # Made while the button was disconnected, and delivered on reconnecting:
        # whatever is wired to it can decide whether a late press still counts.
        if late:
            payload["late"] = True
        await self._mqtt_publish(f"{TOPIC_ROOT}/{button.key}/{kind}", payload)

    def _note_unpublished(self, kind: str) -> None:
        """Say once that a kind of event arrived that nothing is wired to.

        A Duo's swipes and `up`/`down` arrive as button events and go no further.
        (A Twist's rotation never gets this far: it comes through the client's
        `on_rotate_event`, which this agent does not set.) Said once per kind,
        because the point is that it happened at all, not how often.
        """
        if kind in self._unpublished:
            return
        self._unpublished.add(kind)
        LOG.info(
            "[%s] '%s' events arrive from this button but are not published; only %s are.",
            self.name,
            kind,
            ", ".join(GESTURES),
        )

    async def _publish_connection(self, key: str, connected: bool) -> None:
        """Publish a connection change for a button still being listened to."""
        button = self._by_key(key)
        if button is None or key not in self._clients:
            return
        await self._publish_state(button, connected=connected)

    async def _publish_state(self, button: FlicButton, connected: bool) -> None:
        """Say whether a button is reachable, for whoever asks later."""
        await self._mqtt_publish(
            f"{TOPIC_ROOT}/{button.key}/state",
            {"connected": connected, "battery_voltage": self._battery_voltage(button)},
            retain=True,
            # At least once, like the retraction that undoes it: a dropped
            # retained message leaves the broker telling every later subscriber
            # the opposite of where the button is.
            qos=1,
        )

    def _battery_voltage(self, button: FlicButton) -> float | None:
        """The freshest battery voltage for a button, or None when nothing was read."""
        latest = self._battery_volts.get(button.key)
        if latest is not None:
            return round(latest, 2)
        return battery_volts(button)

    def _battery_text(self, button: FlicButton) -> str:
        volts = self._battery_voltage(button)
        return f"{volts:.2f} V" if volts is not None else "unknown"

    async def _clear_retained(self, button: FlicButton) -> None:
        """Take back the retained state of a button that is going away.

        An empty retained payload is how MQTT retracts one. Left behind, the
        broker keeps telling every later subscriber about a button that is no
        longer here.
        """
        await self._mqtt_publish(f"{TOPIC_ROOT}/{button.key}/state", b"", retain=True, qos=1)

    def _have_lib(self) -> bool:
        """Whether the Bluetooth library is importable, looked up once."""
        if not self._checked_lib:
            self._checked_lib = True
            try:
                # Optional dependency (wactorz[flic], Python 3.12+): the agent
                # must load, and say what is missing, on a host without it.
                from pyflic_ble import (  # pyright: ignore[reportMissingImports]
                    FlicClient,
                    PushTwistMode,
                )

                self._client_cls = FlicClient
                self._twist_mode = PushTwistMode
            except ImportError:
                return False
        return self._client_cls is not None

    async def _restore(self) -> Any:
        """Whatever the buttons file holds, or an empty list, read off the event loop.

        A file that cannot be read is treated as an absent one: buttons can be
        paired again, while refusing to start over a damaged file leaves
        nothing working at all.
        """
        try:
            return await asyncio.to_thread(read_store, self._buttons_json)
        except Exception:
            LOG.warning("[%s] Could not read %s", self.name, self._buttons_json, exc_info=True)
            return []

    async def _remember(self) -> None:
        """Write the paired buttons, and whether to listen, back to the private file.

        Written off the event loop, since disk can stall. A failed write is
        logged rather than raised, so a command that has already paired a
        button still answers; the pairing is live either way until the process
        restarts.
        """
        payload = {
            "buttons": [button.to_dict() for button in self._known_buttons],
            "listening": not self._paused,
            "late_presses": self.late_presses,
        }
        try:
            await asyncio.to_thread(write_private_json, self._buttons_json, payload)
        except Exception:
            LOG.exception("[%s] Failed to store known buttons", self.name)


async def discover_buttons(timeout: float) -> list[Any]:
    """Flic buttons advertising in range.

    Filtered on the service they advertise, so a scan does not return every
    piece of Bluetooth in the building.
    """
    # Optional dependency, arriving with pyflic-ble: a host without the extra
    # never reaches this, because every command that would, checks first.
    from bleak import BleakScanner  # pyright: ignore[reportMissingImports]

    service_uuids = [FLIC_SERVICE_UUID, TWIST_SERVICE_UUID]
    found = await asyncio.wait_for(
        BleakScanner.discover(timeout=timeout, service_uuids=service_uuids),
        timeout=timeout * 2,
    )
    return list(found)


async def watch_for_buttons(timeout: float, skip: Collection[str]) -> list[Any]:
    """Flic buttons advertising, watched for until one not in `skip` shows up.

    One scan, however long it runs, that ends as soon as a button whose address
    is not in `skip` appears or `timeout` passes. Returns every Flic button seen
    by then, first seen first. A scan that will not stop is logged and what it
    found is kept: the buttons it saw are real whether or not the controller
    acknowledges the stop.
    """
    # Optional dependency, as in `discover_buttons`.
    from bleak import BleakScanner  # pyright: ignore[reportMissingImports]

    ignored = {address.lower() for address in skip}
    seen: dict[str, Any] = {}
    arrived = asyncio.Event()

    def detected(device: Any, _advertisement: Any) -> None:
        address = str(getattr(device, "address", "")).lower()
        seen.setdefault(address, device)
        if address not in ignored:
            arrived.set()

    # BlueZ refuses to start a scan while it is still stopping an earlier one
    # ("Operation already in progress"), which a stop moments before leaves
    # behind: the agent restarting, or its own search. One more try, a moment
    # later, before the person is told it failed.
    for attempt in range(2):
        scanner = BleakScanner(
            detection_callback=detected,
            service_uuids=[FLIC_SERVICE_UUID, TWIST_SERVICE_UUID],
        )
        try:
            await asyncio.wait_for(scanner.start(), timeout=STOP_TIMEOUT_S)
            break
        except Exception:
            if attempt:
                raise
            LOG.info("Starting the Bluetooth scan failed; trying once more", exc_info=True)
            await asyncio.sleep(SCAN_RETRY_PAUSE_S)
    try:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(arrived.wait(), timeout=timeout)
    finally:
        try:
            await asyncio.wait_for(scanner.stop(), timeout=STOP_TIMEOUT_S)
        except Exception:
            LOG.warning("Stopping the Bluetooth scan failed; keeping what it saw", exc_info=True)
    return list(seen.values())


async def find_button(address: str, timeout: float) -> Any:
    """The `BLEDevice` for the button at `address`, or None when it is not in range."""
    # Optional dependency, as in `discover_buttons`.
    from bleak import BleakScanner  # pyright: ignore[reportMissingImports]

    return await asyncio.wait_for(
        BleakScanner.find_device_by_address(address, timeout=timeout),
        timeout=timeout * 2,
    )


def parse_command(text: str) -> tuple[FlicAgentCommand, dict[str, str]]:
    """The command in a line of text, and whatever it was given.

    The first recognised word wins, so "please pair the kitchen button" and
    "pair kitchen" both pair. What follows it is a name, and a name keeps every
    word it has: "pair Lamp Flic" names the button "Lamp Flic". A rename splits old from new
    at "to", "as" or "into"; without one, the first word is the old name and the
    rest is the new one.
    """
    words = [word for word in re.split(r"\s+", (text or "").strip()) if word]
    for index, word in enumerate(words):
        command = COMMAND_WORDS.get(word.lower().strip("/@,.!?"))
        if command is None:
            continue
        following = words[index + 1 :]
        if command == FlicAgentCommand.RENAME:
            return command, _rename_arguments(following)
        if command == FlicAgentCommand.LATE:
            # Anywhere in the sentence: "turn off late presses" as much as
            # "late presses off".
            switch = next(
                (_SWITCH_WORDS[w.lower()] for w in words if w.lower() in _SWITCH_WORDS), None
            )
            return command, {"value": switch} if switch else {}
        name = _name_from(following)
        if name and command in (FlicAgentCommand.PAIR, FlicAgentCommand.FORGET):
            return command, {"name": name}
        return command, {}
    return FlicAgentCommand.HELP, {}


def _name_from(words: list[str]) -> str:
    """The name in `words`, filler and quotes dropped, its words kept together."""
    kept = [word.strip("\"'") for word in words if word.lower().strip("\"'") not in _FILLER]
    return " ".join(word for word in kept if word)


def _rename_arguments(words: list[str]) -> dict[str, str]:
    """The old and new names in what follows "rename"."""
    split = next((i for i, w in enumerate(words) if w.lower() in _RENAME_SEPARATORS), None)
    if split is not None:
        old, new = _name_from(words[:split]), _name_from(words[split + 1 :])
    else:
        first, _, rest = _name_from(words).partition(" ")
        old, new = first, rest
    arguments = {"name": old} if old else {}
    if new:
        arguments["new_name"] = new
    return arguments


def slug(text: str) -> str:
    """Text as an identifier: lowercase, runs of anything else one hyphen."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")


def unique_name(wanted: str, taken: Iterable[str]) -> str:
    """`wanted`, tidied, as a name whose slug no other button answers to.

    Capitals and spaces are the person's to choose; only the slug has to be
    unique, since that is what chat finds a button by. A clash gets a number:
    a second "Kitchen" becomes "Kitchen 2".
    """
    base = " ".join((wanted or "").split()) or "Flic"
    if not slug(base):
        base = "Flic"
    existing = set(taken)
    if slug(base) not in existing:
        return base
    suffix = 2
    while slug(f"{base} {suffix}") in existing:
        suffix += 1
    return f"{base} {suffix}"


def device_type_of(serial: str) -> Literal["flic2", "duo", "twist"]:
    """Which kind of button a serial number belongs to."""
    upper = (serial or "").upper()
    if upper.startswith("BT"):
        return "twist"
    if upper.startswith("BD"):
        return "duo"
    return "flic2"


def as_hex(value: Any) -> str:
    """A button's uuid as text, whichever way the library hands it over."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return str(value or "")


def battery_volts(button: FlicButton) -> float | None:
    """The battery reading from pairing, in volts, or None when there was none.

    The same conversion the library makes: a Twist reports millivolts, and a
    Flic 2 or Duo a 10-bit reading against a 3.6 V reference.
    """
    if button.battery <= 0:
        return None
    if button.device_type == "twist":
        return round(button.battery / 1000.0, 2)
    return round(button.battery * 3.6 / 1024.0, 2)


def status_reply(
    paired: int,
    connected: int,
    listening: bool,
    waiting: list[str] | None = None,
    late_presses: bool = False,
) -> str:
    """What `status` says, as a sentence rather than a row of fields.

    `waiting` names the paired buttons not connected, so "Listening" with none
    connected says what it is waiting for instead of reading as all is well.
    """
    if not paired:
        return "No buttons paired yet. Say 'pair' while holding one down."
    if not listening:
        listens = "Not listening; say 'listen' to start."
    elif waiting:
        listens = f"Listening; still looking for {name_list(waiting)}. {wake_hint(late_presses)}"
    else:
        listens = "Listening."
    late = " Late presses are on." if late_presses else ""
    return f"{count_of(paired, 'button')} paired, {connected} connected. {listens}{late}"


def count_of(count: int, noun: str) -> str:
    """A count with its noun: "1 button", "2 buttons"."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def listening_reply(connected: list[str], waiting: list[str], late_presses: bool = False) -> str:
    """What `listen` says: the buttons by name, not counts that read like indices.

    A button not connected yet is still being connected to in the background,
    so it is described as that rather than as a failure, with how to wake it.
    """
    parts: list[str] = []
    if connected:
        parts.append(f"Listening to {name_list(connected)}.")
    if waiting:
        parts.append(f"Still connecting to {name_list(waiting)} in the background.")
        parts.append(wake_hint(late_presses))
    return " ".join(parts)


def wake_hint(late_presses: bool) -> str:
    """How to bring back a button that is not connecting, and what that press does.

    A disconnected Flic 2 advertises only after it is pressed, to save its
    battery; nothing the host does makes it reachable before then. The press
    that wakes it arrives late, and a late press is published only when late
    presses are on.
    """
    fate = (
        "that press is published once it connects, marked late"
        if late_presses
        else "that press only wakes it and is not published"
    )
    return f"A disconnected button sleeps until pressed: press it once to wake it ({fate})."


def name_list(names: list[str]) -> str:
    """Names quoted and joined as a sentence would: 'a', 'b' and 'c'."""
    quoted = [f"'{name}'" for name in names]
    if len(quoted) <= 1:
        return "".join(quoted)
    return f"{', '.join(quoted[:-1])} and {quoted[-1]}"


def button_directory(buttons: Iterable[FlicButton]) -> str:
    """Which button each topic belongs to, for the manifest.

    Topics are keyed by serial, so without this a planner asked for "the
    kitchen button" has a list of serial numbers and nothing to match it to.
    """
    entries = [f"'{button.name}' is {TOPIC_ROOT}/{button.key}" for button in buttons]
    return f" Buttons: {'; '.join(entries)}." if entries else ""


def gesture_topics(buttons: Iterable[FlicButton]) -> list[str]:
    """Every topic the paired buttons publish between them.

    One per button per gesture, plus the retained topic carrying whether that
    button is connected. The planner wires from the manifest, so a topic left
    out here cannot be wired to even while the agent publishes it.
    """
    topics: list[str] = []
    for button in buttons:
        base = f"{TOPIC_ROOT}/{button.key}"
        topics.extend(f"{base}/{gesture}" for gesture in GESTURES)
        topics.append(f"{base}/state")
    return topics


def read_store(path: Path) -> Any:
    """The parsed buttons file, or an empty list when there is none yet."""
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def late_presses_from_store(stored: Any) -> bool:
    """Whether the stored file turns late presses on. Anything but an explicit yes is off."""
    return isinstance(stored, dict) and stored.get("late_presses") is True


def listening_from_store(stored: Any) -> bool:
    """Whether the stored file says to listen. Anything but an explicit no means yes."""
    return not (isinstance(stored, dict) and stored.get("listening") is False)


def flic_buttons_from_store(stored: Any) -> list[FlicButton]:
    """The buttons in a stored payload, in either shape it can take.

    Accepts the file's `{"buttons": [...]}` and a bare list. A record that is
    not a button is dropped rather than failing the load: one damaged entry
    should not cost every other pairing.
    """
    buttons: list[FlicButton] = []
    if isinstance(stored, dict):
        stored_buttons = stored.get("buttons", [])
        if isinstance(stored_buttons, list):
            stored = stored_buttons
    if isinstance(stored, list):
        for stored_button in stored:
            restored = flic_button_from_store(stored_button)
            if restored:
                buttons.append(restored)
    return buttons


def flic_button_from_store(stored_button: Any) -> FlicButton | None:
    """One stored mapping as a button, or None when it is not one."""
    if not isinstance(stored_button, dict):
        return None
    try:
        return FlicButton.from_dict(stored_button)
    except (TypeError, ValueError, binascii.Error):
        LOG.warning("Dropping an unreadable button record")
        return None
