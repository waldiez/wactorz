"""Where an install is allowed to land, and what it is allowed to be.

Two callers share this: the installer agent checks the names in a spawn config
before sending them anywhere, and a node installs what an agent it was sent says
it imports. Both take their list from a payload off the broker, so the rule
about what a name may look like is covered in `test_installer_package_names`.

What is covered here is the other half — the command built around those names.
It is ordered by how little of the host it disturbs, and nothing else says so.
"""

import sys

import pytest

from wactorz.core import pip


def _outside_a_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pip, "in_virtualenv", lambda: False)


def _as_root(monkeypatch: pytest.MonkeyPatch, root: bool) -> None:
    monkeypatch.setattr(pip, "is_root", lambda: root)


class TestWhereAnInstallLands:
    """Ordered by how little of the host it disturbs.

    Inside a virtualenv nothing outside it is touched. Outside one, an
    unprivileged install goes to the user's own site-packages, leaving the
    distribution's tree alone. Only root writes where the system package
    manager expects to be in charge.
    """

    def test_inside_a_virtualenv_nothing_special_is_asked_for(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pip, "in_virtualenv", lambda: True)

        cmd, env = pip.install_command(["requests"])

        assert "--user" not in cmd
        assert env == {}, "a virtualenv needs no override to be written to"

    def test_outside_one_an_ordinary_user_writes_only_their_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _outside_a_venv(monkeypatch)
        _as_root(monkeypatch, False)

        cmd, env = pip.install_command(["requests"])

        assert "--user" in cmd
        assert env == {"PIP_BREAK_SYSTEM_PACKAGES": "1"}

    def test_root_writes_where_the_package_manager_is_in_charge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _outside_a_venv(monkeypatch)
        _as_root(monkeypatch, True)

        cmd, env = pip.install_command(["requests"])

        assert "--user" not in cmd
        assert env == {"PIP_BREAK_SYSTEM_PACKAGES": "1"}

    def test_the_override_goes_to_the_child_not_this_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Through the environment, not `--break-system-packages`.

        No pip before 23.0.1 knows that flag, and an edge node running an older
        one would fail on the argument instead of installing. Returned for the
        caller to hand to the child rather than set here, so two installs
        cannot race over it.
        """
        _outside_a_venv(monkeypatch)
        _as_root(monkeypatch, False)

        cmd, env = pip.install_command(["requests"])

        assert "--break-system-packages" not in cmd
        assert "PIP_BREAK_SYSTEM_PACKAGES" not in str(cmd)
        assert env["PIP_BREAK_SYSTEM_PACKAGES"] == "1"


class TestTheCommandItself:
    def test_it_names_this_interpreter(self) -> None:
        # `sys.executable -m pip`, so the packages land in the environment doing
        # the asking rather than whichever pip happens to be on PATH.
        cmd, _env = pip.install_command(["requests"])

        assert cmd[:4] == [sys.executable, "-m", "pip", "install"]

    def test_every_package_asked_for_is_in_it(self) -> None:
        cmd, _env = pip.install_command(["requests", "numpy>=1.2", "uvicorn[standard]"])

        assert {"requests", "numpy>=1.2", "uvicorn[standard]"} <= set(cmd)

    def test_it_never_asks_a_question(self) -> None:
        # There is nobody at the other end on a node: a pip that stopped to ask
        # would hold the spawn until the timeout.
        cmd, _env = pip.install_command(["requests"])

        assert "--no-input" in cmd


class TestSayingWhereItWent:
    """The log line that follows every install, so it is not a guess."""

    def test_a_virtualenv_is_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pip, "in_virtualenv", lambda: True)

        assert sys.prefix in pip.install_destination()

    def test_the_users_own_site_packages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _outside_a_venv(monkeypatch)
        _as_root(monkeypatch, False)

        assert "user" in pip.install_destination()

    def test_the_system_interpreter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _outside_a_venv(monkeypatch)
        _as_root(monkeypatch, True)

        assert sys.executable in pip.install_destination()


class TestKnowingWhereWeAre:
    def test_this_checkout_runs_in_one(self) -> None:
        # The suite runs from `.venv`, so this is measurable rather than mocked.
        assert pip.in_virtualenv() is True

    def test_a_plain_interpreter_is_not_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(sys, "real_prefix", raising=False)
        monkeypatch.setattr(sys, "base_prefix", sys.prefix)

        assert pip.in_virtualenv() is False

    def test_the_shape_the_old_virtualenv_package_left(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Everything since moves `prefix`; `real_prefix` is what the legacy
        # package set, and a node may be running a very old one.
        monkeypatch.setattr(sys, "base_prefix", sys.prefix)
        monkeypatch.setattr(sys, "real_prefix", "/usr", raising=False)

        assert pip.in_virtualenv() is True

    def test_a_symlinked_environment_is_still_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Both are resolved before comparison, or an environment reached through
        # a symlink looks unequal to itself and every install grows a `--user`.
        monkeypatch.delattr(sys, "real_prefix", raising=False)
        monkeypatch.setattr(sys, "prefix", "/usr/./")
        monkeypatch.setattr(sys, "base_prefix", "/usr")

        assert pip.in_virtualenv() is False


class TestWhoWeAre:
    def test_on_posix_it_asks_the_uid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        if pip.os.name == "nt":
            pytest.skip("POSIX uid")
        monkeypatch.setattr(pip.os, "getuid", lambda: 0, raising=False)
        assert pip.is_root() is True

        monkeypatch.setattr(pip.os, "getuid", lambda: 1000, raising=False)
        assert pip.is_root() is False

    def test_windows_is_asked_whether_the_token_is_elevated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No uid there; shell32 answers the equivalent question.
        monkeypatch.setattr(pip.os, "name", "nt")
        import ctypes

        class _Shell32:
            @staticmethod
            def IsUserAnAdmin() -> int:  # the Windows API's own name
                return 1

        monkeypatch.setattr(ctypes, "windll", type("W", (), {"shell32": _Shell32}), raising=False)

        assert pip.is_root() is True

    def test_windows_that_will_not_answer_is_read_as_unprivileged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The safe direction: the install takes the contained path rather than
        assuming it may write anywhere."""
        monkeypatch.setattr(pip.os, "name", "nt")
        import ctypes

        class _Broken:
            @staticmethod
            def IsUserAnAdmin() -> int:  # the Windows API's own name
                raise OSError("no shell32 here")

        monkeypatch.setattr(ctypes, "windll", type("W", (), {"shell32": _Broken}), raising=False)

        assert pip.is_root() is False
