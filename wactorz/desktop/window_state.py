"""Window geometry: remember it across runs, and keep it on a usable screen.

The live geometry is tracked from the window's resized/moved events rather than
read back from the window at shutdown — by then it may be hidden to the tray or
already torn down, which yields garbage.

Everything here is best-effort: a missing, corrupt or hand-edited state file
falls back to the defaults, and saving never raises. Losing window position is
never worth failing a launch or a quit over.
"""

from __future__ import annotations

import json
import os
from typing import Any

from wactorz.desktop.config import WINDOW_STATE_FILE

DEFAULTS: dict[str, Any] = {"width": 1280, "height": 720, "x": None, "y": None}

# Live geometry, seeded from the saved state on launch and kept fresh by the
# window events. `save()` writes this, not the window.
_geometry: dict[str, Any] = dict(DEFAULTS)


def position_supported() -> bool:
    """Whether this session lets a window choose its own position.

    Wayland deliberately does not: `move()` is ignored and the compositor places
    the window, so the toolkit reports a meaningless 0,0. Tracking that would
    overwrite a good saved position with garbage, so callers skip position
    handling entirely here. XWayland (`QT_QPA_PLATFORM=xcb`) does honour moves,
    so it counts as X11 even though WAYLAND_DISPLAY is still set.
    """
    if os.environ.get("QT_QPA_PLATFORM") == "xcb":
        return True
    return not os.environ.get("WAYLAND_DISPLAY")


def sanitize(state: dict) -> dict[str, Any]:
    """Coerce a loaded state dict to valid values.

    The file may be hand-edited, partial, or written by an older version, so
    every field is validated: width/height become positive ints, x/y an int or
    None. Anything unusable falls back to that field's default.
    """
    out = dict(DEFAULTS)
    try:
        w, h = int(state["width"]), int(state["height"])
        if w >= 1 and h >= 1:
            out["width"], out["height"] = w, h
    except (KeyError, TypeError, ValueError):
        pass
    for key in ("x", "y"):
        try:
            out[key] = None if state.get(key) is None else int(state[key])
        except (TypeError, ValueError):
            out[key] = None
    return out


def load() -> dict[str, Any]:
    """Last saved geometry, sanitized — or the defaults if unreadable."""
    try:
        if WINDOW_STATE_FILE.exists():
            return sanitize(json.loads(WINDOW_STATE_FILE.read_text()))
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    return dict(DEFAULTS)


def save() -> None:
    """Persist the tracked geometry. Best-effort — never raises."""
    try:
        WINDOW_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        WINDOW_STATE_FILE.write_text(json.dumps(_geometry))
    except Exception:  # pylint: disable=broad-exception-caught
        pass


def seed(state: dict) -> None:
    """Adopt `state` as the current geometry, so an untouched session re-saves
    exactly what it loaded.
    """
    _geometry.update(state)


def current() -> dict[str, Any]:
    """A copy of the tracked geometry."""
    return dict(_geometry)


def track_resize(width, height) -> None:
    """Record a resize reported by the window."""
    _geometry["width"], _geometry["height"] = int(width), int(height)


def track_move(x, y) -> None:
    """Record a move reported by the window.

    Ignored where the platform does not let a window position itself: the
    reported coordinates are meaningless there, and storing them would clobber
    a position saved from a session that *could* restore it.
    """
    if not position_supported():
        return
    _geometry["x"], _geometry["y"] = int(x), int(y)


def _within(screen, px: int, py: int) -> bool:
    """True if the point lies on `screen`."""
    return screen.x <= px < screen.x + screen.width and screen.y <= py < screen.y + screen.height


def _fit_to_primary(window, screens) -> None:
    """Shrink the window to the primary screen, leaving its position alone."""
    if not screens:
        return
    primary = screens[0]
    w, h = _geometry["width"], _geometry["height"]
    nw, nh = min(w, primary.width), min(h, primary.height)
    if (nw, nh) != (w, h):
        window.resize(nw, nh)
        _geometry.update(width=nw, height=nh)


def place(window, screens) -> None:
    """Move/resize the window so it fits on a screen that still exists.

    Called once the GUI is up (only then are the screens known). Fixes the two
    cases a restored geometry gets wrong: a position on a monitor that has since
    been unplugged, and a size larger than the current display.
    """
    if window is None:
        return
    try:
        screens = list(screens or [])
        if not position_supported():
            # The compositor owns placement; only the size is ours to fix.
            _fit_to_primary(window, screens)
            return
        if not screens:
            return
        w, h = _geometry["width"], _geometry["height"]
        x, y = _geometry["x"], _geometry["y"]

        # No saved position (first run / never moved): create_window already
        # centered it — only shrink if it overflows the primary screen.
        if x is None or y is None:
            _fit_to_primary(window, screens)
            return

        target = next((s for s in screens if _within(s, x, y)), None)
        nw = min(w, (target or screens[0]).width)
        nh = min(h, (target or screens[0]).height)
        if target is not None:
            # On a live screen: keep the position, nudge so it fits within it.
            nx = min(max(x, target.x), target.x + target.width - nw)
            ny = min(max(y, target.y), target.y + target.height - nh)
        else:
            # Saved screen is gone: center on the primary one.
            primary = screens[0]
            nx = primary.x + (primary.width - nw) // 2
            ny = primary.y + (primary.height - nh) // 2
        if (nw, nh) != (w, h):
            window.resize(nw, nh)
        if (nx, ny) != (x, y):
            window.move(nx, ny)
        _geometry.update(width=nw, height=nh, x=nx, y=ny)
    except Exception:  # pylint: disable=broad-exception-caught
        pass
