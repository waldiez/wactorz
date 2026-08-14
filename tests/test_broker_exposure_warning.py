"""What is said at startup about a broker that is not on this machine.

The risk is not "unencrypted telemetry". The remote runner **executes code
delivered over the broker** (`nodes/<name>/spawn`), so anyone who can read the
wire sees that code and anyone who can write to it runs code on every node.
Plaintext plus anonymous is remote code execution offered to a network segment.

There is no TLS support in this client, so a non-loopback broker is always
plaintext — the warning says so rather than implying a setting exists to turn it
on. This is what keeps deferring TLS honest instead of silent.
"""

import pytest

from wactorz.core.mqtt import broker_exposure_warning
from wactorz.core.net import is_loopback as _is_loopback


class TestWhatCountsAsLocal:
    @pytest.mark.parametrize(
        "host",
        ["localhost", "LOCALHOST", "localhost.localdomain", "127.0.0.1", "::1", "[::1]", "", "  "],
    )
    def test_traffic_that_never_leaves_the_machine(self, host: str) -> None:
        assert _is_loopback(host)

    def test_the_whole_loopback_range_counts(self) -> None:
        # 127.0.0.1 is not the only one — systemd-resolved sits on 127.0.0.53.
        assert _is_loopback("127.0.0.53")
        assert _is_loopback("127.1.2.3")

    @pytest.mark.parametrize("host", ["10.0.0.5", "192.168.1.7", "broker.lan", "mqtt.example.com"])
    def test_anything_else_is_remote(self, host: str) -> None:
        assert not _is_loopback(host)

    def test_an_unresolvable_name_is_treated_as_remote(self) -> None:
        # Warning about a broker that turns out to be local wastes a line;
        # staying quiet about one that is not is the failure this prevents.
        assert not _is_loopback("some-host-we-cannot-classify")


class TestTheWarning:
    def test_a_local_broker_says_nothing(self) -> None:
        assert broker_exposure_warning("localhost", "") is None
        assert broker_exposure_warning("127.0.0.1", "user") is None

    def test_a_remote_broker_names_what_travels(self) -> None:
        message = broker_exposure_warning("10.0.0.5", "wactorz")

        assert message is not None
        assert "10.0.0.5" in message
        assert "unencrypted" in message
        # The point of the whole warning: not telemetry, code.
        assert "code spawned agents run" in message

    def test_an_anonymous_broker_says_the_worse_half_too(self) -> None:
        message = broker_exposure_warning("10.0.0.5", "")

        assert message is not None
        assert "unauthenticated" in message
        assert "run code on every node" in message
        assert "MQTT_USERNAME" in message

    def test_an_authenticated_broker_does_not_claim_otherwise(self) -> None:
        # Still plaintext, but telling someone to set credentials they have
        # already set is how a warning gets ignored.
        message = broker_exposure_warning("10.0.0.5", "wactorz")

        assert message is not None
        assert "unauthenticated" not in message
        assert "MQTT_USERNAME" not in message

    @pytest.mark.parametrize("username", ["", "   "])
    def test_whitespace_is_not_a_username(self, username: str) -> None:
        message = broker_exposure_warning("10.0.0.5", username)

        assert message is not None and "unauthenticated" in message
