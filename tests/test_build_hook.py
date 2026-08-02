"""Tests for the packaging build hook's rebuild decision.

The hook decides whether ``static/app`` (committed, and shipped in the wheel)
needs rebuilding before packaging. Getting that wrong is quiet in both
directions: rebuilding when nothing changed rewrites committed files and leaves
a dirty tree, while skipping a rebuild after a real edit publishes a wheel whose
bundled dashboard does not match its source.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_HOOK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "build_hook.py"


def _load_hook():
    """Import the hook module by path — it lives outside the package."""
    spec = importlib.util.spec_from_file_location("wactorz_build_hook", _HOOK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A miniature project: a frontend with one source file, and a built dist."""
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "main.ts").write_text("export const a = 1;\n")
    (frontend / "package.json").write_text('{"packageManager": "bun@1.3.14"}\n')
    dist = tmp_path / "static" / "app"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>\n")
    return tmp_path


def _dist(project: Path) -> Path:
    return project / "static" / "app" / "index.html"


def _frontend(project: Path) -> Path:
    return project / "frontend"


class TestIsStale:
    def test_missing_dist_always_rebuilds(self, project: Path) -> None:
        assert hook._is_stale(project / "static" / "app" / "gone.html", _frontend(project))

    def test_unbuilt_here_rebuilds(self, project: Path) -> None:
        # no fingerprint recorded yet — we cannot know the dist matches
        assert hook._is_stale(_dist(project), _frontend(project))

    def test_unchanged_sources_do_not_rebuild(self, project: Path) -> None:
        hook._record_fingerprint(_frontend(project))
        assert not hook._is_stale(_dist(project), _frontend(project))

    def test_edited_source_rebuilds(self, project: Path) -> None:
        hook._record_fingerprint(_frontend(project))
        (_frontend(project) / "src" / "main.ts").write_text("export const a = 2;\n")
        assert hook._is_stale(_dist(project), _frontend(project))

    def test_new_source_file_rebuilds(self, project: Path) -> None:
        hook._record_fingerprint(_frontend(project))
        (_frontend(project) / "src" / "extra.ts").write_text("export const b = 1;\n")
        assert hook._is_stale(_dist(project), _frontend(project))

    def test_deleted_source_rebuilds(self, project: Path) -> None:
        (_frontend(project) / "src" / "extra.ts").write_text("export const b = 1;\n")
        hook._record_fingerprint(_frontend(project))
        (_frontend(project) / "src" / "extra.ts").unlink()
        assert hook._is_stale(_dist(project), _frontend(project))

    def test_touching_a_source_without_editing_it_does_not_rebuild(self, project: Path) -> None:
        """The reason this is content-hashed rather than timestamped."""
        hook._record_fingerprint(_frontend(project))
        src = _frontend(project) / "src" / "main.ts"
        import os
        import time

        os.utime(src, (time.time() + 10_000, time.time() + 10_000))
        assert not hook._is_stale(_dist(project), _frontend(project))

    def test_a_file_that_cannot_affect_the_bundle_does_not_rebuild(self, project: Path) -> None:
        hook._record_fingerprint(_frontend(project))
        (_frontend(project) / "NOTES.md").write_text("not an input\n")
        assert not hook._is_stale(_dist(project), _frontend(project))

    def test_missing_frontend_keeps_the_committed_dist(self, project: Path) -> None:
        assert not hook._is_stale(_dist(project), project / "no-frontend")


class TestFingerprint:
    def test_is_stable_across_calls(self, project: Path) -> None:
        first = hook._input_fingerprint(_frontend(project))
        assert first == hook._input_fingerprint(_frontend(project))

    def test_distinguishes_content(self, project: Path) -> None:
        before = hook._input_fingerprint(_frontend(project))
        (_frontend(project) / "src" / "main.ts").write_text("export const a = 3;\n")
        assert hook._input_fingerprint(_frontend(project)) != before

    def test_distinguishes_which_file_holds_the_content(self, project: Path) -> None:
        """Path is hashed too, so moving content between files is a change."""
        src = _frontend(project) / "src"
        (src / "one.ts").write_text("X\n")
        (src / "two.ts").write_text("Y\n")
        before = hook._input_fingerprint(_frontend(project))
        (src / "one.ts").write_text("Y\n")
        (src / "two.ts").write_text("X\n")
        assert hook._input_fingerprint(_frontend(project)) != before

    def test_unwritable_cache_is_not_fatal(self, project: Path, monkeypatch) -> None:
        def _boom(*_args, **_kwargs):
            raise OSError("read-only")

        monkeypatch.setattr(Path, "mkdir", _boom)
        hook._record_fingerprint(_frontend(project))  # must not raise


def test_forced_rebuild_ignores_the_fingerprint(project: Path, monkeypatch) -> None:
    """Release and CI builds set the env var to rebuild unconditionally."""
    hook._record_fingerprint(_frontend(project))
    monkeypatch.setattr(hook, "STALE_AFTER", 0)
    assert hook._is_stale(_dist(project), _frontend(project))
