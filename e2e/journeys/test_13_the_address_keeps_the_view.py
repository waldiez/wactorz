"""The address says which view is open: a refresh returns to it, Back steps through the views
visited, a link opens what it names, and an address with no view opens the last one.
"""

from harness import browser, waiting


def test_a_refresh_returns_to_the_view(dashboard: browser.Dashboard) -> None:
    dashboard.show("feed")

    assert dashboard.address == "#/feed"
    dashboard.reload()

    assert dashboard.view == "feed"


def test_back_returns_to_the_view_before(dashboard: browser.Dashboard) -> None:
    # In one test: between tests the guard reads every thread, and each thread
    # it opens is a view visited, which Back would step through first.
    dashboard.show("feed")
    dashboard.read_thread_of("main")
    waiting.until(
        lambda: dashboard.address == "#/chat/main", what="the address to name main's thread"
    )

    dashboard.back()

    waiting.until(lambda: dashboard.view == "feed", what="Back to return to the feed")
    assert dashboard.address == "#/feed"


def test_a_link_opens_the_conversation_it_names(dashboard: browser.Dashboard) -> None:
    dashboard.open_address("#/chat/main")

    waiting.until(lambda: dashboard.view == "chat", what="the link to open the chat")
    assert dashboard.talking_to == "main"


def test_an_address_with_no_view_opens_the_last_one(dashboard: browser.Dashboard) -> None:
    dashboard.show("settings")

    dashboard.open_address("")
    dashboard.reload()

    assert dashboard.view == "settings"
    # The journeys after this one start from the overview.
    dashboard.show("overview")
