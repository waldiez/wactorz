"""Where the compose stack's alerts go is decided in `.env`, and nowhere by default.

Prometheus raises the alerts; Alertmanager, started beside it, delivers them.
Its configuration is written when its container starts, by a shell script, from
two settings: the webhook to POST alerts to, and a bearer token for it. With no
webhook nothing is delivered, so an install that has not said where alerts go
sends them nowhere. A configuration file of the operator's own replaces all of
that, for what one webhook cannot express.

The script is run for real, with `sh`: what it writes, and the permissions it
writes it with, are shell behaviour.
"""

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "alertmanager" / "render-config.sh"

#: The compose files that run the monitoring services.
STACKS = ["compose.yaml", "compose.dev.yaml"]

needs_sh = pytest.mark.skipif(
    sys.platform == "win32", reason="runs through sh, which Windows lacks"
)


def _render(out: Path, own: Path | None = None, **settings: str) -> tuple[dict, str]:
    """Run the script as the container does; its configuration and what it said."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("ALERT")}
    env.update(settings)
    # Somewhere with no file, unless the test supplies one: a developer's own
    # configuration beside the script must not decide what these tests see.
    env["ALERTMANAGER_OWN_CONFIG"] = str(own if own is not None else out / "absent.yml")
    done = subprocess.run(  # a script in this repository
        ["sh", str(SCRIPT), str(out)],  # sh is found on PATH on purpose
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    return yaml.safe_load((out / "alertmanager.yml").read_text(encoding="utf-8")), done.stdout


def _receiver(config: dict) -> dict:
    (receiver,) = config["receivers"]
    assert config["route"]["receiver"] == receiver["name"]
    return receiver


@needs_sh
class TestWhereAlertsGo:
    def test_nowhere_until_a_webhook_is_named(self, tmp_path: Path) -> None:
        config, said = _render(tmp_path)

        assert _receiver(config) == {"name": "default"}
        assert "sent nowhere" in said
        assert sorted(path.name for path in tmp_path.iterdir()) == ["alertmanager.yml"]

    def test_to_the_webhook_in_the_setting(self, tmp_path: Path) -> None:
        config, _said = _render(tmp_path, ALERT_WEBHOOK_URL="https://example.test/hook")

        (webhook,) = _receiver(config)["webhook_configs"]
        assert webhook["url_file"] == str(tmp_path / "webhook_url")
        assert webhook["send_resolved"] is True
        assert "http_config" not in webhook
        assert (tmp_path / "webhook_url").read_text(encoding="utf-8") == "https://example.test/hook"

    def test_with_the_token_as_a_bearer_credential(self, tmp_path: Path) -> None:
        config, _said = _render(
            tmp_path, ALERT_WEBHOOK_URL="https://example.test/hook", ALERT_WEBHOOK_TOKEN="s3cret"
        )

        (webhook,) = _receiver(config)["webhook_configs"]
        assert webhook["http_config"]["authorization"] == {
            "type": "Bearer",
            "credentials_file": str(tmp_path / "webhook_token"),
        }
        assert (tmp_path / "webhook_token").read_text(encoding="utf-8") == "s3cret"

    def test_a_token_with_no_webhook_changes_nothing(self, tmp_path: Path) -> None:
        config, _said = _render(tmp_path, ALERT_WEBHOOK_TOKEN="s3cret")

        assert _receiver(config) == {"name": "default"}
        assert not (tmp_path / "webhook_token").exists()


@needs_sh
class TestTheAddressAndTheTokenStayOutOfTheConfiguration:
    #: Characters that would end or change a YAML value written inline.
    URL = 'https://example.test/hook?key=a\'b&c="d" #e: f'
    TOKEN = "&anchor: 'x' #y"

    def test_they_are_kept_whole_in_files_the_configuration_names(self, tmp_path: Path) -> None:
        _render(tmp_path, ALERT_WEBHOOK_URL=self.URL, ALERT_WEBHOOK_TOKEN=self.TOKEN)

        assert (tmp_path / "webhook_url").read_text(encoding="utf-8") == self.URL
        assert (tmp_path / "webhook_token").read_text(encoding="utf-8") == self.TOKEN

    def test_neither_appears_in_what_alertmanager_shows(self, tmp_path: Path) -> None:
        # Alertmanager serves its configuration on its status page.
        _render(tmp_path, ALERT_WEBHOOK_URL=self.URL, ALERT_WEBHOOK_TOKEN=self.TOKEN)

        shown = (tmp_path / "alertmanager.yml").read_text(encoding="utf-8")
        assert "example.test" not in shown
        assert "anchor" not in shown

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_what_is_written_is_readable_by_its_owner_only(self, tmp_path: Path) -> None:
        _render(tmp_path, ALERT_WEBHOOK_URL=self.URL, ALERT_WEBHOOK_TOKEN=self.TOKEN)

        for name in ("alertmanager.yml", "webhook_url", "webhook_token"):
            assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600, name


@needs_sh
class TestAConfigurationOfYourOwn:
    def test_it_is_used_as_it_is_and_the_settings_are_not_read(self, tmp_path: Path) -> None:
        own = tmp_path / "mine.yml"
        own.write_text("route: {receiver: team}\nreceivers:\n  - name: team\n", encoding="utf-8")
        out = tmp_path / "out"

        config, said = _render(out, own=own, ALERT_WEBHOOK_URL="https://example.test/hook")

        assert config == {"route": {"receiver": "team"}, "receivers": [{"name": "team"}]}
        assert not (out / "webhook_url").exists()
        assert "ALERT_WEBHOOK_URL is not read" in said

    def test_the_place_it_is_looked_for_is_not_tracked(self) -> None:
        # It names where alerts go, often with a credential in it.
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert "infra/alertmanager/alertmanager.yml" in ignored


# ── The stack around it ────────────────────────────────────────────────────────


def _services(name: str) -> dict[str, dict]:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))["services"]


@pytest.mark.parametrize("name", STACKS)
class TestTheComposeService:
    def test_it_is_given_the_two_settings_empty_by_default(self, name: str) -> None:
        environment = _services(name)["alertmanager"]["environment"]

        assert environment == {
            "ALERT_WEBHOOK_URL": "${ALERT_WEBHOOK_URL:-}",
            "ALERT_WEBHOOK_TOKEN": "${ALERT_WEBHOOK_TOKEN:-}",
        }

    def test_it_writes_its_configuration_where_nothing_is_kept(self, name: str) -> None:
        service = _services(name)["alertmanager"]
        (command,) = service["command"]

        assert service["read_only"] is True
        assert "/run/alertmanager" in service["tmpfs"]
        assert "render-config.sh /run/alertmanager" in command
        assert "--config.file=/run/alertmanager/alertmanager.yml" in command

    def test_the_image_is_pinned_by_digest(self, name: str) -> None:
        image = _services(name)["alertmanager"]["image"]

        assert re.fullmatch(r"prom/alertmanager:v[\d.]+@sha256:[0-9a-f]{64}", image), image

    def test_prometheus_waits_for_it_and_shares_its_profile(self, name: str) -> None:
        services = _services(name)

        assert "alertmanager" in services["prometheus"]["depends_on"]
        assert services["alertmanager"]["profiles"] == services["prometheus"]["profiles"]


def test_both_stacks_run_the_same_alertmanager() -> None:
    first, second = (_services(name)["alertmanager"] for name in STACKS)
    assert {**first, "profiles": None} == {**second, "profiles": None}


def test_prometheus_hands_its_alerts_to_that_service() -> None:
    template = yaml.safe_load(
        (ROOT / "infra" / "prometheus" / "prometheus.yml").read_text(encoding="utf-8")
    )
    (manager,) = template["alerting"]["alertmanagers"]
    (static,) = manager["static_configs"]

    # The service's name on the compose network, and the port Alertmanager
    # listens on unless told otherwise, which the command does not.
    assert static["targets"] == ["alertmanager:9093"]
    for name in STACKS:
        (command,) = _services(name)["alertmanager"]["command"]
        assert "--web.listen-address" not in command
