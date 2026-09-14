"""The Google MCP token file is never readable by anyone but its owner.

It holds an OAuth access token and a refresh token, so a moment spent at the
umask's permissions is a moment in which any account on the machine can copy
them — and a refresh token is good until it is revoked, so copying it once is
enough. Narrowing the mode after the write leaves exactly that window, and
leaves the file readable for good on any platform or filesystem where the
narrowing fails.

Creating the file at 0600 closes both. These tests pin the permissions rather
than the mechanism, so a future rewrite is free as long as the guarantee holds.
"""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from wactorz.core.integrations.google_mcp import _write_private_json

#: Windows has no POSIX mode bits to assert on; the guarantee there comes from
#: the directory's ACL, which is a different test.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")


@posix_only
def test_the_file_is_readable_only_by_its_owner(tmp_path: Path) -> None:
    target = tmp_path / "token.json"

    _write_private_json(target, {"tokens": {"refresh_token": "secret"}})

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@posix_only
def test_a_permissive_umask_cannot_widen_it(tmp_path: Path) -> None:
    """The mode is asked for at creation, so the umask never gets a say."""
    target = tmp_path / "token.json"
    previous = os.umask(0)
    try:
        _write_private_json(target, {"tokens": {"refresh_token": "secret"}})
    finally:
        os.umask(previous)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@posix_only
def test_it_stays_private_when_chmod_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The permissions are asked for at creation, so there is no later step left
    to fail. Narrowing them after the write left the tokens readable by everyone
    on any filesystem or platform that refused the call, and said nothing.

    This is the case the other tests here cannot see: writing at the umask and
    narrowing afterwards also ends at 0600 whenever the narrowing works.
    """

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("chmod refused")

    monkeypatch.setattr(Path, "chmod", refuse)
    monkeypatch.setattr(os, "chmod", refuse)

    target = tmp_path / "token.json"
    previous = os.umask(0)
    try:
        _write_private_json(target, {"tokens": {"refresh_token": "secret"}})
    finally:
        os.umask(previous)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_the_content_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    payload = {"tokens": {"access_token": "a", "refresh_token": "r"}}

    _write_private_json(target, payload)

    assert json.loads(target.read_text(encoding="utf-8")) == payload


def test_the_parent_directory_is_created(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "token.json"

    _write_private_json(target, {"tokens": {}})

    assert target.exists()


@posix_only
def test_rewriting_keeps_the_permissions(tmp_path: Path) -> None:
    """A refresh writes through the same path, so the second file is private too."""
    target = tmp_path / "token.json"
    _write_private_json(target, {"tokens": {"access_token": "first"}})

    _write_private_json(target, {"tokens": {"access_token": "second"}})

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text(encoding="utf-8"))["tokens"]["access_token"] == "second"


def test_a_failed_write_leaves_the_previous_file_whole(tmp_path: Path) -> None:
    """The replace is the last step, so a write that raises cannot truncate what
    was already there — the caller still has the tokens it had before."""
    target = tmp_path / "token.json"
    _write_private_json(target, {"tokens": {"access_token": "keep"}})

    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        _write_private_json(target, {"tokens": Unserialisable()})  # pyright: ignore[reportArgumentType]

    assert json.loads(target.read_text(encoding="utf-8"))["tokens"]["access_token"] == "keep"


def test_a_failed_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "token.json"

    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        _write_private_json(target, {"tokens": Unserialisable()})  # pyright: ignore[reportArgumentType]

    assert not list(tmp_path.iterdir())


@posix_only
def test_a_symlink_at_the_temp_path_is_refused(tmp_path: Path) -> None:
    """The write goes through a temp file created with O_EXCL, so a symlink left
    at that path is an error rather than somewhere the tokens are delivered."""
    target = tmp_path / "token.json"
    elsewhere = tmp_path / "attacker-readable.json"
    (tmp_path / f".{target.name}.{os.getpid()}.tmp").symlink_to(elsewhere)

    with pytest.raises(FileExistsError):
        _write_private_json(target, {"tokens": {"refresh_token": "secret"}})

    assert not elsewhere.exists()
