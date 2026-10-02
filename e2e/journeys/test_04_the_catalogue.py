"""The catalogue lists the agents that come with Wactorz, and starts one when asked.

The one started here collects readings from the broker. It installs nothing and
calls nothing outside the install, so the run does not depend on a network.
"""

from harness import backend, browser, waiting

RECIPE = "timeseries-collector"


def test_the_catalogue_says_what_it_has(dashboard: browser.Dashboard) -> None:
    dashboard.say("list", to="catalog")

    dashboard.expect_like(
        "catalog",
        r"Catalog agents\n\d+ total - \d+ recommended.*"
        rf"\n{RECIPE} - [^\n]+.*",
    )


def test_it_starts_the_one_asked_for(dashboard: browser.Dashboard, app: backend.Backend) -> None:
    dashboard.say(f"spawn {RECIPE}", to="catalog")

    dashboard.expect("catalog", f"'{RECIPE}' spawned and running")
    dashboard.wait_for_card(RECIPE)
    waiting.becomes_and_stays(
        lambda: app.rest.state_of(RECIPE) == "running",
        what=f"{RECIPE} to be running, and to stay so",
    )
