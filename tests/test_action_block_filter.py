"""A streamed reply loses its action blocks however it is cut into chunks.

Main runs the blocks once the reply is whole; the user is sent the text around
them as it arrives. The filter must give the same text as the regular
expressions of the non-streaming path, wherever a chunk boundary falls.
"""

import re

import pytest

from wactorz.agents.main.blocks import ActionBlockFilter

REPLIES = [
    "No blocks here.",
    'Starting it.\n<spawn>\n{"name": "a"}\n</spawn>\n\nDone.',
    '<delete>{"name": "a"}</delete>',
    'Asking. <delegate>{"agent": "w", "task": "Athens"}</delegate> Wait.',
    'one <spawn>{"a": 1}</spawn> two <delete>{"b": 2}</delete> three',
    "a < b and c <spa d <del e </spawn> f",
    "trailing <",
    "trailing <spaw",
]


def _expected(reply: str) -> str:
    return re.sub(r"<(spawn|delete|delegate)>.*?</\1>", "", reply, flags=re.DOTALL)


def _streamed(chunks: list[str]) -> tuple[str, list[str]]:
    blocks = ActionBlockFilter()
    shown = [blocks.feed(chunk) for chunk in chunks]
    shown.append(blocks.flush())
    return "".join(shown), shown


@pytest.mark.parametrize("reply", REPLIES)
def test_any_two_chunks_give_the_whole_replys_text(reply: str) -> None:
    for cut in range(len(reply) + 1):
        text, _ = _streamed([reply[:cut], reply[cut:]])
        assert text == _expected(reply), cut


@pytest.mark.parametrize("reply", REPLIES)
def test_one_character_at_a_time_gives_the_same_text(reply: str) -> None:
    text, _ = _streamed(list(reply))

    assert text == _expected(reply)


def test_no_part_of_a_block_is_shown_on_the_way() -> None:
    reply = 'ok <spawn>{"name": "a"}</spawn> fine'

    _, shown = _streamed(list(reply))

    assert not any("spawn" in part or "{" in part for part in shown)


def test_prose_with_an_angle_bracket_is_held_back_one_chunk_at_most() -> None:
    blocks = ActionBlockFilter()

    assert blocks.feed("a <") == "a "
    assert blocks.feed(" b") == "< b"


def test_an_unclosed_block_is_shown_whole_at_the_end() -> None:
    reply = 'cut short <spawn>{"name": "a"'

    text, shown = _streamed(list(reply))

    assert text == reply
    assert shown[-1] == '<spawn>{"name": "a"'


def test_a_closing_tag_split_over_two_chunks_ends_a_long_block() -> None:
    blocks = ActionBlockFilter()
    shown = [blocks.feed("<spawn>"), *(blocks.feed("x" * 10) for _ in range(5))]
    shown += [blocks.feed("</spa"), blocks.feed("wn>after"), blocks.flush()]

    assert "".join(shown) == "after"
