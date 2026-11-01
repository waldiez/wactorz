"""The reference's command table says what `/help` says.

The table is written by `scripts/sync_command_docs.py` from the commands' own
forms; these tests fail while it is out of date, and say how to bring it back.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from wactorz.agents.main.commands import registry
from wactorz.agents.main.commands.dispatch import Form
from wactorz.agents.main.commands.info import help_lines, help_sections

ROOT = Path(__file__).resolve().parents[1]


def _load_script() -> ModuleType:
    """Import `scripts/sync_command_docs.py`, which is not an installed module."""
    path = ROOT / "scripts" / "sync_command_docs.py"
    spec = importlib.util.spec_from_file_location("sync_command_docs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _load_script()


def test_the_reference_table_is_up_to_date() -> None:
    page = sync.REFERENCE.read_text(encoding="utf-8")

    expected = sync.with_table(page, sync.render_table(help_sections(registry)))

    assert page == expected, "run: python scripts/sync_command_docs.py"


def test_every_form_help_lists_is_in_the_table() -> None:
    page = sync.REFERENCE.read_text(encoding="utf-8")

    for _, forms in help_sections(registry):
        for form in forms:
            assert f"`{sync._cell(form.usage)}`" in page, form.usage


def test_help_and_the_table_list_the_same_headings_in_the_same_order() -> None:
    headings = [group for group, _ in help_sections(registry)]
    help_text = "\n".join(help_lines(registry))
    table = sync.render_table(help_sections(registry))

    assert [h for h in headings if f"**{h}**" in help_text] == headings
    assert [line[5:] for line in table.splitlines() if line.startswith("#### ")] == headings


def test_a_pipe_in_a_usage_does_not_end_its_cell() -> None:
    table = sync.render_table([("Models", [Form("/x clear a|b <name>", "unset a|b")])])

    assert r"| `/x clear a\|b <name>` | Unset a\|b |" in table


def test_a_page_without_markers_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no"):
        sync.with_table("# Reference\n", "table", tmp_path / "reference.md")


def test_check_reports_a_stale_table(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stale = tmp_path / "reference.md"
    stale.write_text(f"# Reference\n\n{sync.START}\n{sync.END}\n", encoding="utf-8")
    monkeypatch.setattr(sync, "REFERENCE", stale)
    monkeypatch.setattr(sync, "ROOT", tmp_path)

    assert sync.main(["--check"]) == 1
    assert stale.read_text(encoding="utf-8").count("|") == 0

    assert sync.main([]) == 0
    assert sync.main(["--check"]) == 0
