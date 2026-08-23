"""The backend starts, says where it is at the right moment, and keeps quiet
about its credentials.

Three claims that share one process and are otherwise unrelated. They live
together because starting the application is the expensive part and each of them
is one assertion about the same startup.
"""

from harness import backend, logs


def test_the_backend_reaches_ready(app: backend.Backend) -> None:
    """It is up, and it is up as itself rather than as a bound port.

    `/health` answers as soon as the web server binds - which is why the fixture
    waits for the agents to report themselves running before any scenario runs.
    Asserting both here says which of the two failed when one of them does.
    """
    assert app.rest.ok("/health"), "the backend is not serving /health"
    states = {a["name"]: a["state"] for a in app.rest.agents()}
    assert states.get("main") == "running", f"main is {states.get('main')!r}, not running: {states}"


def test_the_address_is_printed_after_startup_not_before_it(app: backend.Backend) -> None:
    """The one line a person is meant to act on comes last.

    The web server binds before the supervision tree finishes coming up, so an
    address printed at bind time is buried under the whole boot. This is the
    assertion that keeps it at the bottom: the banner must come after the line
    that says the system started, not before it.
    """
    console = app.console()
    assert backend.READY_BANNER in console, (
        f"the ready banner was never printed:\n{console[-3000:]}"
    )
    started = console.find("Wactorz system started")
    banner = console.find(backend.READY_BANNER)
    assert started != -1, f"the system never reported starting:\n{console[-3000:]}"
    assert started < banner, (
        "the dashboard address was printed before the system finished starting, "
        "so it appears above the boot log rather than below it"
    )


def test_no_credentials_reach_the_log(app: backend.Backend) -> None:
    """The broker password was in this process's environment and is not in its log.

    Worth asserting only because the value really was there: the run configured
    the backend with it, connected with it, and reconnected with it. A unit test
    can assert that a redactor redacts; only a real run can assert that nothing
    wrote the secret down before the redactor saw it.
    """
    logs.assert_no_secrets(app.app_log)
