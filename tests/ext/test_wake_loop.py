"""The policy the wake loop carries: what it retries, what it gives up on, and
who owns the microphone while it runs.

Driven against a fake device, the way the host listener's own tests are: what a
real model hears is a question for a room, not for a unit test.
"""

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from wactorz.ext.stt import listener
from wactorz.ext.wake import loop as wake_loop
from wactorz.ext.wake import spotter


class _Stream:
    """A microphone that answers with the blocks it was given."""

    def __init__(self, blocks: list[bytes]) -> None:
        self.blocks = blocks
        self.reads = 0
        self.closed = False

    def read(self, _frames: int) -> tuple[bytes, bool]:
        self.reads += 1
        if self.blocks:
            return self.blocks.pop(0), False
        return b"\x00\x00" * 800, False

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.closed = True


def _a_machine(
    monkeypatch: pytest.MonkeyPatch,
    stream: _Stream | None,
    hears: list[str],
) -> None:
    """A deployment with a microphone, a model, and a spotter that hears `hears`."""

    class _Ear:
        def __init__(self, *_a: object, **_k: object) -> None:
            self.closed = False

        def hears(self, _samples: list[float]) -> str:
            return hears.pop(0) if hears else ""

        def forget(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    class _Device:
        @staticmethod
        def RawInputStream(**_kwargs: object) -> _Stream:
            if stream is None:
                raise OSError("no such device")
            return stream

    monkeypatch.setattr(listener, "AUDIO", True)
    monkeypatch.setattr(listener, "sounddevice", _Device)
    monkeypatch.setattr(listener, "room_floor", lambda *_a: 0.0)
    monkeypatch.setattr(spotter, "Spotter", _Ear)


def _loop(on_turn: Any = lambda _wav: None) -> wake_loop.WakeLoop:
    return wake_loop.WakeLoop(Path("/nowhere"), ["hey waldiez"], on_turn)


class TestWhatItGivesUpOn:
    async def test_a_missing_model_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A configuration answer: trying again cannot make the weights appear,
        # and a loop that retried would say the same thing every thirty seconds
        # for the life of the deployment.
        tries = 0

        def _refuse(*_a: object, **_k: object) -> None:
            nonlocal tries
            tries += 1
            raise spotter.NoSpotter("no model")

        monkeypatch.setattr(listener, "AUDIO", True)
        monkeypatch.setattr(spotter, "Spotter", _refuse)

        await asyncio.wait_for(_loop().run(), timeout=2)

        assert tries == 1

    async def test_a_missing_microphone_is_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Not a configuration answer: a device that is unplugged can be plugged
        # in, and that should start working without a restart.
        _a_machine(monkeypatch, None, [])
        monkeypatch.setattr(wake_loop, "RETRY_SECONDS", 0.01)
        ear = _loop()

        async def _let_it_try_twice() -> None:
            await asyncio.sleep(0.05)
            ear.stop()

        await asyncio.gather(asyncio.wait_for(ear.run(), timeout=2), _let_it_try_twice())

        assert ear.stopping

    async def test_the_model_is_not_loaded_without_a_sound_device(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Twelve megabytes of weights, paid on every retry, only to be told what
        # the flag says immediately.
        loaded = 0

        def _count(*_a: object, **_k: object) -> None:
            nonlocal loaded
            loaded += 1

        monkeypatch.setattr(listener, "AUDIO", False)
        monkeypatch.setattr(spotter, "Spotter", _count)
        monkeypatch.setattr(wake_loop, "RETRY_SECONDS", 0.01)
        ear = _loop()

        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            ear.stop()

        await asyncio.gather(asyncio.wait_for(ear.run(), timeout=2), _stop_soon())

        assert loaded == 0


class TestWakingOnThePhrase:
    async def test_a_turn_is_read_from_the_same_open_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The sentence begins in the same breath as the phrase, so closing the
        # device to reopen it for the turn loses the first words of every one.
        stream = _Stream([b"\x10\x00" * 800])
        _a_machine(monkeypatch, stream, ["HEY WALDIEZ"])
        gathered: list[object] = []
        monkeypatch.setattr(
            listener, "gather_turn", lambda s, *_a: gathered.append(s) or b"\x01\x02"
        )
        turns: list[bytes] = []
        ear = _loop(turns.append)

        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            ear.stop()

        await asyncio.gather(asyncio.wait_for(ear.run(), timeout=2), _stop_soon())

        assert gathered == [stream]
        assert turns and turns[0].startswith(b"RIFF")

    async def test_a_turn_nobody_spoke_in_is_not_handed_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _a_machine(monkeypatch, _Stream([b"\x10\x00" * 800]), ["HEY WALDIEZ"])
        monkeypatch.setattr(listener, "gather_turn", lambda *_a: b"")
        turns: list[bytes] = []
        ear = _loop(turns.append)

        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            ear.stop()

        await asyncio.gather(asyncio.wait_for(ear.run(), timeout=2), _stop_soon())

        assert not turns


class TestGivingTheMicrophoneBack:
    async def test_yielding_waits_until_the_stream_is_actually_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Whoever asked opens the device next, and two streams on one microphone
        # is the failure this exists to avoid.
        stream = _Stream([])
        _a_machine(monkeypatch, stream, [])
        monkeypatch.setattr(wake_loop, "RETRY_SECONDS", 0.01)
        ear = _loop()
        freed = threading.Event()

        async def _ask_for_it() -> None:
            await asyncio.sleep(0.05)
            if await asyncio.to_thread(ear.yield_device, 2.0):
                freed.set()
            ear.stop()

        await asyncio.gather(asyncio.wait_for(ear.run(), timeout=3), _ask_for_it())

        assert freed.is_set()
        assert stream.closed

    async def test_a_hand_back_is_not_mistaken_for_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The borrower clears the flag the moment it is finished, which can
        # happen before the loop looks at it. Deciding by re-reading the flag
        # therefore reads a quick hand-back as a failure and parks on the retry
        # timer -- deaf for half a minute after a check that took a second.
        ear = _loop()
        waited: list[str] = []

        def _yielded_then_returned() -> bool:
            # Exactly the losing order: given back before the loop can look.
            ear._yield_asked.clear()
            return True

        async def _resumed() -> None:
            waited.append("resume")
            ear.stop()

        async def _retried() -> None:
            waited.append("retry")
            ear.stop()

        monkeypatch.setattr(ear, "_listen_blocking", _yielded_then_returned)
        monkeypatch.setattr(ear, "_wait_until_resumed", _resumed)
        monkeypatch.setattr(ear, "_wait_before_retrying", _retried)

        await asyncio.wait_for(ear.run(), timeout=2)

        assert waited == ["resume"]

    async def test_it_starts_listening_again_as_soon_as_the_device_comes_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The pause after a yield is a wait on the hand-back, not the retry
        # timer. Parking for the retry spell would leave the room deaf for half
        # a minute after a check that took a second.
        stream = _Stream([])
        _a_machine(monkeypatch, stream, [])
        monkeypatch.setattr(wake_loop, "RETRY_SECONDS", 30.0)
        ear = _loop()

        async def _borrow_and_give_back() -> None:
            await asyncio.sleep(0.05)
            await asyncio.to_thread(ear.yield_device, 2.0)
            reads_while_parked = stream.reads
            ear.resume()
            await asyncio.sleep(0.3)
            # Listening again well inside the retry spell it must not be using.
            assert stream.reads > reads_while_parked
            ear.stop()

        await asyncio.gather(asyncio.wait_for(ear.run(), timeout=3), _borrow_and_give_back())

    async def test_the_model_is_not_reloaded_on_every_hand_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Twelve megabytes of weights per microphone check would sit oddly with
        # not paying that cost on every retry.
        loads = 0
        stream = _Stream([])

        class _CountingEar:
            def __init__(self, *_a: object, **_k: object) -> None:
                nonlocal loads
                loads += 1

            def hears(self, _samples: list[float]) -> str:
                return ""

            def forget(self) -> None:
                return None

            def close(self) -> None:
                return None

        _a_machine(monkeypatch, stream, [])
        monkeypatch.setattr(spotter, "Spotter", _CountingEar)
        ear = _loop()

        async def _borrow_twice() -> None:
            await asyncio.sleep(0.05)
            for _ in range(2):
                await asyncio.to_thread(ear.yield_device, 2.0)
                ear.resume()
                await asyncio.sleep(0.1)
            ear.stop()

        await asyncio.gather(asyncio.wait_for(ear.run(), timeout=3), _borrow_twice())

        assert loads == 1

    async def test_the_model_is_released_even_if_the_device_never_opened(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Otherwise its temporary keywords file is left to the garbage
        # collector, which is not when a deployment expects its files gone.
        closed: list[bool] = []

        class _Ear:
            def __init__(self, *_a: object, **_k: object) -> None:
                pass

            def hears(self, _samples: list[float]) -> str:  # pragma: no cover
                return ""

            def forget(self) -> None:  # pragma: no cover
                return None

            def close(self) -> None:
                closed.append(True)

        _a_machine(monkeypatch, None, [])
        monkeypatch.setattr(spotter, "Spotter", _Ear)
        monkeypatch.setattr(wake_loop, "RETRY_SECONDS", 0.01)
        ear = _loop()

        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            ear.stop()

        await asyncio.gather(asyncio.wait_for(ear.run(), timeout=2), _stop_soon())

        assert closed == [True]

    async def test_it_does_not_open_a_device_it_has_already_been_asked_for(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A request arriving while parked would otherwise be answered by opening
        # the device the borrower has just been handed, and closing it a block
        # later once the flag was noticed -- a spurious failure on an exclusive
        # device, and another wait.
        opens: list[bool] = []

        class _Device:
            @staticmethod
            def RawInputStream(**_kwargs: object) -> _Stream:
                opens.append(True)
                return _Stream([])

        _a_machine(monkeypatch, _Stream([]), [])
        monkeypatch.setattr(listener, "sounddevice", _Device)
        ear = _loop()
        ear._yield_asked.set()

        assert await asyncio.to_thread(ear._listen_blocking) is True
        assert not opens

    async def test_a_half_heard_phrase_does_not_survive_the_hand_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The model keeps its decoding stream across a hand-back, so a phrase
        # partly said before a check could be completed by the first audio after
        # it -- waking the room on a word nobody would connect to anything.
        forgot: list[bool] = []

        class _Ear:
            def __init__(self, *_a: object, **_k: object) -> None:
                pass

            def hears(self, _samples: list[float]) -> str:
                return ""

            def forget(self) -> None:
                forgot.append(True)

            def close(self) -> None:
                return None

        _a_machine(monkeypatch, _Stream([]), [])
        monkeypatch.setattr(spotter, "Spotter", _Ear)
        ear = _loop()

        async def _borrow_and_give_back() -> None:
            await asyncio.sleep(0.05)
            await asyncio.to_thread(ear.yield_device, 2.0)
            ear.resume()
            await asyncio.sleep(0.2)
            ear.stop()

        await asyncio.gather(asyncio.wait_for(ear.run(), timeout=3), _borrow_and_give_back())

        # Once on the way in, and again on regaining the device.
        assert len(forgot) >= 2

    async def test_the_device_reads_as_free_before_anything_has_opened_it(self) -> None:
        # Asked before the loop ever ran, the answer is still "nothing holds it".
        assert await asyncio.to_thread(_loop().yield_device, 0.5)
