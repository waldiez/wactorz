"""The camera shim generated vision code gets in place of `cv2`.

A camera that reports open and then delivers no frame is treated as not open:
the shim releases it, waits longer each time, and tries again, and hands the
frame it probed with to the first `read()` so it is not thrown away. On Windows
an integer index is opened with DirectShow unless the code asked for a backend.

Driven against a stand-in `cv2`, so neither OpenCV nor a camera is needed.
"""

import sys
import types
from typing import Any

import pytest

from wactorz.agents.dynamic import cv2_shim
from wactorz.agents.dynamic.cv2_shim import resilient_cv2_module


class _Capture:
    """A camera that opens, and delivers a frame, on the attempts it is told to."""

    frames_from_attempt = 1
    opens_from_attempt = 1
    opened_with: "list[tuple[Any, ...]]" = []  # noqa: RUF012  # shared record, reset per test

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.attempt = 0
        self.released = 0
        self.reads = 0

    def open(self, index: Any, *args: Any, **kwargs: Any) -> bool:
        self.attempt += 1
        _Capture.opened_with.append((index, *args))
        return True

    def isOpened(self) -> bool:  # the OpenCV name
        return self.attempt >= self.opens_from_attempt

    def read(self) -> tuple[bool, Any]:
        self.reads += 1
        if self.attempt >= self.frames_from_attempt:
            return True, f"frame-{self.reads}"
        return False, None

    def release(self) -> None:
        self.released += 1


@pytest.fixture(name="cv2")
def cv2_fixture(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    module = types.ModuleType("cv2")
    module.__dict__.update(VideoCapture=_Capture, CAP_DSHOW=700, COLOR_BGR2RGB=4)
    monkeypatch.setitem(sys.modules, "cv2", module)
    _Capture.frames_from_attempt = 1
    _Capture.opens_from_attempt = 1
    _Capture.opened_with = []
    slept: list[float] = []
    monkeypatch.setattr(cv2_shim.time, "sleep", slept.append)
    return slept


def _shim() -> Any:
    shim = resilient_cv2_module("cam")
    assert shim is not None
    return shim


def test_without_opencv_there_is_no_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "cv2", None)

    assert resilient_cv2_module("cam") is None


def test_the_shim_is_cv2_with_a_resilient_capture(cv2: list[float]) -> None:
    shim = _shim()

    assert shim.COLOR_BGR2RGB == 4
    assert issubclass(shim.VideoCapture, _Capture)


def test_a_working_camera_opens_once_and_keeps_its_probe_frame(cv2: list[float]) -> None:
    capture = _shim().VideoCapture("rtsp://camera")

    assert capture.read() == (True, "frame-1")
    assert capture.read() == (True, "frame-2")
    assert cv2 == [0.3], "only the settle pause before the probe"


def test_a_camera_that_opens_without_frames_is_retried_with_backoff(cv2: list[float]) -> None:
    _Capture.frames_from_attempt = 3

    capture = _shim().VideoCapture("rtsp://camera")

    assert capture.attempt == 3
    assert capture.released == 2
    assert cv2 == [0.3, 1.0, 0.3, 2.0, 0.3]


def test_a_camera_that_never_opens_gives_up_after_every_retry(
    cv2: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    _Capture.opens_from_attempt = 99

    capture = _shim().VideoCapture("rtsp://camera")

    assert capture.attempt == 5
    assert cv2 == [1.0, 2.0, 4.0, 8.0]
    assert "could not be opened after 5 attempts" in caplog.text


def test_a_failing_release_does_not_stop_the_retry(
    cv2: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    _Capture.frames_from_attempt = 2

    def _broken(self: _Capture) -> None:
        raise RuntimeError("busy")

    monkeypatch.setattr(_Capture, "release", _broken)

    assert _shim().VideoCapture("rtsp://camera").attempt == 2


@pytest.mark.parametrize(
    ("args", "kwargs", "expected"),
    [((), {}, (0, 700)), ((200,), {}, (0, 200)), ((), {"apiPreference": 1}, (0,))],
)
def test_windows_uses_directshow_unless_a_backend_was_chosen(
    cv2: list[float],
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    expected: tuple[Any, ...],
) -> None:
    monkeypatch.setattr(cv2_shim.sys, "platform", "win32")

    _shim().VideoCapture(0, *args, **kwargs)

    assert _Capture.opened_with == [expected]
