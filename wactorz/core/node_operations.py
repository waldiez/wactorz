"""What the model may ask a node for: named operations that only read.

A node's shell is reachable over SSH from main's installer, and main's model
reads untrusted text by design -- mail, web pages, device names -- so a command
it writes is only as trustworthy as everything it read. It is never sent one.
It asks for an operation by name instead, and what runs is that operation's
fixed command, with at most a number in it that is checked against its bounds,
and the node's own name, quoted. A command the operator types reaches a node
through `/nodes run`, which the model cannot send.

The table is data in the package nodes install as well, so a node can check a
request against it too: the same names, the same commands, the same bounds.
Every command runs from the node user's home directory, where a deploy puts
`wactorz/` and its virtualenv.
"""

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: How long a command the operator typed may run on a node, in seconds. Generous:
#: the bug prevented is a wait with no end, and an `apt` run takes minutes.
COMMAND_TIMEOUT_S = 300

#: How long a named operation may run on a node, in seconds. Each only reads.
OPERATION_TIMEOUT_S = 60

#: How much of a command's output comes back, in characters: a reply in chat.
OUTPUT_CHARS = 4000


class OperationRefused(ValueError):
    """An operation, or a parameter for one, that is not in the table."""

    @classmethod
    def unknown(cls, name: str) -> "OperationRefused":
        return cls(f"{name!r} is not an operation; the operations are {', '.join(OPERATIONS)}")

    @classmethod
    def takes_no(cls, name: str, key: str) -> "OperationRefused":
        return cls(f"{name} takes no {key!r}")

    @classmethod
    def out_of_bounds(cls, name: str, key: str, low: int, high: int) -> "OperationRefused":
        return cls(f"{name}: {key} must be a whole number from {low} to {high}")


@dataclass(frozen=True)
class Bounded:
    """A whole number an operation takes, and the range it must fall in."""

    default: int
    low: int
    high: int

    def read(self, name: str, key: str, value: Any) -> int:
        """``value`` as the number it names, or why it is not one in range."""
        if value is None:
            raise OperationRefused.out_of_bounds(name, key, self.low, self.high)
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise OperationRefused.out_of_bounds(name, key, self.low, self.high)
        text = str(value).strip()
        if not text.isdigit():
            raise OperationRefused.out_of_bounds(name, key, self.low, self.high)
        number = int(text)
        if not self.low <= number <= self.high:
            raise OperationRefused.out_of_bounds(name, key, self.low, self.high)
        return number


@dataclass(frozen=True)
class NodeOperation:
    """One operation: what it is for, and the command it runs.

    ``command`` is a shell template. Its fields are the operation's parameters,
    each read through its `Bounded`, and ``log``, the node's log file quoted.
    """

    summary: str
    command: str
    params: Mapping[str, Bounded] = field(default_factory=dict)


#: Where a node's runner logs: the journal under either systemd unit, or the
#: file a deploy's unsupervised fallback writes. Which one is not known from
#: here, so all three are read and the last lines kept.
_NODE_LOG = (
    "{{ journalctl -u wactorz-node.service -n {lines} --no-pager -o cat -q 2>/dev/null; "
    "journalctl --user-unit=wactorz-node.service -n {lines} --no-pager -o cat -q 2>/dev/null; "
    "tail -n {lines} {log} 2>/dev/null; }} | tail -n {lines}"
)

#: Every zone the kernel reports, as ``type millidegrees``.
_TEMPERATURE = (
    "for zone in /sys/class/thermal/thermal_zone*; do "
    '[ -r "$zone/temp" ] && echo "$(cat "$zone/type") $(cat "$zone/temp")"; '
    "done; true"
)

OPERATIONS: Mapping[str, NodeOperation] = {
    "uptime": NodeOperation("how long the node has been up, and its load", "uptime"),
    "disk": NodeOperation("free and used space on each filesystem", "df -h"),
    "memory": NodeOperation("free and used memory, in MB", "free -m"),
    "temperature": NodeOperation(
        "each thermal zone's temperature, in thousandths of a degree C", _TEMPERATURE
    ),
    "os": NodeOperation("the operating system and kernel", "cat /etc/os-release; uname -a"),
    "network": NodeOperation("each network interface and its addresses", "ip -brief address"),
    "python": NodeOperation(
        "the Python the node's runner uses", "wactorz/venv/bin/python --version"
    ),
    "packages": NodeOperation(
        "the Python packages installed for the node's runner",
        "wactorz/venv/bin/python -m pip list --format=freeze",
    ),
    "node-log": NodeOperation(
        "the last lines the node's runner logged (lines: 1-200, default 50)",
        _NODE_LOG,
        {"lines": Bounded(default=50, low=1, high=200)},
    ),
}


def command_for(name: str, params: Mapping[str, Any] | None, node_name: str) -> str:
    """The shell command for operation ``name`` on ``node_name``.

    Raises `OperationRefused` for a name not in the table, a parameter the
    operation does not take, or one outside its bounds. Nothing the caller
    supplies reaches the command but checked numbers and the quoted node name.
    """
    operation = OPERATIONS.get(name)
    if operation is None:
        raise OperationRefused.unknown(name)
    given = dict(params or {})
    unknown = sorted(set(given) - set(operation.params))
    if unknown:
        raise OperationRefused.takes_no(name, unknown[0])
    values = {
        key: bound.read(name, key, given.get(key, bound.default))
        for key, bound in operation.params.items()
    }
    return operation.command.format(log=shlex.quote(f"wactorz/{node_name}.log"), **values)


def described() -> str:
    """One line per operation, ``name: summary``, for a prompt or a help text."""
    return "\n".join(f"  {name}: {op.summary}" for name, op in OPERATIONS.items())
