"""app.tcss must only style things the app actually mounts.

Textual silently ignores a selector that matches nothing, so a stylesheet
accumulates rules for widgets that were renamed or deleted and nobody notices.
This walks every `#id` and `.class` selector in the stylesheet and resolves it
against a live app — the check that caught the leftover `#titlebar` rule.
"""

# pylint: disable=missing-function-docstring

import re
from pathlib import Path

import pytest

from wactorz.tui import app as app_mod
from wactorz.tui.app import WactorzTUI
from wactorz.tui.context import TUIContext


def _stylesheet_path() -> Path:
    """Resolve CSS_PATH — Textual stores it relative to the app's module."""
    path = Path(WactorzTUI.CSS_PATH)  # pyright: ignore[reportArgumentType]
    if path.is_absolute():
        return path
    return Path(app_mod.__file__).with_name(str(WactorzTUI.CSS_PATH))


STYLESHEET = _stylesheet_path()
_SOURCE = STYLESHEET.read_text(encoding="utf-8")
# Strip comments so a commented-out rule is not mistaken for a live one.
_RULES = re.sub(r"/\*.*?\*/", "", _SOURCE, flags=re.DOTALL)

_SELECTOR_BLOCKS = re.findall(r"^([^{}]+)\{", _RULES, flags=re.MULTILINE)
_IDS = sorted({m for block in _SELECTOR_BLOCKS for m in re.findall(r"#([\w-]+)", block)})
_CLASSES = sorted({m for block in _SELECTOR_BLOCKS for m in re.findall(r"\.([a-z][\w-]*)", block)})


def test_the_stylesheet_was_actually_found() -> None:
    assert STYLESHEET.is_file()
    assert _IDS and _CLASSES  # the parse below is only meaningful if it found some


@pytest.mark.parametrize("selector_id", _IDS)
async def test_every_styled_id_exists_in_the_app(selector_id: str) -> None:
    """A `#foo` rule with no `id="foo"` widget is dead weight."""
    app = WactorzTUI(TUIContext())
    async with app.run_test() as pilot:
        # Visit every tab: panes are composed lazily inside their TabPane.
        for tab in ("home", "chat", "agents", "logs", "settings"):
            pilot.app.query_one("TabbedContent").active = tab  # type: ignore[attr-defined]
            await pilot.pause()
        assert pilot.app.query(f"#{selector_id}"), f"#{selector_id} styles nothing"


@pytest.mark.parametrize("css_class", _CLASSES)
async def test_every_styled_class_is_used(css_class: str) -> None:
    """A `.foo` rule is live only if some widget is built with that class."""
    import wactorz.tui as tui_pkg  # pylint: disable=import-outside-toplevel

    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in Path(tui_pkg.__file__).parent.rglob("*.py")
    )
    assert css_class in sources, f".{css_class} styles nothing"


def test_no_selector_is_defined_twice_by_accident() -> None:
    # Duplicate blocks for the same selector mean one silently wins.
    seen = [block.strip() for block in _SELECTOR_BLOCKS if block.strip()]
    duplicates = {s for s in seen if seen.count(s) > 1}
    assert not duplicates, f"duplicated selector blocks: {sorted(duplicates)}"
