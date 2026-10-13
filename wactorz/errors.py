"""Exceptions a program embedding Wactorz can catch.

Stdlib-only, so it can be imported from anywhere in the package without
pulling the application in.
"""


class StartupError(RuntimeError):
    """The system cannot start as configured.

    Raised by :func:`wactorz.serve` and :func:`wactorz.app.app` for what the
    ``wactorz`` command reports and exits on: an exposed bind address without
    an API key, a broker certificate that cannot be loaded, a chat interface
    whose token is missing, an orchestrator that did not come up. The message
    says what to change. The command turns it into exit status 1; a host
    program catches it like any other exception.
    """
