"""Secret redaction on the logging path.

The regression these pin: a secret that reaches a log record is readable by
anything that can read the buffer, and a redactor that misses a shape fails
*silently* — nothing errors, the credential is simply there. So each shape gets
its own test, and the shapes that occur here are the machine-authored ones
(URL userinfo, dict reprs, env dumps), not just ``password=``.

The backtracking test is not a style check: these patterns run over every log
record, agent output gets logged, and an agent can be induced to emit an
adversarial string. Catastrophic backtracking would hang the logging path and
take the process with it.
"""

import logging
import time

import pytest

from wactorz.monitoring.log_redaction import (
    REDACTED,
    SecretRedactingFilter,
    install_redaction,
    redact,
    redacted_message,
)


def record(msg: str, *args: object) -> logging.LogRecord:
    """A log record as ``logging`` would build it, args unmerged."""
    return logging.LogRecord(
        name="wactorz.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args or None,
        exc_info=None,
    )


def scrub(msg: str, *args: object) -> str:
    """The redacted, args-merged message for one record."""
    cleaned = redacted_message(record(msg, *args))
    assert cleaned is not None
    return cleaned


class TestAssignmentPairs:
    @pytest.mark.parametrize(
        "key",
        ["password", "passwd", "pwd", "token", "api_key", "apikey", "secret", "access_key"],
    )
    def test_key_equals_value(self, key: str) -> None:
        out = scrub(f"connecting with {key}=hunter2 to broker")
        assert "hunter2" not in out
        assert REDACTED in out
        assert "to broker" in out

    def test_case_insensitive(self) -> None:
        assert "hunter2" not in scrub("PASSWORD=hunter2")

    def test_prefixed_env_name(self) -> None:
        """``MQTT_PASSWORD=`` has no word boundary before ``PASSWORD``."""
        out = scrub("env: MQTT_PASSWORD=hunter2 INFLUX_TOKEN=abc123")
        assert "hunter2" not in out
        assert "abc123" not in out

    def test_stops_at_delimiter(self) -> None:
        """Redaction must not swallow the rest of the line."""
        out = scrub("password=hunter2, host=broker.local")
        assert "hunter2" not in out
        assert "broker.local" in out

    def test_spaces_around_equals(self) -> None:
        assert "hunter2" not in scrub("token = hunter2")


class TestHeaders:
    def test_authorization_bearer(self) -> None:
        out = scrub("GET /api/actors Authorization: Bearer eyJhbGciOi.abc.def")
        assert "eyJhbGciOi.abc.def" not in out
        assert "/api/actors" in out

    def test_bare_bearer(self) -> None:
        assert "sk-live-9f3a" not in scrub("retrying with Bearer sk-live-9f3a")

    def test_basic(self) -> None:
        assert "dXNlcjpwYXNz" not in scrub("Authorization: Basic dXNlcjpwYXNz")


class TestPrivateKeys:
    def test_full_block(self) -> None:
        out = scrub(
            "loaded key:\n"
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAA\n"
            "-----END OPENSSH PRIVATE KEY-----\n"
            "done"
        )
        assert "b3BlbnNzaC1rZXktdjEAAAAA" not in out
        assert "done" in out

    def test_unterminated_block(self) -> None:
        """A truncated key must not leak its body for want of an END marker."""
        out = scrub("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA9f3a\n")
        assert "MIIEowIBAAKCAQEA9f3a" not in out


class TestUrlUserinfo:
    def test_broker_url(self) -> None:
        """This codebase's own shape — the helper builds broker URLs like this."""
        out = scrub("connecting to mqtt://wactorz:s3cr3t@broker.local:1883")
        assert "s3cr3t" not in out
        assert "broker.local:1883" in out
        assert "wactorz" in out, "the username is not the secret; keep it readable"

    def test_https_url(self) -> None:
        assert "p4ss" not in scrub("POST https://admin:p4ss@influx.local/write")

    def test_url_without_credentials_untouched(self) -> None:
        url = "mqtt://broker.local:1883"
        assert scrub(f"connecting to {url}") == f"connecting to {url}"


class TestDictRepr:
    def test_single_quoted(self) -> None:
        out = scrub("payload={'host': 'rpi', 'api_key': 'sk-live-9f3a'}")
        assert "sk-live-9f3a" not in out
        assert "rpi" in out

    def test_double_quoted(self) -> None:
        assert "sk-live" not in scrub('payload={"token": "sk-live-9f3a"}')

    def test_mixed_with_other_keys(self) -> None:
        out = scrub("{'user': 'pi', 'password': 'hunter2', 'port': 22}")
        assert "hunter2" not in out
        assert "'pi'" in out
        assert "22" in out


class TestLeavesOrdinaryLinesAlone:
    @pytest.mark.parametrize(
        "line",
        [
            "[Supervisor] Stopped.",
            "actor spawned: temperature-monitor (rpi-node)",
            "GET /api/actors 200 in 4ms",
            "token bucket refilled",  # 'token' as a word, not an assignment
            "mqtt://broker.local:1883 connected",
            "cost limit reached: 5.00 USD of 5.00",
        ],
    )
    def test_unchanged(self, line: str) -> None:
        assert scrub(line) == line


class TestRecordHandling:
    def test_secret_in_args_is_redacted(self) -> None:
        """``msg`` alone is not the message — %-args carry values too."""
        out = scrub("connecting with %s", "password=hunter2")
        assert "hunter2" not in out

    def test_broken_record_returns_none(self) -> None:
        """Raising on the logging path would break logging itself."""
        assert redacted_message(record("%d items", "not-a-number")) is None

    def test_non_string_msg(self) -> None:
        assert redacted_message(record(12345)) == "12345"  # type: ignore[arg-type]

    def test_record_is_not_mutated(self) -> None:
        """Callers read a copy; what other handlers write is unaffected."""
        rec = record("password=hunter2")
        redacted_message(rec)
        assert rec.getMessage() == "password=hunter2"


class TestIdempotent:
    """The filter mutates the record and runs once per handler.

    A pattern that re-matches its own placeholder corrupts the message a little
    more on each pass — ``password=[redacted]]``, then ``]]``, and so on.
    """

    @pytest.mark.parametrize(
        "line",
        [
            "password=hunter2",
            "MQTT_PASSWORD=hunter2 host=broker",
            "{'api_key': 'sk-live-9f3a'}",
            "Authorization: Bearer eyJ.abc",
            "mqtt://user:s3cr3t@broker.local:1883",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----",
        ],
    )
    def test_second_pass_changes_nothing(self, line: str) -> None:
        once = redact(line)
        assert redact(once) == once

    def test_stable_over_many_passes(self) -> None:
        text = redact("password=hunter2")
        for _ in range(5):
            text = redact(text)
        assert text == "password=[redacted]"


class TestInstallRedaction:
    """Wiring: the console and the log file get filtered, not just the buffer."""

    @pytest.fixture
    def root_with_handler(self):
        root = logging.getLogger()
        handler = logging.NullHandler()
        root.addHandler(handler)
        yield root, handler
        root.removeHandler(handler)

    def test_attaches_to_existing_handlers(self, root_with_handler) -> None:
        _, handler = root_with_handler
        install_redaction()
        assert any(isinstance(f, SecretRedactingFilter) for f in handler.filters)

    def test_idempotent(self, root_with_handler) -> None:
        """Called twice, a handler must not end up filtering twice."""
        _, handler = root_with_handler
        install_redaction()
        install_redaction()
        filters = [f for f in handler.filters if isinstance(f, SecretRedactingFilter)]
        assert len(filters) == 1

    def test_handler_output_is_redacted(self, root_with_handler) -> None:
        seen: list[str] = []

        class Capture(logging.Handler):
            def emit(self, rec: logging.LogRecord) -> None:
                seen.append(rec.getMessage())

        root, _ = root_with_handler
        capture = Capture()
        root.addHandler(capture)
        try:
            install_redaction()
            logging.getLogger("wactorz.test.redaction").warning("password=hunter2")
        finally:
            root.removeHandler(capture)
        assert seen and all("hunter2" not in line for line in seen)


class TestFilterRecordHandling:
    def test_never_drops_records(self) -> None:
        assert SecretRedactingFilter().filter(record("anything")) is True

    def test_args_cleared_after_merge(self) -> None:
        """Redacting merged text means the record must stop re-merging."""
        rec = record("connecting with %s", "password=hunter2")
        SecretRedactingFilter().filter(rec)
        assert rec.args in (None, ())
        assert "hunter2" not in rec.getMessage()

    def test_survives_a_broken_record(self) -> None:
        """A raising filter would break logging itself — fail open, not closed."""
        assert SecretRedactingFilter().filter(record("%d items", "not-a-number")) is True


class TestNoCatastrophicBacktracking:
    """Agent output reaches the log, so the patterns meet adversarial input."""

    @pytest.mark.parametrize(
        "hostile",
        [
            "password=" + "a" * 20_000,
            "token=" + "a=" * 10_000,
            "mqtt://" + "u:" * 10_000 + "@host",
            "-----BEGIN RSA PRIVATE KEY-----" + "A" * 20_000,
            "{'api_key': '" + "x" * 20_000,
            "Bearer " + "-" * 20_000,
            " " * 20_000 + "password=x",
        ],
    )
    def test_completes_promptly(self, hostile: str) -> None:
        started = time.perf_counter()
        scrub(hostile)
        assert time.perf_counter() - started < 1.0, "pattern backtracks on hostile input"
