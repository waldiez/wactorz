"""The operations the model may ask a node for, by name.

Free text never reaches a node's shell from the model: a command typed by the
operator does, through `/nodes run`, and the model has these instead. Each is a
fixed command that only reads; what may vary is a number, checked against its
bounds, and the node's own name, quoted. The table is data in one module that
nodes install too, so it can later be checked on the node as well as here.
"""

import shlex

import pytest

from wactorz.agents.prompts.main_actor_prompts import orchestrator_prompt
from wactorz.core.node_operations import OPERATIONS, OperationRefused, command_for

AGREED = {
    "uptime",
    "disk",
    "memory",
    "temperature",
    "os",
    "network",
    "python",
    "packages",
    "node-log",
}

#: What none of them may do: change the machine or what runs on it.
DISRUPTIVE = (
    "reboot",
    "shutdown",
    "poweroff",
    "kill",
    "rm ",
    "systemctl",
    "apt",
    "pip install",
    "sudo",
)


class TestTheTable:
    def test_it_holds_the_agreed_operations(self) -> None:
        assert set(OPERATIONS) == AGREED

    @pytest.mark.parametrize("name", sorted(AGREED))
    def test_none_of_them_changes_anything(self, name: str) -> None:
        command = command_for(name, {}, "rpi-kitchen")

        assert not [word for word in DISRUPTIVE if word in command]

    @pytest.mark.parametrize("name", sorted(AGREED))
    def test_each_says_what_it_is_for(self, name: str) -> None:
        assert OPERATIONS[name].summary


class TestRefusing:
    def test_a_name_not_in_the_table(self) -> None:
        with pytest.raises(OperationRefused, match="not an operation"):
            command_for("reboot", {}, "rpi")

    def test_a_parameter_the_operation_does_not_take(self) -> None:
        with pytest.raises(OperationRefused, match="takes no 'path'"):
            command_for("disk", {"path": "/etc"}, "rpi")

    @pytest.mark.parametrize("lines", [0, 10_000, "50; reboot", 2.5, True, None])
    def test_a_number_out_of_its_bounds_or_not_a_number(self, lines: object) -> None:
        with pytest.raises(OperationRefused, match="lines"):
            command_for("node-log", {"lines": lines}, "rpi")


class TestTheNodeLog:
    def test_it_reads_as_many_lines_as_asked(self) -> None:
        assert "-n 20" in command_for("node-log", {"lines": 20}, "rpi")

    def test_a_number_given_as_text_is_read_as_one(self) -> None:
        assert "-n 20" in command_for("node-log", {"lines": "20"}, "rpi")

    def test_without_a_number_it_reads_its_default(self) -> None:
        default = OPERATIONS["node-log"].params["lines"].default

        assert f"-n {default}" in command_for("node-log", {}, "rpi")

    def test_the_nodes_name_reaches_the_shell_quoted(self) -> None:
        command = command_for("node-log", {}, "x; reboot")

        assert shlex.quote("wactorz/x; reboot.log") in command


class TestWhatTheModelIsTold:
    def test_main_offers_each_operation_and_no_free_command(self) -> None:
        prompt = orchestrator_prompt()

        assert "node_op" in prompt
        assert all(f"{name}: {OPERATIONS[name].summary}" in prompt for name in AGREED)
        assert "node_run" not in prompt
