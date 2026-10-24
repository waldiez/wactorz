"""Which branch listens, speaks and wakes: as configured, and as chosen while running.

``WACTORZ_STT``, ``WACTORZ_TTS`` and ``WACTORZ_WAKE`` are read here rather than
by the extensions they steer, because they are one decision with one ceiling:
the store keeps them in one record and ``POST /api/voice`` changes them together.

The environment sets what a deployment starts as. Some of those choices are ones
a person changes while using it -- which branch listens, which speaks, and in
what voice -- and restarting to try another is a poor way to find out which you
want. Those are kept here, in the same place and the same shape as the spend
limit's override: what is stored supersedes the environment until it is cleared.

**The environment is also the ceiling.** Each branch list runs from doing
nothing to doing the most on this machine -- for listening, up to its own
microphone hearing the room -- and a choice may go as far as the configured
value and no further. A browser holding the API key can turn the room's
microphone down, never up past what the operator set; that decision stays
with the machine, like the addresses below.

**Addresses are deliberately absent.** ``WACTORZ_STT_URI`` and
``WACTORZ_TTS_URI`` name services this process opens connections to, and a
setting a browser can write is a setting a browser can point anywhere it likes,
including at whatever else is reachable from this machine. Where the services
live stays with the machine they were configured on.
"""

import logging
import os
import warnings
from typing import Any

from ...config import _unquote
from ...core.persistence import get_db

logger = logging.getLogger(__name__)

#: The speech-to-text branches, in the order they concede privacy.
#:
#: ``off``      no transcription; the browser hides the microphone.
#: ``browser``  the client transcribes locally and sends only text.
#: ``server``   the browser captures, the host transcribes.
#: ``host``     the host captures from its own microphone and transcribes.
#:
#: ``server`` and ``host`` differ in who owns the microphone, which is what
#: decides whether a secure context is needed: only browser capture calls
#: ``getUserMedia``, so ``host`` is the branch that works over plain HTTP -- and
#: the only one where the server hears the room.
STT_MODES = ("off", "browser", "server", "host")

#: How this deployment speaks, if it does.
#:
#: ``browser`` and ``server`` differ in where the speech is made, and so in
#: whether the words leave this machine at all: the browser's own voice never
#: sends them anywhere, while ``server`` hands them to whatever synthesises. That
#: is a choice a deployment makes rather than one its installed packages make.
#:
#: ``host`` speaks through the server's own audio device rather than the
#: listener's, which is the branch that answers into a room instead of into a
#: page. Only replies to a turn someone started are read aloud, so a machine
#: standing in a room does not narrate its own upkeep.
TTS_MODES = ("off", "browser", "server", "host")

#: Whether the machine listens for a phrase before a turn. Only meaningful on the
#: ``host`` listening branch: it is the one that owns a microphone, and a wake
#: word is how a room without a screen starts a turn.
WAKE_MODES = ("off", "on")


def _branch(variable: str, branches: tuple[str, ...], unset: str) -> str:
    """The branch `variable` names, or `unset` when it names none or no real one."""
    value = _unquote(os.getenv(variable, "") or "").lower()
    if not value:
        return unset
    if value not in branches:
        # Named rather than ignored: a typo here shows up as a microphone that
        # never appears, which looks like a broken feature rather than a setting.
        warnings.warn(
            f"{variable}={value!r} is not one of {', '.join(branches)} — using {unset!r}",
            RuntimeWarning,
            stacklevel=3,
        )
        return unset
    return value


def _stt_mode() -> str:
    """The configured listening branch, or ``off`` when unset or unrecognised."""
    return _branch("WACTORZ_STT", STT_MODES, "off")


def _tts_mode() -> str:
    """The configured speaking branch, or ``server`` when unset.

    Unset means speak if this deployment can, and let the browser cover it when
    it cannot. Saying ``browser`` is how a deployment keeps the words on this
    machine while still having a voice.
    """
    return _branch("WACTORZ_TTS", TTS_MODES, "server")


def _wake_mode() -> str:
    """Whether a phrase wakes this deployment, ``off`` when unset."""
    return _branch("WACTORZ_WAKE", WAKE_MODES, "off")


#: The configured branches: where a deployment starts, and the most a choice
#: may reach. The browser learns them from ``/api/config`` rather than from how
#: the bundle was built, so one wheel serves every deployment.
STT_MODE = _stt_mode()
TTS_MODE = _tts_mode()
WAKE_MODE = _wake_mode()

#: Where the overrides live, beside the other things the system remembers.
_KEY = "_voice_settings"

#: What may be set, each with its branches in order, from doing nothing to
#: doing the most on this machine.
SETTINGS: dict[str, tuple[str, ...]] = {
    "listening": STT_MODES,
    "speaking": TTS_MODES,
    "waking": WAKE_MODES,
}

#: The variable that sets each one's ceiling, named in a refusal so the person
#: refused knows what the operator would change.
VARIABLES = {"listening": "WACTORZ_STT", "speaking": "WACTORZ_TTS", "waking": "WACTORZ_WAKE"}


class NoStoreError(RuntimeError):
    """Raised when a choice is made where nothing can remember it."""

    def __init__(self) -> None:
        super().__init__("this deployment has no store to remember settings in")


def _stored() -> dict[str, Any]:
    """Whatever has been chosen so far, or nothing."""
    db = get_db()
    if db is None:
        return {}
    try:
        held = db.kv_get("_system", _KEY)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # A deployment with no working store still runs on its environment.
        logger.warning("[voice] Could not read the stored settings: %s", exc)
        return {}
    return held if isinstance(held, dict) else {}


def listening() -> str:
    """Which branch listens, as chosen or as configured."""
    return _resolved("listening")


def speaking() -> str:
    """Which branch speaks, as chosen or as configured."""
    return _resolved("speaking")


def waking() -> str:
    """Whether a phrase starts a turn, as chosen or as configured."""
    return _resolved("waking")


def ceiling(setting: str) -> str:
    """The most this deployment lets `setting` do: what its variable says.

    Read from this module on every call rather than bound into a table at
    import, so a test can pin it.
    """
    return {"listening": STT_MODE, "speaking": TTS_MODE, "waking": WAKE_MODE}[setting]


def allowed(setting: str) -> tuple[str, ...]:
    """The branches `setting` may be set to here, in order, up to its ceiling."""
    branches = SETTINGS[setting]
    return branches[: branches.index(ceiling(setting)) + 1]


def voice() -> str:
    """The voice to speak in, as chosen or as configured. Empty means default."""
    chosen = _stored().get("voice")
    if isinstance(chosen, str):
        return chosen.strip()
    return ""


def _resolved(setting: str) -> str:
    """One setting, preferring what was chosen over what was configured."""
    chosen = _stored().get(setting)
    # Checked rather than trusted: what is stored outlives the release that wrote
    # it, and a branch this version does not have would be worse than the default.
    # Checked against the ceiling too, which the operator may have lowered since
    # the choice was made. The choice is kept rather than dropped, so raising the
    # ceiling again brings it back.
    if isinstance(chosen, str) and chosen in allowed(setting):
        return chosen
    return ceiling(setting)


def refuses(setting: str, value: str) -> str:
    """Why this cannot be set, or empty when it can.

    Separate from setting it, so a request naming several can be judged whole
    before any of it is kept.
    """
    if setting == "voice":
        return ""
    if setting not in SETTINGS:
        return f"no such setting: {setting}"
    if value not in SETTINGS[setting]:
        return f"{setting} cannot be {value!r}; one of {', '.join(SETTINGS[setting])}"
    if value not in allowed(setting):
        return (
            f"{setting} cannot be {value!r} here: this deployment allows up to "
            f"{ceiling(setting)!r} ({VARIABLES[setting]})"
        )
    return ""


def choose(setting: str, value: str) -> None:
    """Change one setting for as long as this deployment keeps its store.

    Raises `ValueError` for a setting or value this version does not have, so a
    typo is refused rather than quietly stored and ignored for ever, and for a
    branch above what the environment allows.
    """
    problem = refuses(setting, value)
    if problem:
        raise ValueError(problem)
    _write({**_stored(), setting: value.strip() if setting == "voice" else value})


def forget() -> None:
    """Drop every choice, so the environment decides again."""
    _write({})


def _write(settings: dict[str, Any]) -> None:
    """Store the chosen settings, or say why they will not be remembered."""
    db = get_db()
    if db is None:
        raise NoStoreError
    db.kv_set("_system", _KEY, settings)
