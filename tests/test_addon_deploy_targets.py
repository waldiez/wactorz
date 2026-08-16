"""Every deploy-target field is reachable from the Home Assistant add-on.

A `DeployTarget` field is configured by three files that have no way of knowing
about each other: the dataclass reads `DEPLOY_<SLUG>_<FIELD>` from the
environment, the add-on's `config.yaml` decides which keys the Supervisor will
accept, and `run.sh` decides which of those keys become environment variables.

`broker_user` and `broker_password` were added to the dataclass and to nothing
else, so per-node broker accounts existed in Wactorz and were unreachable from
the add-on — every node silently got the server's account instead. Nothing
failed; the option simply was not there. These tests are the check that would
have caught it.
"""

import dataclasses
import os
import re

import pytest
import yaml

from wactorz.config import DeployTarget

ADDONS = ["wactorz", "wactorz-ultra"]

#: Add-on option key → dataclass field, where the two deliberately differ.
#: `key` is the option name; `key_path` says what it holds.
ALIASES = {"key": "key_path"}

#: Not configured per target: `name` is the entry's identity and comes from
#: `DEPLOY_TARGETS`, not from a `DEPLOY_<SLUG>_NAME` variable.
NOT_PER_FIELD = {"name"}


def _repo_path(relative: str) -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, relative)


def _schema_fields(addon: str) -> set[str]:
    """The per-target keys the Supervisor will accept for this add-on."""
    with open(_repo_path(f"ha-addon/{addon}/config.yaml")) as handle:
        config = yaml.safe_load(handle)
    return set(config["schema"]["deploy_targets"][0])


def _exported_fields(addon: str) -> set[str]:
    """The keys `run.sh` turns into `DEPLOY_<SLUG>_*` variables.

    Read out of the loop itself rather than by running the script: the point is
    that this list is hand-maintained and drifts, so the test has to read the
    thing that drifts.
    """
    with open(_repo_path(f"ha-addon/{addon}/run.sh")) as handle:
        source = handle.read()
    match = re.search(r"for deploy_field in ([^;]+); do", source)
    assert match is not None, "the deploy_targets field loop moved or was renamed"
    return set(match.group(1).split())


def _dataclass_fields() -> set[str]:
    return {f.name for f in dataclasses.fields(DeployTarget)}


@pytest.mark.parametrize("addon", ADDONS)
class TestTheAddonCanConfigureEveryField:
    def test_every_dataclass_field_is_in_the_schema(self, addon: str) -> None:
        # A field missing here cannot be entered at all: the Supervisor rejects
        # keys the schema does not declare.
        offered = {ALIASES.get(key, key) for key in _schema_fields(addon)}
        assert not _dataclass_fields() - offered - NOT_PER_FIELD

    def test_every_dataclass_field_is_exported_by_run_sh(self, addon: str) -> None:
        # A field in the schema but not in the loop is worse than one that is
        # missing: it can be entered, it validates, and it is silently dropped.
        exported = {ALIASES.get(key, key) for key in _exported_fields(addon)}
        assert not _dataclass_fields() - exported - NOT_PER_FIELD

    def test_the_schema_offers_nothing_the_loop_drops(self, addon: str) -> None:
        assert not _schema_fields(addon) - _exported_fields(addon) - NOT_PER_FIELD

    def test_the_loop_exports_nothing_the_schema_refuses(self, addon: str) -> None:
        # The other direction: an exported key nobody can set is dead code, and
        # usually the leftover of a renamed option.
        assert not _exported_fields(addon) - _schema_fields(addon)


class TestBothAddonsAgree:
    def test_the_schemas_match(self) -> None:
        first, second = (_schema_fields(addon) for addon in ADDONS)
        assert first == second

    def test_the_export_loops_match(self) -> None:
        first, second = (_exported_fields(addon) for addon in ADDONS)
        assert first == second


class TestBrokerCredentialsSpecifically:
    """The fields T20 added, named rather than left to the set arithmetic.

    The tests above would go quiet if `DeployTarget` ever lost these; these say
    out loud that a node can carry its own broker account.
    """

    @pytest.mark.parametrize("addon", ADDONS)
    def test_a_node_can_carry_its_own_broker_account(self, addon: str) -> None:
        assert {"broker_user", "broker_password"} <= _schema_fields(addon)
        assert {"broker_user", "broker_password"} <= _exported_fields(addon)

    def test_the_broker_password_stays_out_of_the_repr(self) -> None:
        # The dataclass is frozen and gets logged; a credential in its repr
        # would reach the log file and the dashboard's log view.
        target = DeployTarget(name="rpi-garage", broker_password="s3cret")

        assert "s3cret" not in repr(target)
