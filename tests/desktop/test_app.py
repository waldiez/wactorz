"""The desktop shell itself: the JS bridge, the window state machine and launch.

`webview` is replaced by a fake window here — creating a real one needs a display
and a GUI loop — and `os._exit` is neutralised so `_shutdown` can be asserted on.

The rules worth pinning: the Api methods run on pywebview's JS-API thread, so
none of them may navigate synchronously or block; `retry()` must not persist
anything while `save_config()` must persist *before* restarting; and the
hide/minimize state is derived from real window events, because a native
minimize raises no hide event and a naive flag makes the next tray click a
no-op.
"""

# pylint: disable=missing-function-docstring,too-few-public-methods

import os
import sys
from types import SimpleNamespace

import pytest

from wactorz.desktop import app

# ── fakes ───────────────────────────────────────────────────────────────────


class _Event:
    """pywebview's `window.events.<name> += handler`."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _FakeWindow:
    """Records navigation and visibility calls."""

    def __init__(self):
        self.nav = []
        self.calls = []
        self.events = SimpleNamespace(
            closing=_Event(),
            resized=_Event(),
            moved=_Event(),
            minimized=_Event(),
            restored=_Event(),
            loaded=_Event(),
        )

    def load_html(self, html):
        self.nav.append(("html", html))

    def load_url(self, url):
        self.nav.append(("url", url))

    def show(self):
        self.calls.append("show")

    def hide(self):
        self.calls.append("hide")

    def restore(self):
        self.calls.append("restore")


class _FakeThread:
    """Runs the target immediately on start(), recording the call."""

    def __init__(self, record):
        self.record = record

    def __call__(self, target, args=(), daemon=False, **_kw):
        self.record.append(SimpleNamespace(target=target, args=args, daemon=daemon))
        return SimpleNamespace(start=lambda: target(*args))


@pytest.fixture(name="shell", autouse=True)
def shell_fixture(monkeypatch):
    """A fake window installed as the shell's, with side effects neutralised."""
    window = _FakeWindow()
    log = []
    threads = []

    monkeypatch.setattr(app, "_window", window)
    monkeypatch.setattr(app, "_hidden", False)
    monkeypatch.setattr(app, "_minimized", False)
    monkeypatch.setattr(app, "_shown", False)
    monkeypatch.setattr(app, "_tray", None)
    monkeypatch.setattr(app, "_tray_ok", False)

    monkeypatch.setattr(app.time, "sleep", lambda _s: None)
    monkeypatch.setattr(os, "_exit", lambda code: log.append(("exit", code)))
    monkeypatch.setattr(app.window_state, "save", lambda: log.append("state-saved"))
    monkeypatch.setattr(app.backend, "stop", lambda: log.append("backend-stopped"))
    monkeypatch.setattr(app.threading, "Thread", _FakeThread(threads))

    yield SimpleNamespace(window=window, log=log, threads=threads)

    app._window = None
    app._hidden = app._minimized = app._shown = app._tray_ok = False
    app._tray = None


# ── _defer_nav ──────────────────────────────────────────────────────────────


def test_navigation_is_deferred_to_a_daemon_thread(shell):
    # Navigating inside an Api method makes pywebview deliver the return value
    # on the new page, where the callback is gone.
    ran = []
    app._defer_nav(lambda: ran.append("navigated"))
    assert ran == ["navigated"]
    assert shell.threads[0].daemon is True


def test_a_failing_navigation_is_swallowed():
    app._defer_nav(lambda: (_ for _ in ()).throw(RuntimeError("window gone")))


# ── Api ─────────────────────────────────────────────────────────────────────


@pytest.fixture(name="api")
def api_fixture(monkeypatch):
    """An Api with navigation running inline so it can be asserted on."""
    monkeypatch.setattr(app, "_defer_nav", lambda action: action())
    return app.Api()


def test_the_page_can_raise_a_notification(api, monkeypatch):
    seen = []
    monkeypatch.setattr(app.notifications, "notify", lambda t, b: seen.append((t, b)))
    api.notify("Title", "Body")
    assert seen == [("Title", "Body")]


def test_open_config_loads_the_configure_form(api, shell, monkeypatch):
    monkeypatch.setattr(app.backend, "is_running", lambda: True)
    monkeypatch.setattr(app.backend_config, "load", dict)
    api.open_config()
    kind, html = shell.window.nav[-1]
    assert kind == "html" and "save_config" in html


def test_cancel_returns_to_the_running_app(api, shell):
    api.close_config()
    assert shell.window.nav == [("url", app.URL)]


def test_cancel_is_safe_without_a_window(api, monkeypatch):
    monkeypatch.setattr(app, "_window", None)
    api.close_config()  # must not raise


def test_retry_does_not_persist_anything(api, monkeypatch):
    # Retry means "try again with what's already saved" — writing here would
    # silently commit whatever the form happened to be showing.
    saved = []
    monkeypatch.setattr(app.backend_config, "save", saved.append)
    monkeypatch.setattr(app, "_restart_backend", lambda: None)
    assert api.retry() is True
    assert not saved


def test_retry_restarts_off_the_js_api_thread(api, shell, monkeypatch):
    monkeypatch.setattr(app, "_restart_backend", lambda: None)
    api.retry()
    assert shell.threads[0].target is app._restart_backend
    assert shell.threads[0].daemon is True


def test_save_config_persists_before_restarting(api, shell, monkeypatch):
    order = []
    monkeypatch.setattr(app.backend_config, "save", lambda v: order.append(("save", v)))
    monkeypatch.setattr(app, "_restart_backend", lambda: order.append("restart"))
    assert api.save_config({"MQTT_HOST": "broker"}) is True
    assert order == [("save", {"MQTT_HOST": "broker"}), "restart"]


def test_save_config_restarts_off_the_js_api_thread(api, shell, monkeypatch):
    monkeypatch.setattr(app.backend_config, "save", lambda _v: None)
    monkeypatch.setattr(app, "_restart_backend", lambda: None)
    api.save_config({})
    assert shell.threads[0].target is app._restart_backend
    assert shell.threads[0].daemon is True


def test_save_config_tolerates_a_missing_payload(api, monkeypatch):
    saved = []
    monkeypatch.setattr(app.backend_config, "save", saved.append)
    monkeypatch.setattr(app, "_restart_backend", lambda: None)
    api.save_config(None)  # type: ignore[arg-type]
    assert saved == [{}]


# ── _show_config ────────────────────────────────────────────────────────────


@pytest.fixture(name="config_page", autouse=True)
def config_page_fixture(monkeypatch):
    monkeypatch.setattr(app.backend_config, "load", dict)


def test_cancel_is_offered_only_while_the_backend_is_up(shell, monkeypatch):
    monkeypatch.setattr(app.backend, "is_running", lambda: True)
    app._show_config()
    assert "close_config()" in shell.window.nav[-1][1]

    monkeypatch.setattr(app.backend, "is_running", lambda: False)
    app._show_config()
    assert "close_config()" not in shell.window.nav[-1][1]


def test_the_config_message_is_shown(shell, monkeypatch):
    monkeypatch.setattr(app.backend, "is_running", lambda: False)
    app._show_config("MQTT broker unreachable")
    assert "MQTT broker unreachable" in shell.window.nav[-1][1]


def test_opening_config_brings_the_window_forward(shell, monkeypatch):
    # It can be opened from the tray while the window is hidden.
    monkeypatch.setattr(app.backend, "is_running", lambda: False)
    monkeypatch.setattr(app, "_hidden", True)
    app._show_config()
    assert "show" in shell.window.calls
    assert app._hidden is False


def test_showing_config_is_safe_without_a_window(monkeypatch):
    monkeypatch.setattr(app, "_window", None)
    app._show_config()  # must not raise


# ── _restart_backend ────────────────────────────────────────────────────────


@pytest.fixture(name="restart")
def restart_fixture(monkeypatch):
    """Scriptable backend for the restart sequence."""
    st = SimpleNamespace(order=[], mqtt=True, serving=True)
    monkeypatch.setattr(app.backend, "stop", lambda: st.order.append("stop"))
    monkeypatch.setattr(app.backend, "start", lambda: st.order.append("start"))
    monkeypatch.setattr(app.backend, "mqtt_reachable", lambda: st.mqtt)
    monkeypatch.setattr(app.backend, "wait_until_serving", lambda: st.serving)
    monkeypatch.setattr(app.backend, "is_running", lambda: False)
    return st


def test_a_restart_stops_the_old_child_first(restart, shell):
    # The new child would otherwise fail to bind the port the window points at.
    app._restart_backend()
    assert restart.order == ["stop", "start"]


def test_a_restart_shows_the_splash_then_the_app(restart, shell):
    app._restart_backend()
    assert shell.window.nav == [("html", app.pages.LOADING_HTML), ("url", app.URL)]


def test_a_restart_without_mqtt_goes_back_to_configure(restart, shell):
    restart.mqtt = False
    app._restart_backend()
    assert "start" not in restart.order  # pointless: the backend would exit
    assert "MQTT broker still unreachable" in shell.window.nav[-1][1]


def test_a_backend_that_never_serves_shows_the_error_page_on_restart(restart, shell):
    restart.serving = False
    app._restart_backend()
    assert str(app.BACKEND_LOG) in shell.window.nav[-1][1]


def test_a_restart_is_safe_without_a_window(restart, monkeypatch):
    monkeypatch.setattr(app, "_window", None)
    app._restart_backend()
    assert restart.order == ["stop", "start"]


def test_a_restart_without_a_window_and_without_a_backend_is_safe(restart, monkeypatch):
    monkeypatch.setattr(app, "_window", None)
    restart.serving = False
    app._restart_backend()  # must not raise


# ── show / hide / minimize ──────────────────────────────────────────────────


def test_the_tray_toggle_hides_a_visible_window(shell):
    app._toggle()
    assert shell.window.calls == ["hide"]
    assert app._hidden is True


def test_the_tray_toggle_reveals_a_hidden_window(shell, monkeypatch):
    monkeypatch.setattr(app, "_hidden", True)
    app._toggle()
    assert shell.window.calls == ["show"]
    assert app._hidden is False


def test_the_tray_toggle_restores_a_minimized_window(shell, monkeypatch):
    # A native minimize raises no hide event; a naive flag would make this a
    # no-op and the user would have to click twice.
    monkeypatch.setattr(app, "_minimized", True)
    app._toggle()
    assert shell.window.calls == ["restore", "show"]
    assert app._minimized is False


def test_the_toggle_is_safe_without_a_window(monkeypatch):
    monkeypatch.setattr(app, "_window", None)
    app._toggle()  # must not raise


def test_window_events_track_the_minimized_state():
    app._on_minimized()
    assert app._minimized is True
    app._on_restored()
    assert app._minimized is False


def test_revealing_is_safe_without_a_window(monkeypatch):
    monkeypatch.setattr(app, "_window", None)
    app._reveal_window()  # must not raise


# ── first paint ─────────────────────────────────────────────────────────────


def test_the_window_is_revealed_once_the_page_paints(shell):
    app._on_app_loaded()
    assert shell.window.calls == ["show"]


def test_the_window_is_revealed_only_once(shell):
    # Both the 'loaded' event and the timeout fallback call this.
    app._on_app_loaded()
    app._on_app_loaded()
    assert shell.window.calls == ["show"]


def test_nothing_is_revealed_without_a_window(monkeypatch):
    monkeypatch.setattr(app, "_window", None)
    app._on_app_loaded()
    assert app._shown is False


# ── closing / shutdown ──────────────────────────────────────────────────────


def test_closing_hides_to_the_tray_when_there_is_one(shell, monkeypatch):
    monkeypatch.setattr(app, "_tray_ok", True)
    assert app._on_closing() is False  # the real close is cancelled
    assert shell.window.calls == ["hide"]
    assert app._hidden is True


def test_closing_quits_without_a_tray(shell):
    # Hiding would leave the window unreachable.
    assert app._on_closing() is True
    assert ("exit", 0) in shell.log


def test_shutdown_saves_the_geometry_before_exiting(shell):
    app._shutdown()
    assert shell.log == ["state-saved", "backend-stopped", ("exit", 0)]


# ── _load_when_ready ────────────────────────────────────────────────────────


@pytest.fixture(name="startup")
def startup_fixture(monkeypatch):
    """Scriptable startup sequence with every side effect captured."""
    st = SimpleNamespace(order=[], mqtt=True, serving=True, auto_update=False, screens=["s0"])
    monkeypatch.setattr(app, "webview", SimpleNamespace(screens=st.screens))
    monkeypatch.setattr(app.window_state, "place", lambda w, s: st.order.append(("place", w, s)))
    monkeypatch.setattr(
        app.platform_hooks,
        "install_quit_handler",
        lambda on_quit, on_reopen: st.order.append(("quit-handler", on_quit, on_reopen)),
    )
    monkeypatch.setattr(app.notifications, "request_authorization", lambda: st.order.append("auth"))
    monkeypatch.setattr(app.notifications, "notify", lambda t, b: st.order.append(("notify", b)))
    monkeypatch.setattr(app.backend, "wait_for_mqtt", lambda: st.mqtt)
    monkeypatch.setattr(app.backend, "start", lambda: st.order.append("start"))
    monkeypatch.setattr(app.backend, "wait_until_serving", lambda: st.serving)
    monkeypatch.setattr(app.backend, "is_running", lambda: False)
    monkeypatch.setattr(app.settings, "auto_update_check", lambda: st.auto_update)
    monkeypatch.setattr(
        app.updates,
        "check_for_updates",
        lambda interactive: st.order.append(("update-check", interactive)),
    )
    return st


def test_startup_places_the_window_and_installs_the_quit_handler(startup, shell):
    app._load_when_ready(shell.window)
    assert ("place", shell.window, startup.screens) in startup.order
    assert ("quit-handler", app._shutdown, app._reveal_window) in startup.order


def test_startup_asks_for_notification_permission_before_notifying(startup, shell):
    startup.mqtt = False
    app._load_when_ready(shell.window)
    assert startup.order.index("auth") < next(
        i for i, o in enumerate(startup.order) if isinstance(o, tuple) and o[0] == "notify"
    )


def test_the_backend_is_spawned_only_once_mqtt_answers(startup, shell):
    # Spawning earlier races a still-booting broker and the child exits.
    app._load_when_ready(shell.window)
    assert "start" in startup.order
    assert shell.window.nav[-1] == ("url", app.URL)


def test_an_unreachable_broker_opens_configure_instead_of_failing(startup, shell):
    startup.mqtt = False
    app._load_when_ready(shell.window)
    assert "start" not in startup.order
    assert "MQTT broker unreachable" in shell.window.nav[-1][1]
    assert app._shown is True  # the user can still see and fix it


def test_a_backend_that_never_serves_shows_the_error_page(startup, shell):
    startup.serving = False
    app._load_when_ready(shell.window)
    assert str(app.BACKEND_LOG) in shell.window.nav[-1][1]
    assert ("notify", "Backend did not start in time") in startup.order
    assert app._shown is True


def test_the_window_is_revealed_even_if_the_loaded_event_never_fires(startup, shell):
    app._load_when_ready(shell.window)
    assert app._on_app_loaded in shell.window.events.loaded.handlers
    assert app._shown is True  # the timeout fallback fired


def test_no_update_check_when_the_preference_is_off(startup, shell):
    app._load_when_ready(shell.window)
    assert not any(isinstance(o, tuple) and o[0] == "update-check" for o in startup.order)


def test_the_launch_update_check_is_silent(startup, shell):
    startup.auto_update = True
    app._load_when_ready(shell.window)
    assert ("update-check", False) in startup.order


# ── GUI backend detection ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("probe", "module"),
    [(app._qt_available, "PySide6"), (app._gtk_available, "gi")],
    ids=["qt", "gtk"],
)
def test_a_gui_backend_is_detected_without_importing_it(probe, module, monkeypatch):
    # find_spec, not import: probing must not pull in a heavy toolkit, and must
    # not crash when the toolkit is present but unusable (no display).
    seen = []
    monkeypatch.setattr(app.importlib.util, "find_spec", lambda name: seen.append(name) or object())
    assert probe() is True
    assert seen == [module]


@pytest.mark.parametrize("probe", [app._qt_available, app._gtk_available], ids=["qt", "gtk"])
def test_a_missing_gui_backend_is_reported(probe, monkeypatch):
    monkeypatch.setattr(app.importlib.util, "find_spec", lambda _name: None)
    assert probe() is False


# ── launch_desktop ──────────────────────────────────────────────────────────


@pytest.fixture(name="launch")
def launch_fixture(monkeypatch):
    """Everything launch_desktop touches, faked and recorded."""
    st = SimpleNamespace(
        created=[],
        started=[],
        window=_FakeWindow(),
        qt=True,
        gtk=True,
        positioning=True,
        installed=[],
        tray_obj=object(),
        state={"width": 1280, "height": 800, "x": 100, "y": 50},
    )

    def _create_window(*args, **kwargs):
        st.created.append((args, kwargs))
        return st.window

    monkeypatch.setattr(
        app,
        "webview",
        SimpleNamespace(
            create_window=_create_window,
            start=lambda *a, **k: st.started.append((a, k)),
            screens=[],
        ),
    )
    monkeypatch.setattr(app, "_qt_available", lambda: st.qt)
    monkeypatch.setattr(app, "_gtk_available", lambda: st.gtk)
    monkeypatch.setattr(app.desktop_entry, "install", lambda: st.installed.append("desktop-entry"))
    monkeypatch.setattr(app.platform_hooks, "set_app_user_model_id", lambda: None)
    monkeypatch.setattr(app.signal, "signal", lambda *_a: None)
    monkeypatch.setattr(app.window_state, "load", lambda: dict(st.state))
    monkeypatch.setattr(app.window_state, "position_supported", lambda: st.positioning)
    monkeypatch.setattr(app.window_state, "seed", lambda _s: None)
    monkeypatch.setattr(app.tray, "build_qt_tray", lambda h: ("qt-tray", h))
    monkeypatch.setattr(app.tray, "build_pystray_tray", lambda h: ("pystray", h))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    return st


def test_launch_opens_a_window_and_runs_the_gui_loop(launch):
    app.launch_desktop()
    assert len(launch.created) == 1
    assert len(launch.started) == 1


def test_launch_restores_the_saved_geometry(launch):
    app.launch_desktop()
    _args, kwargs = launch.created[0]
    assert (kwargs["width"], kwargs["height"]) == (1280, 800)
    assert (kwargs["x"], kwargs["y"]) == (100, 50)


def test_no_position_is_requested_where_the_compositor_owns_it(launch):
    # Wayland ignores a client-chosen position; asking centres the window.
    launch.positioning = False
    app.launch_desktop()
    _args, kwargs = launch.created[0]
    assert kwargs["x"] is None and kwargs["y"] is None
    assert kwargs["width"] == 1280  # size is still restored


def test_the_js_bridge_is_attached_to_the_window(launch):
    # The splash/error/config pages call window.pywebview.api.*.
    app.launch_desktop()
    _args, kwargs = launch.created[0]
    assert isinstance(kwargs["js_api"], app.Api)


def test_launch_wires_the_window_events(launch):
    app.launch_desktop()
    events = launch.window.events
    assert events.closing.handlers == [app._on_closing]
    assert events.resized.handlers == [app.window_state.track_resize]
    assert events.moved.handlers == [app.window_state.track_move]
    assert events.minimized.handlers == [app._on_minimized]
    assert events.restored.handlers == [app._on_restored]


def test_the_menu_entry_is_installed_before_the_window_on_linux(launch):
    # Qt registers with the portal at window creation; the entry must exist.
    app.launch_desktop()
    assert launch.installed == ["desktop-entry"]


def test_no_menu_entry_is_installed_off_linux(launch, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    app.launch_desktop()
    assert launch.installed == []


def test_qt_is_used_when_pyside_is_present(launch):
    app.launch_desktop()
    _args, kwargs = launch.started[0]
    assert kwargs["gui"] == "qt"
    assert app._tray[0] == "qt-tray"  # pyright: ignore[reportOptionalSubscript, reportIndexIssue]


def test_the_gtk_backend_is_used_without_pyside(launch):
    launch.qt = False
    app.launch_desktop()
    _args, kwargs = launch.started[0]
    assert "gui" not in kwargs  # pywebview picks GTK itself
    assert app._tray[0] == "pystray"  # pyright: ignore[reportOptionalSubscript, reportIndexIssue]


def test_launch_fails_clearly_with_no_linux_gui_backend(launch, capsys):
    launch.qt = False
    launch.gtk = False
    with pytest.raises(SystemExit) as exc:
        app.launch_desktop()
    assert exc.value.code == 1
    assert "no GUI backend found" in capsys.readouterr().err


def test_gnome_is_forced_onto_xwayland(launch, monkeypatch):
    # GNOME renders the bundled QtWebEngine as a blank unthemed window.
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    app.launch_desktop()
    assert os.environ["QT_QPA_PLATFORM"] == "xcb"


def test_an_explicit_platform_choice_wins_over_the_gnome_workaround(launch, monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    monkeypatch.setenv("QT_QPA_PLATFORM", "wayland")
    app.launch_desktop()
    assert os.environ["QT_QPA_PLATFORM"] == "wayland"


def test_kde_is_left_on_wayland(launch, monkeypatch):
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    app.launch_desktop()
    assert "QT_QPA_PLATFORM" not in os.environ


def test_the_tray_hooks_reach_the_shell(launch):
    app.launch_desktop()
    hooks = app._tray[1]  # pyright: ignore[reportOptionalSubscript, reportIndexIssue]
    assert hooks.on_toggle is app._toggle
    assert hooks.on_configure is app._show_config
    assert hooks.on_quit is app._shutdown
    assert hooks.autostart_enabled is app.autostart.is_enabled
    assert hooks.pending_update_version is app.updates.pending_version


def test_the_tray_autostart_toggle_writes_through(launch, monkeypatch):
    written = []
    monkeypatch.setattr(app.autostart, "set_enabled", written.append)
    app.launch_desktop()
    app._tray[1].set_autostart(True)  # pyright: ignore[reportOptionalSubscript, reportIndexIssue]
    assert written == [True]


def test_no_tray_means_closing_quits(launch, monkeypatch):
    monkeypatch.setattr(app.tray, "build_qt_tray", lambda _h: None)
    app.launch_desktop()
    assert app._tray_ok is False


def test_launch_stops_if_the_window_cannot_be_created(launch, monkeypatch):
    monkeypatch.setattr(app.webview, "create_window", lambda *a, **k: None)
    app.launch_desktop()
    assert launch.started == []


# ── entry point ─────────────────────────────────────────────────────────────


def test_the_entry_point_opens_the_window(monkeypatch):
    seen = []
    monkeypatch.setattr(app, "launch_desktop", lambda: seen.append("launched"))
    monkeypatch.setattr(sys, "argv", ["wactorz-desktop"])
    app.main()
    assert seen == ["launched"]


def test_the_backend_child_re_enters_through_the_same_entry_point(monkeypatch):
    # The frozen app has no python -m; it re-invokes itself with --run-backend.
    seen = []
    monkeypatch.setattr(app.backend, "run_in_process", lambda: seen.append("backend"))
    monkeypatch.setattr(app, "launch_desktop", lambda: seen.append("launched"))
    monkeypatch.setattr(sys, "argv", ["wactorz-desktop", "--run-backend"])
    app.main()
    assert seen == ["backend"]
