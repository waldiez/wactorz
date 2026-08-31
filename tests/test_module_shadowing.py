"""No test module may shadow a dependency that is actually installed.

Tests share one process and ``sys.modules`` is never restored, so a module-scope
stub outlives the file that created it. Two rules follow:

* On a machine without the optional extras, an empty stand-in is fine — that is
  what lets the suite run dep-free.
* On a machine that *has* the dependency, a stand-in must never replace it.
  ``sys.modules.setdefault`` gets this wrong: it asks whether the module was
  imported yet, not whether it can be. The symptom was `pytest.approx` calling
  ``isscalar`` on an empty ``numpy``.

See :mod:`tests.optional_deps`, which is the safe way to do this.
"""

import importlib
import importlib.machinery
import importlib.util
import sys

import pytest

from tests.optional_deps import ensure_importable

# Optional/heavy deps that test modules stand in for at import time.
_OPTIONAL_DEPS = (
    "aiomqtt",
    "psutil",
    "anthropic",
    "openai",
    "aiohttp",
    "discord",
    "twilio",
    "pdfplumber",
    "fitz",
    "ultralytics",
    "torch",
    "numpy",
    "asyncssh",
)

# Test modules that touch sys.modules at import time — the ones that can plant a
# stand-in merely by being collected.
_IMPORT_TIME_MODULES = (
    "tests.test_supervisor",
    "tests.backend_parity_harness",
    "tests.test_cost_and_history",
    "tests.test_llm_provider_tools",
    "tests.test_ollama_provider",
    "tests.test_one_off_actuator_flow",
)


# Package modules a test file could plausibly hand-load instead of importing.
_CORE_MODULES = ("wactorz.core.actor", "wactorz.core.registry")


def _is_placeholder(name: str) -> bool:
    """True when sys.modules holds an empty stand-in rather than a real module.

    A genuine module — pure Python or C — always carries an import spec; only a
    hand-built ``types.ModuleType`` has ``__spec__`` of None.
    """
    module = sys.modules.get(name)
    return module is not None and getattr(module, "__spec__", None) is None


def _installed(name: str) -> bool:
    """Is the dependency present on disk?

    Asked of the path finder directly, not ``importlib.util.find_spec``: that
    one consults ``sys.modules`` first and raises ValueError on exactly the
    placeholders this module is looking for.
    """
    return importlib.machinery.PathFinder.find_spec(name, sys.path) is not None


def _shadowed() -> list[str]:
    """Installed dependencies that something replaced with a stand-in."""
    return [name for name in _OPTIONAL_DEPS if _is_placeholder(name) and _installed(name)]


@pytest.mark.parametrize("module_name", _IMPORT_TIME_MODULES)
def test_importing_a_test_module_shadows_nothing_installed(module_name: str) -> None:
    """Collecting a test file must not swap out a dependency that is present."""
    importlib.import_module(module_name)
    assert not _shadowed(), (
        f"after importing {module_name}, these installed modules are stand-ins: "
        f"{_shadowed()}. Use tests.optional_deps.ensure_importable instead of "
        "sys.modules.setdefault — setdefault cannot tell 'absent' from "
        "'not imported yet'."
    )


def test_pytest_approx_survives_importing_the_test_modules() -> None:
    """The regression this module exists for."""
    # The concrete symptom: approx() consults sys.modules["numpy"] and calls
    # np.isscalar on whatever it finds there.
    for module_name in _IMPORT_TIME_MODULES:
        importlib.import_module(module_name)
    assert pytest.approx(0.3) == 0.1 + 0.2


@pytest.mark.parametrize("name", ["aiohttp", "aiomqtt", "websockets"])
def test_a_hard_dependency_is_never_stubbed(name: str) -> None:
    """Core deps are always installed; a stand-in only hides breakage."""
    # These are not optional. Faking one makes failures depend on test ordering.
    assert not _is_placeholder(name), f"{name} is a hard dependency and must not be stubbed"


def test_the_safe_helper_does_not_replace_an_installed_module() -> None:
    """A module that is present must survive the call untouched."""

    installed = next(
        (n for n in ("aiohttp", "pytest", "rich") if importlib.util.find_spec(n)), None
    )
    assert installed, "expected at least one of these to be installed"
    ensure_importable(installed)
    assert not _is_placeholder(installed)


def test_the_safe_helper_still_covers_a_missing_module() -> None:
    """The dep-free case: the suite must run without the optional extras."""

    name = "wactorz_no_such_optional_dep"
    try:
        ensure_importable(name)
        assert name in sys.modules
        assert _is_placeholder(name)
    finally:
        sys.modules.pop(name, None)


@pytest.mark.parametrize("module_name", _IMPORT_TIME_MODULES)
def test_importing_a_test_module_replaces_no_wactorz_module(module_name: str) -> None:
    """Collecting a test file must not re-exec a package module under its own name.

    Hand-loading ``wactorz.core.actor`` from its path and storing the result in
    ``sys.modules`` leaves two live copies: whatever imported it earlier keeps
    the first, everything after gets the second. Their classes are then distinct
    objects that compare equal but fail ``is``, so every identity check on an
    enum member silently flips to False.
    """
    for name in _CORE_MODULES:
        importlib.import_module(name)
    before = {name: sys.modules[name] for name in _CORE_MODULES}

    importlib.import_module(module_name)

    replaced = [name for name, module in before.items() if sys.modules[name] is not module]
    assert not replaced, (
        f"importing {module_name} replaced {replaced} in sys.modules. Import the "
        "package normally instead of exec'ing its files through "
        "importlib.util.spec_from_file_location."
    )
