"""What a model is shown when a turn carries attachments.

The rule that shapes this: a file can be small enough to upload and still too
large to send. Base64 costs a third on top and the whole request is capped, so
the usable inline size sits well below the upload limit — and a file in that gap
must be named to the model rather than dropped, or the agent answers as though
nothing was attached.
"""

from typing import Any

import pytest

from wactorz import config
from wactorz.agents.llm import attachments

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
PDF = b"%PDF-1.7\n" + b"\x00" * 40


def _ref(mime: str, size: int = 10, name: str = "file", file_id: str = "a" * 32) -> dict[str, Any]:
    return {"id": file_id, "name": name, "mime": mime, "size": size}


def _reader(data: bytes | None) -> Any:
    return lambda _file_id: data


class TestWhatTheModelCanRead:
    def test_an_image_becomes_an_image_block(self) -> None:
        blocks = attachments.to_blocks([_ref("image/png")], _reader(PNG))

        assert blocks[0]["type"] == "image"
        assert blocks[0]["source"]["media_type"] == "image/png"

    def test_a_pdf_becomes_a_document_block(self) -> None:
        blocks = attachments.to_blocks([_ref("application/pdf")], _reader(PDF))

        assert blocks[0]["type"] == "document"

    def test_encoded_data_carries_no_newlines(self) -> None:
        # The encoded string is sent verbatim; a wrapped one is rejected.
        blocks = attachments.to_blocks([_ref("image/png")], _reader(PNG * 100))

        assert "\n" not in blocks[0]["source"]["data"]

    def test_text_is_inlined_under_its_name(self) -> None:
        blocks = attachments.to_blocks([_ref("text/csv", name="sales.csv")], _reader(b"a,b\n1,2\n"))

        assert blocks[0]["type"] == "text"
        assert "sales.csv" in blocks[0]["text"]
        assert "a,b" in blocks[0]["text"]

    def test_text_is_decided_by_content_not_by_the_label(self) -> None:
        # The declared type is the one part of an upload nobody verified, and a
        # csv, json and md are all just text.
        blocks = attachments.to_blocks([_ref("application/octet-stream")], _reader(b"plain text"))

        assert blocks[0]["type"] == "text"
        assert "plain text" in blocks[0]["text"]


class TestWhatItCannotRead:
    def test_a_file_over_the_inline_limit_is_named_not_dropped(self) -> None:
        # ⚠ The gap: this uploads fine and cannot be sent. Silence here reads as
        # the agent ignoring a file the user can see they attached.
        blocks = attachments.to_blocks(
            [_ref("image/png", size=attachments.MAX_INLINE_BYTES + 1, name="huge.png")],
            _reader(PNG),
        )

        assert blocks[0]["type"] == "text"
        assert "huge.png" in blocks[0]["text"]
        assert "too large" in blocks[0]["text"]

    def test_the_oversize_file_is_never_read_from_disk(self) -> None:
        # Reading it in to discover it is too big would be the cost the limit
        # exists to avoid.
        def _explode(_file_id: str) -> bytes:
            raise AssertionError("read a file that cannot be sent")

        attachments.to_blocks([_ref("image/png", size=attachments.MAX_INLINE_BYTES + 1)], _explode)

    def test_audio_is_named_rather_than_silently_skipped(self) -> None:
        # Transcription is separate work; when it lands it fills in the content
        # without changing this shape.
        blocks = attachments.to_blocks([_ref("audio/ogg", name="note.ogg")], _reader(b"\x00\x01"))

        assert "note.ogg" in blocks[0]["text"]
        assert "transcribed" in blocks[0]["text"]

    def test_a_reference_whose_file_is_gone(self) -> None:
        blocks = attachments.to_blocks([_ref("image/png", name="old.png")], _reader(None))

        assert "old.png" in blocks[0]["text"]
        assert "no longer available" in blocks[0]["text"]

    def test_an_unreadable_binary_is_named(self) -> None:
        blocks = attachments.to_blocks(
            [_ref("application/zip", name="bundle.zip")], _reader(b"PK\x03\x04\x00\x01")
        )

        assert "bundle.zip" in blocks[0]["text"]


class TestTheLimitItself:
    def test_it_leaves_room_for_encoding_and_the_rest_of_the_request(self) -> None:
        # A file at the limit must still fit once base64 has added a third and
        # the surrounding JSON has been counted.
        encoded = attachments.MAX_INLINE_BYTES * 4 / 3

        assert encoded < 32 * 1024 * 1024

    def test_it_is_below_what_the_upload_endpoint_accepts(self) -> None:
        # ⚠ Deliberate: the two limits are different questions. If this ever
        # inverts, the "too large" branch above becomes unreachable and oversize
        # files start failing at the model instead.
        assert attachments.MAX_INLINE_BYTES < config.UPLOAD_MAX_BYTES


class TestOrdering:
    def test_blocks_keep_the_order_they_were_attached_in(self) -> None:
        refs = [_ref("image/png", name="one"), _ref("application/pdf", name="two")]

        blocks = attachments.to_blocks(refs, lambda file_id: PNG)

        assert [b["type"] for b in blocks] == ["image", "document"]

    def test_nothing_attached_is_no_blocks(self) -> None:
        assert attachments.to_blocks([], _reader(PNG)) == []


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg", "image/gif", "image/webp"])
def test_every_type_the_uploader_serves_inline_is_readable(mime: str) -> None:
    # The two lists have to agree: a type the browser renders but the model
    # cannot read would show a thumbnail the agent then claims not to see.
    blocks = attachments.to_blocks([_ref(mime)], _reader(PNG))

    assert blocks[0]["type"] == "image"
