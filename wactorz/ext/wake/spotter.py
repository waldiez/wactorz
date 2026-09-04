"""Hearing one phrase in a stream of audio, without recognising the rest.

A keyword spotter is a very small recogniser that can only decode the phrases it
was given: it answers with one of them or with nothing. That is the whole point
-- it runs continuously on a room's audio, and audio it does not wake on never
reaches a recogniser, a model, or a log.

Open vocabulary, so the phrase is configuration rather than a trained artefact:
the words are given as text and converted to the model's own tokens at startup.
An invented name costs nothing extra, which matters when the phrase someone
wants is the product's name.
"""

import logging
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:  # optional dependency — the module must import without it
    import sherpa_onnx

    SPOTTING = True
except ImportError:
    # The name stays bound either way: every use sits behind the flag, and an
    # attribute that vanishes with the dependency cannot be patched in tests.
    sherpa_onnx: Any = None
    SPOTTING = False


#: What the model was trained at, and what the capture is opened at.
RATE = 16000

#: How sure the spotter must be before it says it heard the phrase.
#:
#: The model's own default. Lower hears a phrase more often and also hears it
#: when nobody said it; a room that wakes itself is worse than one that needs
#: asking twice, so this errs high and is left to be tuned per room.
THRESHOLD = 0.25

#: The files a keyword model is made of, relative to its directory.
ENCODER = "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"
DECODER = "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"
JOINER = "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"
TOKENS = "tokens.txt"
BPE_MODEL = "bpe.model"


class NoSpotter(RuntimeError):
    """This deployment cannot listen for a phrase, and why.

    The reasons are named here rather than written at each raise: they are the
    whole vocabulary of "this cannot start", and someone reading the class should
    see what can go wrong without going looking for the places that say so.
    """

    @classmethod
    def uninstalled(cls) -> "NoSpotter":
        """The optional dependency is absent."""
        return cls("sherpa-onnx not installed — pip install 'wactorz[wake]'")

    @classmethod
    def nothing_to_listen_for(cls) -> "NoSpotter":
        """Configured with no phrases at all."""
        return cls("no wake phrases configured")

    @classmethod
    def incomplete_model(cls, model_dir: Path, missing: list[str]) -> "NoSpotter":
        """The model directory is short of files the spotter reads."""
        return cls(f"{model_dir} is missing {', '.join(missing)}")


class Spotter:
    """One loaded model, listening for one set of phrases.

    Holds a decoding stream across calls: a phrase arrives over many blocks of
    audio, and a spotter that forgot between them could only ever match a phrase
    that fell inside one block.
    """

    def __init__(self, model_dir: Path, phrases: list[str], threshold: float = THRESHOLD) -> None:
        if not SPOTTING:
            raise NoSpotter.uninstalled()
        if not phrases:
            raise NoSpotter.nothing_to_listen_for()

        # BPE_MODEL among them: the conversion below reads it, and leaving it out
        # turned a configuration answer into an exception the loop retried for
        # ever rather than a refusal it could report and stop on.
        missing = [
            f for f in (ENCODER, DECODER, JOINER, TOKENS, BPE_MODEL) if not (model_dir / f).exists()
        ]
        if missing:
            raise NoSpotter.incomplete_model(model_dir, missing)

        self._phrases = phrases
        # Written to a file rather than passed per stream: the library documents
        # the file as where keywords come from, and the per-stream argument as
        # *extra* ones added to it. Kept for the spotter's lifetime because the
        # library reads it when a stream is created, not only at construction.
        self._held = tempfile.TemporaryDirectory(prefix="wactorz-wake-")
        keywords_file = Path(self._held.name) / "keywords.txt"
        keywords_file.write_text(as_tokens(model_dir, phrases) + "\n", encoding="utf-8")

        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(model_dir / TOKENS),
            encoder=str(model_dir / ENCODER),
            decoder=str(model_dir / DECODER),
            joiner=str(model_dir / JOINER),
            keywords_file=str(keywords_file),
            sample_rate=RATE,
            keywords_threshold=threshold,
        )
        self._stream = self._spotter.create_stream()

    def forget(self) -> None:
        """Drop whatever half-heard phrase is in progress.

        A phrase can be partly matched when the microphone is taken away, and the
        first audio after it comes back would complete it -- so a room could wake
        on a word said before a check, which nobody would connect to anything.
        """
        self._spotter.reset_stream(self._stream)

    def close(self) -> None:
        """Drop the keywords file this spotter was given."""
        self._held.cleanup()

    @property
    def phrases(self) -> list[str]:
        """What this is listening for, as configured."""
        return list(self._phrases)

    def hears(self, samples: list[float]) -> str:
        """The phrase these samples completed, or empty.

        Takes floats in [-1, 1] at :data:`RATE`, the shape the model reads.
        """
        self._stream.accept_waveform(RATE, samples)
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
        said = self._spotter.get_result(self._stream)
        if said:
            # Reset or the phrase stays in the result and every later block
            # reports it again, so one utterance would wake the room repeatedly.
            self._spotter.reset_stream(self._stream)
        return said


def as_tokens(model_dir: Path, phrases: list[str]) -> str:
    """Turn plain phrases into the token lines the spotter expects.

    Done here rather than asking for a prepared file: the phrases are a setting
    someone types, and making them run a command-line tool to convert their own
    wake word before it works is a step that will be got wrong.
    """
    if not SPOTTING:
        raise NoSpotter.uninstalled()
    encoded = sherpa_onnx.text2token(
        [p.upper() for p in phrases],
        tokens=str(model_dir / TOKENS),
        tokens_type="bpe",
        bpe_model=str(model_dir / BPE_MODEL),
    )
    return "\n".join(" ".join(tokens) for tokens in encoded)
