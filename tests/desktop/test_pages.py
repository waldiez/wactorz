"""Splash / error / config pages served by the desktop shell.

These are built by string interpolation, and the config form is pre-filled with
values the user typed — so the escaping assertions here are the guard against a
stored value (or an error message carrying a path) breaking out of its attribute
and injecting markup into the shell's own privileged page.
"""

# pylint: disable=missing-function-docstring

import pytest

from wactorz.desktop import pages

# ── splash ──────────────────────────────────────────────────────────────────


def test_loading_page_is_a_complete_document():
    assert pages.LOADING_HTML.lstrip().startswith("<!doctype html")


# ── error page ──────────────────────────────────────────────────────────────


def test_error_page_shows_the_log_path():
    html = pages.error_html("/var/log/wactorz/backend.log")
    assert "/var/log/wactorz/backend.log" in html


def test_error_page_offers_the_way_out():
    # Configure is the user's only route out of a failed start.
    assert "window.pywebview.api.open_config()" in pages.error_html("/tmp/x.log")


# ── config form ─────────────────────────────────────────────────────────────


def test_config_form_prefills_current_values():
    html = pages.config_html({"MQTT_HOST": "broker.local"})
    assert 'value="broker.local"' in html


def test_config_form_posts_back_through_the_js_bridge():
    html = pages.config_html({})
    assert "window.pywebview.api.save_config" in html


def test_a_message_renders_as_a_banner():
    html = pages.config_html({}, message="MQTT broker unreachable")
    assert "MQTT broker unreachable" in html
    assert 'class="banner"' in html


def test_no_banner_without_a_message():
    assert 'class="banner"' not in pages.config_html({})


def test_cancel_is_offered_only_when_there_is_an_app_to_return_to():
    # No running backend → no way back, so no Cancel.
    assert "close_config()" not in pages.config_html({}, can_cancel=False)
    assert "close_config()" in pages.config_html({}, can_cancel=True)


# ── escaping ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hostile",
    [
        '" autofocus onfocus="alert(1)',  # break out of the value attribute
        "<script>alert(1)</script>",
        "a&b",
    ],
    ids=["attribute-break", "script-tag", "ampersand"],
)
def test_stored_values_cannot_inject_markup(hostile):
    html = pages.config_html({"MQTT_HOST": hostile})
    assert hostile not in html  # present only in escaped form
    assert "<script>alert(1)</script>" not in html


def test_the_banner_message_is_escaped():
    html = pages.config_html({}, message="<img src=x onerror=alert(1)>")
    assert "<img src=x" not in html
    assert "&lt;img" in html


def test_a_missing_value_renders_empty_not_none():
    # "None" is a legitimate provider label, so check the attribute instead:
    # a Python None must never leak into a field value.
    assert 'value="None"' not in pages.config_html({})


def test_retry_and_cancel_are_mutually_exclusive():
    # With a live app you go Back; without one there is nothing to go back to,
    # so the button re-tries the connection instead.
    with_app = pages.config_html({}, can_cancel=True)
    assert "api.close_config()" in with_app and "api.retry()" not in with_app

    without_app = pages.config_html({}, can_cancel=False)
    assert "api.retry()" in without_app and "api.close_config()" not in without_app
