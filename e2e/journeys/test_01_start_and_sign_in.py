"""An install that has just started: it wants the key, and then shows what it runs."""

from harness import backend, browser, logs
from harness.probe import Rest
from harness.run import Run

#: What a server starts by itself, before anyone has asked for anything.
STARTED_BY_THE_SERVER = {"main", "monitor", "installer", "catalog"}


def test_a_browser_that_has_not_signed_in_is_asked_for_the_key(
    visitor: browser.Dashboard,
) -> None:
    visitor.page.goto(visitor.base_url, wait_until="domcontentloaded")

    assert visitor.page.url.endswith("/login")
    assert visitor.at_sign_in


def test_a_wrong_key_does_not_get_in(visitor: browser.Dashboard) -> None:
    visitor.page.goto(visitor.base_url, wait_until="domcontentloaded")
    visitor.page.fill(browser.LOGIN_KEY, "not-the-key")
    visitor.page.click(browser.LOGIN_SUBMIT)
    visitor.page.wait_for_load_state("domcontentloaded")

    assert visitor.at_sign_in
    assert "not" in visitor.page.inner_text("body").lower()


def test_the_api_answers_nobody_without_the_key(app: backend.Backend) -> None:
    stranger = Rest(app.url)

    assert stranger.raw("GET", "/api/actors").status == 401
    assert app.rest.raw("GET", "/api/actors").status == 200


def test_signed_in_the_dashboard_shows_what_the_server_runs(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.show("overview")

    assert app.started_with >= STARTED_BY_THE_SERVER
    assert dashboard.card_names() == app.started_with


def test_home_assistants_agents_start_only_when_it_is_configured(app: backend.Backend) -> None:
    # This run configures no Home Assistant (the backend's environment empties
    # HA_URL and HA_TOKEN), so none of its agents has anything to talk to.
    assert not {name for name in app.started_with if name.startswith("home-assistant")}


def test_every_view_draws(dashboard: browser.Dashboard) -> None:
    for view in browser.VIEWS:
        dashboard.show(view)


def test_starting_wrote_none_of_its_secrets_down(app: backend.Backend, run: Run) -> None:
    for log in (app.console_log, app.app_log):
        logs.assert_no_secrets(log, run)
