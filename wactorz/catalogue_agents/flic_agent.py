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
import json
import logging
import re
import sys
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from enum import Enum
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
#: told the person to hold one down.
PAIR_WAIT_S = 45.0
#: A breath between those scans, so a backend that answers at once cannot spin.
PAIR_RESCAN_PAUSE_S = 1.0
PAIR_INSTRUCTIONS = (
    "Hold the Flic button down for about 7 seconds to put it in pairing mode. "
    f"Looking for it for the next {PAIR_WAIT_S:.0f} seconds..."
)
STOP_TIMEOUT_S = 20.0
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
    """

    name: str
    address: str
    pairing_id: int
    pairing_key: bytes
    serial_number: str
    sig_bits: int
    device_type: Literal["flic2", "duo", "twist"] = "flic2"
    battery: int = 0
    button_uuid: str = ""
    firmware_version: int = 0
    twist_push_mode: Literal["default", "continuous", "selector"] = "default"

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
}

HELP_TEXT = """Flic buttons:
  scan                  buttons in range, and which are already paired
  pair [name]           hold a button down for 7s, then pair it
  list                  paired buttons, with battery and last press
  rename <old> <new>    rename a button and move its topics
  forget <name>         unpair a button and take back its topics
  listen / stop         start or stop listening, keeping the pairings
  status                what is installed, paired and connected

Every press is published on custom/flic/<name>/<click|double_click|hold>."""


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
        self._buttons_json = self._persistence_dir / "buttons.json"
        self._known_buttons: list[FlicButton] = []
        self._clients: dict[str, FlicClient] = {}
        self._last_press: dict[str, float] = {}
        self._listening = False
        self._checked_lib = False
        self._client_cls: type[FlicClient] | None = None
        self._presses: asyncio.Queue[tuple[str, str, float, dict[str, Any]]] = asyncio.Queue()
        self._pump: asyncio.Task[None] | None = None
        #: Looks for buttons whose session is down and hands the library a fresh
        #: device for each; see `_find_missing`.
        self._finder: asyncio.Task[None] | None = None
        #: BlueZ keeps one discovery per client. A scan started while another is
        #: stopping gets "No discovery started" when it stops its own, so every
        #: scan and lookup here takes turns.
        self._scan_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.publish_queued = False
        #: Kinds of event seen that this agent does not publish, so each is
        #: mentioned once rather than every time one arrives.
        self._unpublished: set[str] = set()

    async def on_start(self) -> None:
        """Load the paired buttons, start listening to them, and announce."""
        self._known_buttons = flic_buttons_from_store(self._restore())
        self._loop = asyncio.get_running_loop()
        if self._have_lib():
            self._pump = asyncio.create_task(self._publish_presses())
            if self._known_buttons:
                await self._listen()
        else:
            LOG.warning("[%s] %s", self.name, MISSING_LIB)
        await self._announce()

    async def on_stop(self) -> None:
        """Put every button down before the agent goes."""
        await self._stop()
        pump, self._pump = self._pump, None
        if pump:
            pump.cancel()

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
                "action": "str - help | scan | pair | list | rename | listen | stop | status | forget",
                "name": "str - button to act on, or the name to give a new pairing",
                "new_name": "str - replacement name, for rename",
            },
            output_schema={
                "button": "str - the button that was pressed",
                "serial": "str - its serial number",
                "gesture": "str - click | double_click | hold",
                "at": "float - unix epoch when the host received the press",
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
        if msg.type == MessageType.DELETE:
            # The agent is going away for good. The pairings stay on disk, so
            # spawning it again finds its buttons, but the retained state of a
            # button nothing is listening to has to be taken back.
            await self._stop()
            for button in self._known_buttons:
                await self._clear_retained(button)
            return
        if msg.type != MessageType.TASK:
            return

        raw = msg.payload
        arguments: dict[str, str] = {}
        if isinstance(raw, dict):
            action = str(raw.get("action") or "").strip()
            if action:
                command = COMMAND_WORDS.get(action.lower(), FlicAgentCommand.HELP)
                arguments = {
                    key: str(raw[key]) for key in ("name", "new_name") if raw.get(key) is not None
                }
            else:
                text = raw.get("text") or raw.get("content") or raw.get("query") or ""
                command, arguments = parse_command(str(text))
        else:
            command, arguments = parse_command(str(raw or ""))

        result: dict[str, Any] = {
            "ok": True,
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
        return f"flic ({len(self._known_buttons)} paired, listening={self._listening})"

    async def _handle_cmd(self, cmd: FlicAgentCommand, **kwargs: Any) -> str:
        """Run one parsed command and return what to say about it."""
        if not self._have_lib() and cmd not in (FlicAgentCommand.HELP, FlicAgentCommand.STATUS):
            return MISSING_LIB
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

        Says first what to do with the button, because a button only shows up
        while it is in pairing mode, and asking is usually what reminds someone
        to pick it up. Then keeps scanning until one appears or `PAIR_WAIT_S`
        runs out, so the order of holding and asking does not matter.
        """
        await self.notify_user(PAIR_INSTRUCTIONS)
        deadline = time.monotonic() + PAIR_WAIT_S
        paired = {button.address.lower() for button in self._known_buttons}
        while True:
            try:
                found = await self._discover()
            except Exception as exc:
                LOG.exception("[%s] Scan before pairing failed", self.name)
                return f"Could not scan for buttons: {exc}"
            candidates = [
                device
                for device in found
                if str(getattr(device, "address", "")).lower() not in paired
            ]
            if candidates or time.monotonic() >= deadline:
                break
            await asyncio.sleep(PAIR_RESCAN_PAUSE_S)
        if not candidates:
            return (
                f"No button came into pairing mode within {PAIR_WAIT_S:.0f} seconds. "
                "Hold it down for about 7 seconds and ask again."
            )

        # Whichever answered the scan first. More than one is worth saying out
        # loud, because the reply names what was paired and the other button is
        # still waiting.
        device = candidates[0]
        button = await self._pair_device(device, name)
        if button is None:
            return "Pairing failed. Hold the button down for about 7 seconds and try again."

        also = len(candidates) - 1

        self._known_buttons.append(button)
        self._remember()
        self._listening = True
        # The device the scan just found: the session needs one, and looking
        # again would only find the same button.
        await self._start_button(button, device)
        await self._announce()
        reply = (
            f"Paired '{button.name}' ({button.serial_number}). "
            f"Its presses are on {TOPIC_ROOT}/{topic_key(button)}/<gesture>."
        )
        if also:
            reply += f" {also} other button was in pairing mode; ask again to pair it."
        return reply

    def _list(self) -> str:
        """Paired buttons, with battery and when each was last pressed."""
        if not self._known_buttons:
            return "No buttons paired yet. Say 'pair' while holding one down."
        lines = []
        for button in self._known_buttons:
            client = self._clients.get(button.name)
            connected = bool(client is not None and getattr(client, "is_connected", False))
            last = self._last_press.get(button.name)
            when = f"{time.time() - last:.0f}s ago" if last else "not since start"
            lines.append(
                f"  {button.name} — {button.serial_number}, "
                f"{'connected' if connected else 'disconnected'}, "
                f"battery {button.battery}%, last press {when}"
            )
        return "Paired buttons:\n" + "\n".join(lines)

    async def _rename(self, name: str, new_name: str) -> str:
        """Rename a button, moving its topics and announcing them again."""
        if not name and len(self._known_buttons) == 1:
            # "rename it to …" with one button paired can only mean that one.
            name = self._known_buttons[0].name
        if not name or not new_name:
            return "Say which button to rename and what to call it: rename <old> <new>."
        button = self._button(name)
        if button is None:
            return f"No button called '{name}'. Say 'list' to see them."
        wanted = unique_name(new_name, {b.name for b in self._known_buttons} - {button.name})

        # The old topics stop existing, so the retained state under them is
        # taken back before the button answers to anything else.
        await self._clear_retained(button)
        await self._stop_button(button)

        renamed = replace(button, name=wanted)
        self._known_buttons = [renamed if b.name == button.name else b for b in self._known_buttons]
        self._last_press.pop(button.name, None)
        self._remember()
        if self._listening:
            await self._start_button(renamed)
        await self._announce()
        return f"'{button.name}' is now '{wanted}', publishing on {TOPIC_ROOT}/{wanted}/<gesture>."

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
        self._known_buttons = [b for b in self._known_buttons if b.name != button.name]
        self._last_press.pop(button.name, None)
        self._remember()
        await self._announce()
        return f"Forgot '{button.name}'. Pair it again whenever you like."

    async def _listen(self) -> str:
        """Start listening to every paired button."""
        if not self._known_buttons:
            return "No buttons paired yet. Say 'pair' while holding one down."
        self._listening = True
        started = 0
        for button in self._known_buttons:
            if await self._start_button(button):
                started += 1
        failed = len(self._known_buttons) - started
        if failed:
            return (
                f"Listening to {started}; {failed} could not be reached yet. "
                "They are retried in the background and start publishing when they connect."
            )
        return f"Listening to {started}."

    async def _stop(self) -> str:
        """Stop listening, keeping the pairings."""
        self._listening = False
        finder, self._finder = self._finder, None
        if finder:
            finder.cancel()
        for button in list(self._known_buttons):
            await self._stop_button(button)
        # A client for a button no longer known can still be running.
        for name in list(self._clients):
            await self._close(name)
        return "Stopped listening. The pairings are kept."

    def _status(self) -> str:
        """Whether the library is present, and what is paired and connected."""
        if not self._have_lib():
            return MISSING_LIB
        connected = sum(
            1 for client in self._clients.values() if getattr(client, "is_connected", False)
        )
        return (
            f"{len(self._known_buttons)} paired, "
            f"{connected} connected, listening={self._listening}."
        )

    def _button(self, name: str) -> FlicButton | None:
        """The paired button by that name, or None."""
        wanted = slug(name)
        for button in self._known_buttons:
            if button.name == wanted or button.serial_number.lower() == name.strip().lower():
                return button
        return None

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

        taken = {button.name for button in self._known_buttons}
        return FlicButton(
            name=unique_name(name or f"flic-{len(self._known_buttons) + 1}", taken),
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
        client_cls = self._client_cls
        if client_cls is None:
            return False
        if button.name in self._clients:
            return bool(getattr(self._clients[button.name], "is_connected", False))
        if device is None:
            device = await self._locate(button.address)
        client = client_cls(
            address=button.address,
            ble_device=device,
            pairing_id=button.pairing_id,
            pairing_key=button.pairing_key,
            serial_number=button.serial_number,
            sig_bits=button.sig_bits,
        )
        client.on_button_event = self._make_press_handler(button.name)
        client.register_state_callback(self._make_state_handler(button.name))
        self._clients[button.name] = client
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

    async def _discover(self) -> list[Any]:
        """Flic buttons in range, one scan at a time."""
        async with self._scan_lock:
            return await discover_buttons(SCAN_TIMEOUT_S)

    async def _locate(self, address: str) -> Any:
        """The `BLEDevice` for one known button, or None when it is not in range."""
        async with self._scan_lock:
            try:
                return await find_button(address, FIND_TIMEOUT_S)
            except Exception:
                LOG.debug("[%s] Looking for %s failed", self.name, address, exc_info=True)
                return None

    def _ensure_finder(self) -> None:
        """Start looking for unconnected buttons, unless something already is."""
        if self._listening and (self._finder is None or self._finder.done()):
            self._finder = asyncio.create_task(self._find_missing())

    async def _find_missing(self) -> None:
        """Hand a fresh device to every listening client that is not connected.

        Home Assistant does this from every advertisement it hears; here it is a
        lookup by address, backing off while a button stays away. A device that
        went stale while the button was gone is replaced the same way. Ends when
        every button is connected, and starts again when one drops.
        """
        delay = FIND_INTERVAL_S
        while self._listening:
            waiting = [
                (name, client)
                for name, client in self._clients.items()
                if not getattr(client, "is_connected", False)
            ]
            if not waiting:
                return
            for name, client in waiting:
                button = self._button(name)
                if button is None:
                    continue
                device = await self._locate(button.address)
                if device is not None and self._clients.get(name) is client:
                    client.set_ble_device(device)
            await asyncio.sleep(delay)
            delay = min(delay * 2, FIND_MAX_INTERVAL_S)

    async def _stop_button(self, button: FlicButton) -> None:
        """Stop listening to one button, leaving its pairing alone."""
        if button.name in self._clients:
            await self._close(button.name)
            await self._publish_state(button, connected=False)

    async def _close(self, name: str) -> None:
        """Drop a client, whether or not it manages to say goodbye."""
        client = self._clients.pop(name, None)
        if client is None:
            return
        try:
            await asyncio.wait_for(client.stop(), timeout=STOP_TIMEOUT_S)
        except Exception:
            # `stop()` can stall while disconnecting, and a shutdown that waits
            # on a radio is a shutdown that does not finish. The client is
            # dropped either way.
            LOG.warning("[%s] '%s' did not stop cleanly", self.name, name, exc_info=True)

    @staticmethod
    async def _disconnect(client: Any) -> None:
        """Hang up on a client used for one exchange."""
        try:
            await asyncio.wait_for(client.disconnect(), timeout=STOP_TIMEOUT_S)
        except Exception:
            LOG.debug("Disconnect did not complete cleanly", exc_info=True)

    def _make_press_handler(self, name: str):
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
            loop.call_soon_threadsafe(self._presses.put_nowait, (name, kind, at, dict(data)))

        return handler

    def _make_state_handler(self, name: str):
        """A callback for one button's connection coming and going.

        The library reconnects by itself, so the first start is not the only
        moment a button becomes reachable. Handed to the loop like a press, and
        a drop also sets the finder looking, in case the device went stale.
        """

        def handler(state: Any) -> None:
            loop = self._loop
            if loop is None:
                return
            connected = bool(getattr(state, "connected", False))
            loop.call_soon_threadsafe(
                self._presses.put_nowait,
                (name, CONNECTION_EVENT, time.time(), {"connected": connected}),
            )
            if not connected:
                loop.call_soon_threadsafe(self._ensure_finder)

        return handler

    async def _publish_presses(self) -> None:
        """Publish presses as they are handed over, until the agent stops."""
        while True:
            name, kind, at, data = await self._presses.get()
            try:
                if kind == CONNECTION_EVENT:
                    await self._publish_connection(name, bool(data.get("connected")))
                    continue
                await self._publish_press(name, kind, at, data)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("[%s] Could not publish a press from '%s'", self.name, name)

    async def _publish_press(self, name: str, kind: str, at: float, data: dict[str, Any]) -> None:
        """Publish one press, if it is one anybody wired anything to."""
        if kind not in GESTURES:
            self._note_unpublished(kind)
            return
        if data.get("was_queued") and not self.publish_queued:
            return
        button = self._button(name)
        if button is None:
            return
        self._last_press[button.name] = at
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
        await self._mqtt_publish(f"{TOPIC_ROOT}/{topic_key(button)}/{kind}", payload)

    def _note_unpublished(self, kind: str) -> None:
        """Say once that a kind of event arrived that nothing is wired to.

        A Duo's swipes and a Twist's rotation reach the agent and go no further.
        Said once per kind, because a rotation arrives many times a second and
        the point is that it happened at all, not how often.
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

    async def _publish_connection(self, name: str, connected: bool) -> None:
        """Publish a connection change for a button still being listened to."""
        button = self._button(name)
        if button is None or name not in self._clients:
            return
        await self._publish_state(button, connected=connected)

    async def _publish_state(self, button: FlicButton, connected: bool) -> None:
        """Say whether a button is reachable, for whoever asks later."""
        await self._mqtt_publish(
            f"{TOPIC_ROOT}/{topic_key(button)}/state",
            {"connected": connected, "battery": button.battery},
            retain=True,
            # At least once, like the retraction that undoes it: a dropped
            # retained message leaves the broker telling every later subscriber
            # the opposite of where the button is.
            qos=1,
        )

    async def _clear_retained(self, button: FlicButton) -> None:
        """Take back the retained state of a button that is going away.

        An empty retained payload is how MQTT retracts one. Left behind, the
        broker keeps telling every later subscriber about a button that is no
        longer here.
        """
        await self._mqtt_publish(f"{TOPIC_ROOT}/{topic_key(button)}/state", b"", retain=True, qos=1)

    def _have_lib(self) -> bool:
        """Whether the Bluetooth library is importable, looked up once."""
        if not self._checked_lib:
            self._checked_lib = True
            try:
                from pyflic_ble import FlicClient  # pyright: ignore[reportMissingImports]

                self._client_cls = FlicClient
            except ImportError:
                return False
        return self._client_cls is not None

    def _restore(self) -> Any:
        """Whatever the buttons file holds, or an empty list.

        A file that cannot be read is treated as an absent one: buttons can be
        paired again, while refusing to start over a damaged file leaves
        nothing working at all.
        """
        if not self._buttons_json.exists():
            return []
        try:
            return json.loads(self._buttons_json.read_text(encoding="utf-8"))
        except Exception:
            LOG.warning("[%s] Could not read %s", self.name, self._buttons_json, exc_info=True)
            return []

    def _remember(self) -> None:
        """Write the paired buttons back to the private file.

        A failed write is logged rather than raised, so a command that has
        already paired a button still answers; the pairing is live either way
        until the process restarts.
        """
        try:
            payload = {"buttons": [button.to_dict() for button in self._known_buttons]}
            write_private_json(self._buttons_json, payload)
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
    word it has: "pair Lamp Flic" pairs `lamp-flic`. A rename splits old from new
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
    """A name as it appears in a topic: lowercase, no separators of its own."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")


def unique_name(wanted: str, taken: Iterable[str]) -> str:
    """`wanted` as a topic-safe name that nothing else here answers to."""
    base = slug(wanted) or "flic"
    existing = set(taken)
    if base not in existing:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing:
        suffix += 1
    return f"{base}-{suffix}"


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


def topic_key(button: FlicButton) -> str:
    """The segment that identifies one button inside its topics.

    A rename moves a button's topics, and this is the only place that decides
    which ones they are.
    """
    return button.name


def gesture_topics(buttons: Iterable[FlicButton]) -> list[str]:
    """Every topic the paired buttons publish between them.

    One per button per gesture, plus the retained topic carrying whether that
    button is connected. The planner wires from the manifest, so a topic left
    out here cannot be wired to even while the agent publishes it.
    """
    topics: list[str] = []
    for button in buttons:
        base = f"{TOPIC_ROOT}/{topic_key(button)}"
        topics.extend(f"{base}/{gesture}" for gesture in GESTURES)
        topics.append(f"{base}/state")
    return topics


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
