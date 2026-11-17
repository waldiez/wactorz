# SPDX-License-Identifier: Apache-2.0
"""What an agent says about the character the dashboard draws for it.

An agent's avatar is a small dictionary carried in its heartbeat: a ``kind``
naming one of the dashboard's ready-made characters, and, for a character of
its own, the parts to draw it from (``accent``, ``outfit``, ``neck``,
``specs``, ``face``, ``top``, ``ant``, ``prop``). The dashboard owns the list
of kinds and parts and draws a character from the agent's name for anything
it does not know, so this module checks the shape of the dictionary and never
its vocabulary: a new part needs no change here.
"""

import logging
import re
from collections.abc import Mapping
from typing import Any

logger: logging.Logger = logging.getLogger(__name__)

#: An avatar as it travels: part name to the part chosen.
Avatar = dict[str, str | int]

MAX_PARTS = 12
MAX_VALUE_LENGTH = 32
_KEY = re.compile(r"[a-z][a-z_]{0,15}")
_VALUE = re.compile(r"[#A-Za-z0-9_-]*")


class AvatarError(ValueError):
    """An avatar that is not a small dictionary of part names and plain values."""

    @classmethod
    def not_a_mapping(cls, value: object) -> "AvatarError":
        """The error for an avatar that is not a dictionary."""
        return cls(f"an avatar is a dictionary of parts, not a {type(value).__name__}")

    @classmethod
    def too_many_parts(cls) -> "AvatarError":
        """The error for an avatar naming more parts than a character has."""
        return cls(f"an avatar names at most {MAX_PARTS} parts")

    @classmethod
    def bad_part(cls, key: object) -> "AvatarError":
        """The error for a part whose name is not a short lower-case word."""
        return cls(f"an avatar's part name is a short lower-case word, not {key!r}")

    @classmethod
    def bad_value(cls, key: str, value: object) -> "AvatarError":
        """The error for a part whose value is not a short plain word, a colour or a small number."""
        return cls(
            f"the avatar's {key!r} is a short word, a #colour or a small number, not {value!r}"
        )


def _checked_value(key: str, value: object) -> str | int:
    if isinstance(value, bool):
        raise AvatarError.bad_value(key, value)
    if isinstance(value, int) and 0 <= value <= 9:
        return value
    if isinstance(value, str) and len(value) <= MAX_VALUE_LENGTH and _VALUE.fullmatch(value):
        return value
    raise AvatarError.bad_value(key, value)


def checked_avatar(value: object) -> Avatar:
    """The avatar in ``value``, copied, or an `AvatarError` saying what is wrong with it."""
    if not isinstance(value, Mapping):
        raise AvatarError.not_a_mapping(value)
    if len(value) > MAX_PARTS:
        raise AvatarError.too_many_parts()
    avatar: Avatar = {}
    for key, part in value.items():
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise AvatarError.bad_part(key)
        avatar[key] = _checked_value(key, part)
    return avatar


def spawn_avatar(config: Mapping[str, Any], name: str) -> Avatar | None:
    """The avatar a spawn config asks for, or None when it asks for none.

    An avatar is decoration: one that fails the check is left out with a
    warning, and the spawn goes on with the character its class or its name
    gives it.
    """
    asked = config.get("avatar")
    if asked is None:
        return None
    try:
        return checked_avatar(asked)
    except AvatarError as problem:
        logger.warning("Spawning '%s' without the avatar it asked for: %s", name, problem)
        return None


__all__ = ["Avatar", "AvatarError", "checked_avatar", "spawn_avatar"]
