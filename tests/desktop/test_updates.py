"""Update check against the latest GitHub release.

No network here: `urlopen` is patched. The comparison tests matter because a
wrong answer is user-visible in both directions — nagging about an update that
isn't newer, or staying silent on one that is.

An interactive check (the tray's "Check for Updates") always reports something;
a background check stays quiet unless there is actually an update.
"""

# pylint: disable=missing-function-docstring,too-few-public-methods

import json
from types import SimpleNamespace

import pytest

from wactorz.desktop import updates


class _Resp:
    """Minimal urlopen context manager returning a canned body."""

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


@pytest.fixture(name="captured", autouse=True)
def captured_fixture(monkeypatch):
    """Capture notifications and browser opens; forget any pending update."""
    monkeypatch.setattr(updates, "_pending_update", None)
    seen = SimpleNamespace(notes=[], opened=[])
    monkeypatch.setattr(updates, "notify", lambda _t, body: seen.notes.append(body))
    monkeypatch.setattr(updates.webbrowser, "open", seen.opened.append)
    yield seen
    updates._pending_update = None


def _serve(monkeypatch, tag="v9.9.9", html_url="https://example/rel"):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        lambda *a, **k: _Resp({"tag_name": tag, "html_url": html_url}),
    )


# ── version comparison ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("0.6.0", "0.5.1", True),
        ("0.5.2", "0.5.1", True),
        ("1.0.0", "0.9.9", True),
        ("0.5.1", "0.5.1", False),  # same
        ("0.5.0", "0.5.1", False),  # older
        ("0.5", "0.5.0", False),  # zero-padded: equal, not newer
        ("0.5.0", "0.5", False),
        ("0.10.0", "0.9.0", True),  # numeric, not lexicographic
        ("", "0.5.1", False),  # unparseable → never "newer"
        ("not-a-version", "0.5.1", False),
    ],
)
def test_is_newer(latest, current, expected):
    assert updates._is_newer(latest, current) is expected


def test_version_tuple_drops_non_numeric_parts():
    assert updates._version_tuple("1.2.3rc1") == (1, 2)
    assert updates._version_tuple("1.2.3") == (1, 2, 3)


# ── pending update / download ───────────────────────────────────────────────


def test_no_pending_version_before_a_check():
    assert updates.pending_version() == ""


def test_download_falls_back_to_the_releases_page(captured):
    updates.open_download()
    assert captured.opened == [updates._RELEASES_PAGE]


def test_download_opens_the_pending_release(monkeypatch, captured):
    monkeypatch.setattr(updates, "_pending_update", {"version": "9.9.9", "url": "https://rel"})
    updates.open_download()
    assert captured.opened == ["https://rel"]
    assert updates.pending_version() == "9.9.9"


# ── the check itself ────────────────────────────────────────────────────────


def test_a_newer_release_notifies_and_becomes_pending(monkeypatch, captured):
    _serve(monkeypatch)
    updates._update_check_task(interactive=False)
    assert updates.pending_version() == "9.9.9"
    assert any("Update available" in n for n in captured.notes)


def test_a_background_check_does_not_open_the_browser(monkeypatch, captured):
    _serve(monkeypatch)
    updates._update_check_task(interactive=False)
    assert captured.opened == []  # quiet: only the notification


def test_an_interactive_check_opens_the_release_page(monkeypatch, captured):
    _serve(monkeypatch)
    updates._update_check_task(interactive=True)
    assert captured.opened == ["https://example/rel"]


def test_an_up_to_date_background_check_says_nothing(monkeypatch, captured):
    _serve(monkeypatch, tag="v0.0.1")  # older than any real version
    updates._update_check_task(interactive=False)
    assert captured.notes == []
    assert updates.pending_version() == ""


def test_an_up_to_date_interactive_check_still_reports(monkeypatch, captured):
    _serve(monkeypatch, tag="v0.0.1")
    updates._update_check_task(interactive=True)
    assert any("up to date" in n for n in captured.notes)


def test_a_failed_check_is_silent_in_the_background(monkeypatch, captured):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no network")),
    )
    updates._update_check_task(interactive=False)
    assert captured.notes == []


def test_a_failed_interactive_check_tells_the_user(monkeypatch, captured):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no network")),
    )
    updates._update_check_task(interactive=True)
    assert any("Could not check" in n for n in captured.notes)


def test_a_release_without_a_page_url_falls_back(monkeypatch, captured):
    monkeypatch.setattr(
        updates.urllib.request, "urlopen", lambda *a, **k: _Resp({"tag_name": "v9.9.9"})
    )
    updates._update_check_task(interactive=False)
    assert updates._pending_update["url"] == updates._RELEASES_PAGE  # pyright: ignore[reportOptionalSubscript]


def test_a_missing_certifi_still_completes_the_check(monkeypatch, captured):
    # The frozen app prefers certifi's CA bundle; without it we fall back to the
    # default context rather than failing the check.
    import builtins

    real_import = builtins.__import__

    def _no_certifi(name, *args, **kwargs):
        if name == "certifi":
            raise ImportError("no certifi")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_certifi)
    _serve(monkeypatch)
    updates._update_check_task(interactive=False)
    assert updates.pending_version() == "9.9.9"


def test_check_for_updates_runs_off_the_gui_thread(monkeypatch):
    started = []

    class _Thread:
        def __init__(self, target, args, daemon):
            started.append((target, args, daemon))

        def start(self):
            started.append("started")

    monkeypatch.setattr(updates.threading, "Thread", _Thread)
    updates.check_for_updates(interactive=True)
    assert started[0][0] is updates._update_check_task
    assert started[0][1] == (True,)
    assert started[0][2] is True  # daemon: must not block quitting
    assert started[-1] == "started"
