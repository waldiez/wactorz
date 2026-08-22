"""A cv2.VideoCapture that survives a flaky camera backend.

Generated vision code opens the camera with `cv2.VideoCapture(0)` and assumes it
works. On Windows the MSMF backend regularly grabs the device index and then
fails to deliver frames, which shows up as a flap loop rather than an error, so
the shim retries the open with backoff and settles before probing a read.

Built per agent rather than imported: the class subclasses the real
`cv2.VideoCapture`, and cv2 is an optional dependency that may not be installed.
"""

import logging
import sys
import time
import types
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


def resilient_cv2_module(agent_name: str) -> Any | None:
    """A stand-in `cv2` module whose VideoCapture retries, or None if cv2 is absent."""
    try:
        import cv2 as real_cv2
    except ImportError:
        return None

    class ResilientVideoCapture(real_cv2.VideoCapture):
        """Drop-in replacement for cv2.VideoCapture that retries the open
        with backoff when the MSMF backend grabs the device index but
        then immediately fails to deliver frames.

        Transparent to LLM code — same API, same isinstance() checks.
        """

        _RETRY_DELAYS: ClassVar[list[float]] = [
            1.0,
            2.0,
            4.0,
            8.0,
        ]  # seconds between retries
        # Time to wait after a successful open() before probing read().
        # MSMF/DSHOW source readers need ~200-300ms to start streaming
        # even after isOpened() returns True. Probing too soon yields
        # the cyclic "opened but read failed" log we used to see.
        _POST_OPEN_SETTLE = 0.3  # seconds

        def __init__(self, index_or_path: Any, *args: Any, **kwargs: Any) -> None:
            super().__init__()
            # ── Windows: force DSHOW for integer indices ──────────
            # MSMF (the OpenCV default on Windows) is flaky on
            # consumer laptop / cheap USB cameras and produces
            # error -1072873821 (MF_E_HW_MFT_FAILED_START_STREAMING)
            # in a flap loop. DSHOW (DirectShow) is older but far
            # more reliable for this hardware class. Only override
            # when the LLM didn't pass an explicit backend.
            if (
                sys.platform == "win32"
                and isinstance(index_or_path, int)
                and not args
                and "apiPreference" not in kwargs
            ):
                try:
                    args = (real_cv2.CAP_DSHOW,)
                    logger.info(
                        "[%s] Windows detected — forcing CAP_DSHOW backend for camera index %s (more reliable than MSMF)",
                        agent_name,
                        index_or_path,
                    )
                except Exception as exc:
                    logger.debug("[%s] Could not force the DSHOW backend: %s", agent_name, exc)
            self._index = index_or_path
            self._args = args
            self._kwargs = kwargs
            self._do_open()

        def read(self) -> Any:
            # Return the probe frame captured during open verification
            # so the first cap.read() in process() is not lost.
            if hasattr(self, "_probe_frame") and self._probe_frame is not None:
                frame, self._probe_frame = self._probe_frame, None
                return True, frame
            return super().read()

        def _do_open(self) -> None:
            attempts = len(self._RETRY_DELAYS) + 1
            for attempt, delay in enumerate([0.0, *self._RETRY_DELAYS], start=1):
                if self._open_attempt(attempt, delay, attempts):
                    return

            logger.error("[%s] Camera could not be opened after %s attempts", agent_name, attempts)

        def _open_attempt(self, attempt: int, delay: float, attempts: int) -> bool:
            """One open, settle and probe. True when the camera really works."""
            if delay:
                # Release before retrying so MSMF frees the device
                try:
                    super().release()
                except Exception as exc:
                    logger.debug("[%s] Release before retry failed: %s", agent_name, exc)
                logger.info(
                    "[%s] Camera open retry %s/%s - waiting %.0fs for OS to release device",
                    agent_name,
                    attempt,
                    attempts,
                    delay,
                )
                time.sleep(delay)

            super().open(self._index, *self._args, **self._kwargs)
            if not super().isOpened():
                return False

            # Give the source reader time to start streaming before the probe.
            # MSMF/DSHOW both need a beat after isOpened() returns True; probing
            # immediately produces -1072873821 even when the device is fine.
            time.sleep(self._POST_OPEN_SETTLE)

            # Verify we can actually grab a frame — MSMF sometimes reports
            # isOpened()=True but then immediately errors. Stash the probe frame
            # so the first cap.read() in process() is not handed an empty result
            # (grab() is destructive and there is no unread()).
            ok, probe = super().read()
            if ok and probe is not None:
                self._probe_frame = probe
                logger.info("[%s] Camera opened successfully on attempt %s", agent_name, attempt)
                return True

            logger.warning(
                "[%s] Camera opened but read() failed on attempt %s - device may not be ready",
                agent_name,
                attempt,
            )
            return False

    # Wrap in a module proxy so `import cv2` inside agent code still works,
    # and `cv2.VideoCapture` transparently becomes the resilient version.
    shim: Any = types.ModuleType("cv2")
    shim.__dict__.update(real_cv2.__dict__)
    shim.VideoCapture = ResilientVideoCapture
    return shim
