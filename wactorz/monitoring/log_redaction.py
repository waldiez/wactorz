"""Secret redaction for the logging path.

Redaction happens at the **sink**, not the call site: a filter here cannot be
forgotten by a future caller, whereas "remember to redact before logging" is
wrong within a release. :func:`setup_logging` attaches it to the stream and file
handlers as they are built, so the console and ``wactorz.log`` are covered, not
only the in-memory buffer.

Three properties this file has to keep, all easy to lose in a later edit:

* **Patterns stay linear.** They run over every log record, agent output reaches
  the log, and an agent can be induced to emit an adversarial string — so
  catastrophic backtracking here hangs the logging path and takes the process
  with it. Bounded quantifiers and negated character classes only; no nested
  quantifiers.
* **Nothing here raises.** Throwing on the logging path breaks the one subsystem
  that has to work while everything else is failing.
* **:func:`redact` is idempotent.** The filter mutates the record and is attached
  to every handler, so one record is redacted once per handler; a pattern that
  re-matches its own placeholder corrupts the message a little more each pass.
"""

import logging
import re

# Rendering a traceback needs a formatter; only ``formatException`` is used.
EXC_FORMATTER = logging.Formatter()


REDACTED = "[redacted]"

# Key names worth hiding, with an optional prefix so environment-variable forms
# match too: `MQTT_PASSWORD=` has no word boundary before `PASSWORD`. The prefix
# is bounded, so the match cost stays proportional to the input.
_PREFIX = r"[A-Za-z0-9_.\-]{0,40}"
_KEYS = r"(?:password|passwd|pwd|token|api[_-]?key|apikey|secret|access[_-]?key|client[_-]?secret)"

# `password=hunter2`, `MQTT_PASSWORD = hunter2`. The value stops at whitespace or
# a structural delimiter so redaction never swallows the rest of the line.
# The negative lookahead keeps redaction idempotent. Without it a second pass
# re-matches the placeholder's own text (the value class excludes ``]``, so
# ``[redacted]`` matches as ``[redacted``) and re-wraps it, growing a bracket
# each time. That matters because the filter mutates the record and is attached
# to every handler, so a record is filtered once per handler.
_ASSIGNMENT = re.compile(
    rf"(?i)\b({_PREFIX}{_KEYS})(\s*=\s*)(?!{re.escape(REDACTED)})([^\s,;&)\]}}'\"]+)",
)

# `password="hunter2"`, `token='abc'` — a quoted assignment, as it appears in a
# kwargs repr or a source line. The unquoted pattern below cannot reach these:
# its value class excludes quotes, so it never matches a value that starts with
# one, and the dict pattern only covers the `'key': 'value'` colon form.
_ASSIGNMENT_QUOTED = re.compile(
    rf"(?i)\b({_PREFIX}{_KEYS})(\s*=\s*)(['\"])([^'\"]*)(['\"])",
)

# `'api_key': 'sk-live-...'` — a dict repr reaching the log via %s or f-string.
_DICT_ITEM = re.compile(
    rf"(?i)(['\"]{_PREFIX}{_KEYS}['\"]\s*:\s*)(['\"])([^'\"]*)(['\"])",
)

# An unterminated dict value: the quote opens and the record ends. Without this
# a truncated repr leaks everything after the opening quote.
_DICT_ITEM_OPEN = re.compile(
    rf"(?i)(['\"]{_PREFIX}{_KEYS}['\"]\s*:\s*)(['\"])([^'\"]*)$",
)

# `Authorization: Bearer <token>` and the bare `Bearer <token>` / `Basic <b64>`.
_AUTH_SCHEME = re.compile(
    r"(?i)\b(bearer|basic)(\s+)([A-Za-z0-9\-._~+/=]+)",
)

# `mqtt://user:s3cr3t@host` — the shape this codebase builds for broker URLs.
# The username is kept: it is not the secret, and hiding it makes connection
# failures harder to read.
_URL_USERINFO = re.compile(
    r"(?i)\b([a-z][a-z0-9+.\-]{0,20}://)([^:/@\s]{1,128})(:)([^@/\s]{1,256})(@)",
)

# A complete PEM block, then anything from a BEGIN marker to the end of the
# record. The second pattern is what stops a truncated key from leaking its body
# for want of an END marker; it must run after the first.
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----.*?-----END [A-Z ]{0,40}PRIVATE KEY-----",
    re.DOTALL,
)
_PEM_TRAILING = re.compile(
    r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----.*",
    re.DOTALL,
)


# Deliberately carries no BEGIN marker: the trailing pattern runs after the
# block pattern and would otherwise match its own replacement, swallowing the
# rest of the record.
_PEM_PLACEHOLDER = f"{REDACTED} private key"


def redact(text: str) -> str:
    """Replace known secret shapes in ``text``. Order matters where noted."""
    text = _PEM_BLOCK.sub(_PEM_PLACEHOLDER, text)
    text = _PEM_TRAILING.sub(_PEM_PLACEHOLDER, text)
    text = _URL_USERINFO.sub(rf"\1\2\3{REDACTED}\5", text)
    text = _DICT_ITEM.sub(rf"\1\2{REDACTED}\4", text)
    text = _DICT_ITEM_OPEN.sub(rf"\1\2{REDACTED}", text)
    text = _AUTH_SCHEME.sub(rf"\1\2{REDACTED}", text)
    # Quoted before unquoted: the unquoted value class stops at a quote, so it
    # would leave `password="x"` untouched rather than mangle it, but running
    # the specific pattern first keeps the order obvious.
    text = _ASSIGNMENT_QUOTED.sub(rf"\1\2\3{REDACTED}\5", text)
    return _ASSIGNMENT.sub(rf"\1\2{REDACTED}", text)


def redacted_message(record: logging.LogRecord) -> str | None:
    """The record's message, args merged and secrets removed.

    Merged rather than raw ``msg``, because a secret is as likely to arrive as a
    ``%s`` argument as in the format string. Returns ``None`` when the record
    cannot be formatted at all — a malformed format string is the caller's bug,
    and neither the filter nor the buffer may raise on it.
    """
    try:
        return redact(record.getMessage())
    except Exception:
        return None


class SecretRedactingFilter(logging.Filter):
    """Rewrite a record in place, so a handler's *output* is redacted.

    Attached to the stream and file handlers, this is what keeps secrets out of
    ``wactorz.log`` and the console. Merging args here means ``args`` must be
    cleared, or the record would re-merge an already-merged string downstream.

    Mutating, and attached to several handlers, so it runs more than once per
    record — which is why :func:`redact` has to be idempotent.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        cleaned = redacted_message(record)
        if cleaned is not None and (cleaned != record.msg or record.args):
            record.msg = cleaned
            record.args = None
        self._redact_traceback(record)
        return True

    @staticmethod
    def _redact_traceback(record: logging.LogRecord) -> None:
        """Redact the exception and stack text a formatter renders separately.

        Rewriting ``msg`` is not enough: ``Formatter.format`` builds the
        traceback from ``exc_info`` on its own, so a secret inside an exception's
        *message* — an SSH auth failure, a URL error, ``KeyError: 'secret'`` —
        reached the file and the console untouched while only the in-memory
        buffer was clean.

        Pre-setting ``exc_text`` is what fixes it: the formatter uses that when
        present instead of rendering the traceback itself. It also makes this
        idempotent across handlers for free — the first pass fills the cache and
        later ones skip.
        """
        try:
            if record.exc_info and not record.exc_text:
                record.exc_text = redact(EXC_FORMATTER.formatException(record.exc_info))
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
        except Exception:
            # Never raise on the logging path; an unredacted traceback is worse
            # than none of the record at all, so drop the text rather than
            # letting it through unfiltered.
            record.exc_info = None
            record.exc_text = f"<traceback withheld: {REDACTED} could not be applied>"


def install_redaction() -> None:
    """Attach the redactor to every handler already on the root logger.

    Handler-level, not logger-level: a filter on a logger only sees records
    logged *directly* to it, never those propagated from child loggers, so a
    single filter on the root logger would miss essentially everything the
    application logs.

    Idempotent — a handler already carrying the filter is left alone.
    """
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(SecretRedactingFilter())
