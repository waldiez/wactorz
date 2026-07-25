"""Window-geometry persistence for the desktop shell.

The state file is user-writable and survives upgrades, so it must be treated as
untrusted input: every load path here feeds it something a hand-edit or an older
version could plausibly produce. The placement tests cover the two cases a
restored geometry gets wrong — a monitor that was unplugged, and a size larger
than the screen it lands on.
"""

# pylint: disable=missing-function-docstring
from types import SimpleNamespace

import pytest

from wactorz.desktop import window_state


@pytest.fixture(name="reset_geometry", autouse=True)
def reset_geometry_fixture(monkeypatch):
    """Isolate the module state between tests.

    Also pins the session to one where a window may position itself, so the
    placement tests behave the same on an X11 CI runner and a Wayland desktop.
    Tests that care about the other case opt in via the `wayland` fixture.
    """
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    window_state.seed(dict(window_state.DEFAULTS))
    yield
    window_state.seed(dict(window_state.DEFAULTS))


@pytest.fixture(name="state_file")
def state_file_fixture(tmp_path, monkeypatch):
    """Redirect load/save at a tmp file."""
    path = tmp_path / "sub" / "window_state.json"
    monkeypatch.setattr(window_state, "WINDOW_STATE_FILE", path)
    return path


def _screen(x=0, y=0, width=1920, height=1080):
    return SimpleNamespace(x=x, y=y, width=width, height=height)


class _FakeWindow:
    """Records the geometry calls `place()` makes."""

    def __init__(self):
        self.resized_to = None
        self.moved_to = None

    def resize(self, w, h):
        self.resized_to = (w, h)

    def move(self, x, y):
        self.moved_to = (x, y)


# ── sanitize ────────────────────────────────────────────────────────────────


def test_sanitize_keeps_a_valid_state():
    assert window_state.sanitize({"width": 800, "height": 600, "x": 10, "y": 20}) == {
        "width": 800,
        "height": 600,
        "x": 10,
        "y": 20,
    }


@pytest.mark.parametrize(
    "bad",
    [
        {},  # missing keys entirely
        {"width": 800},  # partial
        {"width": "wide", "height": 600},  # non-numeric
        {"width": None, "height": 600},  # null
        {"width": 0, "height": 600},  # zero is not a usable size
        {"width": -100, "height": 600},  # negative
    ],
    ids=["empty", "partial", "non-numeric", "null", "zero", "negative"],
)
def test_sanitize_falls_back_to_default_size(bad):
    out = window_state.sanitize(bad)
    assert (out["width"], out["height"]) == (
        window_state.DEFAULTS["width"],
        window_state.DEFAULTS["height"],
    )


def test_sanitize_coerces_numeric_strings_and_floats():
    out = window_state.sanitize({"width": "800", "height": 600.7, "x": "10", "y": 20.9})
    assert (out["width"], out["height"]) == (800, 600)
    assert (out["x"], out["y"]) == (10, 20)


@pytest.mark.parametrize("bad_pos", [{"x": "left"}, {"x": [1]}, {}], ids=["str", "list", "missing"])
def test_sanitize_drops_an_unusable_position(bad_pos):
    out = window_state.sanitize({"width": 800, "height": 600, **bad_pos})
    assert out["x"] is None


def test_sanitize_never_mutates_the_defaults():
    window_state.sanitize({"width": 1, "height": 1, "x": 5, "y": 5})
    assert window_state.DEFAULTS == {"width": 1280, "height": 720, "x": None, "y": None}


# ── load / save ─────────────────────────────────────────────────────────────


def test_load_returns_defaults_when_no_file(state_file):
    assert not state_file.exists()
    assert window_state.load() == window_state.DEFAULTS


def test_load_returns_defaults_on_corrupt_json(state_file):
    state_file.parent.mkdir(parents=True)
    state_file.write_text("{not json at all")
    assert window_state.load() == window_state.DEFAULTS


def test_load_sanitizes_what_it_reads(state_file):
    state_file.parent.mkdir(parents=True)
    state_file.write_text('{"width": 900, "height": 700, "x": "nope", "y": 5}')
    assert window_state.load() == {"width": 900, "height": 700, "x": None, "y": 5}


def test_load_result_is_detached_from_defaults(state_file):
    loaded = window_state.load()
    loaded["width"] = 1
    assert window_state.DEFAULTS["width"] == 1280


def test_save_writes_tracked_geometry_and_creates_the_directory(state_file):
    window_state.seed({"width": 640, "height": 480, "x": 3, "y": 4})
    window_state.save()
    assert not state_file.parent.exists() or state_file.exists()
    assert window_state.load() == {"width": 640, "height": 480, "x": 3, "y": 4}


def test_save_never_raises_when_the_path_is_unwritable(tmp_path, monkeypatch):
    # A directory where the file should be: writing must fail, quietly.
    blocked = tmp_path / "state.json"
    blocked.mkdir()
    monkeypatch.setattr(window_state, "WINDOW_STATE_FILE", blocked)
    window_state.save()  # must not raise


def test_save_then_load_round_trips(state_file):
    window_state.track_resize(1024, 768)
    window_state.track_move(100, 200)
    window_state.save()
    assert window_state.load() == {"width": 1024, "height": 768, "x": 100, "y": 200}


# ── tracking ────────────────────────────────────────────────────────────────


def test_track_resize_and_move_update_current():
    window_state.track_resize("1024", 768.9)  # events may hand us floats/strings
    window_state.track_move(-5, "12")
    assert window_state.current() == {"width": 1024, "height": 768, "x": -5, "y": 12}


def test_current_returns_a_copy():
    window_state.current()["width"] = 1
    assert window_state.current()["width"] == window_state.DEFAULTS["width"]


def test_seed_adopts_the_loaded_state():
    window_state.seed({"width": 800, "height": 600, "x": 1, "y": 2})
    assert window_state.current() == {"width": 800, "height": 600, "x": 1, "y": 2}


# ── _within ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("px", "py", "expected"),
    [
        (0, 0, True),  # top-left corner is inclusive
        (1919, 1079, True),  # bottom-right is exclusive-1
        (1920, 500, False),  # one past the right edge
        (500, 1080, False),  # one past the bottom edge
        (-1, 500, False),
    ],
)
def test_within_screen_bounds(px, py, expected):
    assert window_state._within(_screen(), px, py) is expected


# ── place ───────────────────────────────────────────────────────────────────


def test_place_is_a_noop_without_a_window():
    window_state.place(None, [_screen()])  # must not raise


def test_place_is_a_noop_without_screens():
    win = _FakeWindow()
    window_state.place(win, [])
    window_state.place(win, None)
    assert win.resized_to is None and win.moved_to is None


def test_place_leaves_a_fitting_unpositioned_window_alone():
    # No saved x/y: create_window already centred it.
    win = _FakeWindow()
    window_state.seed({"width": 800, "height": 600, "x": None, "y": None})
    window_state.place(win, [_screen()])
    assert win.resized_to is None and win.moved_to is None


def test_place_shrinks_an_unpositioned_window_larger_than_the_screen():
    win = _FakeWindow()
    window_state.seed({"width": 3000, "height": 2000, "x": None, "y": None})
    window_state.place(win, [_screen(width=1920, height=1080)])
    assert win.resized_to == (1920, 1080)
    assert win.moved_to is None  # position is left to create_window
    assert window_state.current()["width"] == 1920


def test_place_leaves_a_window_that_still_fits_its_screen():
    win = _FakeWindow()
    window_state.seed({"width": 800, "height": 600, "x": 100, "y": 100})
    window_state.place(win, [_screen()])
    assert win.resized_to is None and win.moved_to is None


def test_place_nudges_a_window_overflowing_its_screen():
    # Sits near the right edge: keep the screen, pull it back into bounds.
    win = _FakeWindow()
    window_state.seed({"width": 800, "height": 600, "x": 1800, "y": 900})
    window_state.place(win, [_screen(width=1920, height=1080)])
    assert win.moved_to == (1120, 480)  # 1920-800, 1080-600
    assert win.resized_to is None


def test_place_recenters_when_the_saved_monitor_is_gone():
    # Saved on a second monitor at x=1920 that is no longer connected.
    win = _FakeWindow()
    window_state.seed({"width": 800, "height": 600, "x": 2500, "y": 300})
    window_state.place(win, [_screen(width=1920, height=1080)])
    assert win.moved_to == ((1920 - 800) // 2, (1080 - 600) // 2)
    assert window_state.current()["x"] == 560


def test_place_picks_the_screen_the_window_is_on():
    # Two monitors side by side; the window is on the right-hand one.
    left, right = _screen(width=1920, height=1080), _screen(x=1920, width=1280, height=1024)
    win = _FakeWindow()
    window_state.seed({"width": 800, "height": 600, "x": 2000, "y": 100})
    window_state.place(win, [left, right])
    assert win.moved_to is None  # already fits on the right screen
    assert win.resized_to is None


def test_place_shrinks_to_the_smaller_screen_it_lands_on():
    left, right = _screen(width=1920, height=1080), _screen(x=1920, width=1024, height=768)
    win = _FakeWindow()
    window_state.seed({"width": 1600, "height": 900, "x": 2000, "y": 100})
    window_state.place(win, [left, right])
    assert win.resized_to == (1024, 768)


def test_place_swallows_a_failing_window_call():
    # A window torn down mid-placement must not take the launch down with it.
    class _Angry(_FakeWindow):
        def resize(self, w, h):
            raise RuntimeError("window is gone")

    window_state.seed({"width": 3000, "height": 2000, "x": None, "y": None})
    window_state.place(_Angry(), [_screen()])  # must not raise


# ── position_supported (Wayland cannot place its own windows) ───────────────


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, True),  # plain X11
        ({"WAYLAND_DISPLAY": "wayland-0"}, False),  # native Wayland
        ({"WAYLAND_DISPLAY": "wayland-0", "QT_QPA_PLATFORM": "xcb"}, True),  # XWayland
        ({"QT_QPA_PLATFORM": "wayland"}, True),  # no WAYLAND_DISPLAY → X11 session
    ],
    ids=["x11", "wayland", "xwayland", "qt-wayland-without-display"],
)
def test_position_supported_per_session(env, expected, monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert window_state.position_supported() is expected


@pytest.fixture(name="wayland")
def wayland_fixture(monkeypatch):
    """A session where the compositor owns window placement."""
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)


def test_track_move_is_ignored_where_position_is_not_ours(wayland):
    # The toolkit reports a meaningless 0,0 on Wayland; recording it would
    # destroy the position saved from an X11 session.
    window_state.seed({"width": 800, "height": 600, "x": 400, "y": 300})
    window_state.track_move(0, 0)
    assert (window_state.current()["x"], window_state.current()["y"]) == (400, 300)


def test_track_resize_still_works_on_wayland(wayland):
    # Size is settable there — only position is the compositor's call.
    window_state.track_resize(1024, 768)
    assert window_state.current()["width"] == 1024


def test_track_move_records_on_x11():
    window_state.track_move(42, 24)
    assert (window_state.current()["x"], window_state.current()["y"]) == (42, 24)


def test_place_only_fixes_size_on_wayland(wayland):
    # A saved position must not be applied (move is a no-op) nor overwritten.
    win = _FakeWindow()
    window_state.seed({"width": 3000, "height": 2000, "x": 400, "y": 300})
    window_state.place(win, [_screen(width=1920, height=1080)])
    assert win.resized_to == (1920, 1080)  # oversize is still corrected
    assert win.moved_to is None  # placement left to the compositor
    assert window_state.current()["x"] == 400  # saved position preserved


def test_place_on_wayland_without_screens_does_nothing(wayland):
    win = _FakeWindow()
    window_state.place(win, [])
    assert win.resized_to is None and win.moved_to is None
