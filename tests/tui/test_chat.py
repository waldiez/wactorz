"""The chat tab: threads, what the box offers, and picking among agents."""

from typing import Any, cast

import pytest
from rich.markdown import Markdown
from textual.pilot import Pilot
from textual.widgets import Input, OptionList, TabbedContent

from tests.tui.fake import FakeClient, context_over, part
from wactorz.client import Client, Frame, Part, UnavailableError
from wactorz.tui.app import WactorzTUI
from wactorz.tui.chatlog import ALL, ASSISTANT, USER, ChatLog, Message, message_from_row
from wactorz.tui.completion import Offers
from wactorz.tui.context import TUIContext
from wactorz.tui.panes import ChatPane, home
from wactorz.tui.panes import chat as chat_mod
from wactorz.tui.snapshot import Snapshot


def _reply(thread: str, text: str = "hi", ts: float = 0.0) -> Message:
    return Message(thread, ASSISTANT, thread, text, ts)


def _with_agents(client: FakeClient | None = None) -> TUIContext:
    context = context_over(client)
    context.state.apply(
        Frame(
            "full_snapshot",
            {
                "state": {
                    "agents": [
                        {"agent_id": "m", "name": "main"},
                        {"agent_id": "w1", "name": "w", "node": "edge"},
                        {"agent_id": "w2", "name": "w", "node": "shed"},
                    ]
                }
            },
        )
    )
    return context


async def _say(pilot: Pilot[Any], text: str) -> None:
    box = pilot.app.query_one("#cmd", Input)
    box.focus()
    box.value = text
    await pilot.press("enter")
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


class TestTheLog:
    def test_all_shows_every_thread_and_a_thread_only_itself(self) -> None:
        log = ChatLog()
        log.add(_reply("a"))
        log.add(_reply("b"))

        assert len(log.visible()) == 2
        log.select("a")
        assert [m.thread for m in log.visible()] == ["a"]

    def test_what_is_said_elsewhere_is_unread_until_read(self) -> None:
        log = ChatLog()
        log.select("a")
        log.add(_reply("b"))
        assert log.unread == {"b": 1}

        log.select("b")
        assert log.unread == {}

    def test_nothing_is_unread_in_the_view_of_everything(self) -> None:
        log = ChatLog()
        log.add(_reply("b"))

        assert log.unread == {}

    def test_restoring_keeps_time_order_and_repeats_nothing(self) -> None:
        log = ChatLog()
        log.add(_reply("a", "now", ts=5.0))

        log.restore([_reply("a", "earlier", ts=1.0), _reply("a", "now", ts=5.0)])

        assert [m.text for m in log.messages] == ["earlier", "now"]

    def test_a_row_is_read_whatever_is_missing_from_it(self) -> None:
        message = message_from_row({"role": "user", "attachments": "nonsense"})

        assert (message.role, message.files, message.speaker) == (USER, [], "main")


class TestOffers:
    @pytest.fixture(name="offers")
    def offers_fixture(self) -> Offers:
        found = Offers()
        found.commands = ["/agents", "/plans approve"]
        found.agents = ["catalog", "w@edge"]
        found.typed = ["hello there", "help me"]
        return found

    @pytest.mark.parametrize(
        ("typed", "offered"),
        [
            ("/pl", "/plans approve"),
            ("@w", "@w@edge "),
            ("hel", "help me"),  # the newest of what was typed
            ("", None),
            ("/nothing", None),
            ("/agents", None),  # already all there
        ],
    )
    async def test_what_is_offered(self, offers: Offers, typed: str, offered: str | None) -> None:
        assert await offers.get_suggestion(typed) == offered


class TestThreads:
    def test_a_shared_name_is_told_apart_by_node(self) -> None:
        labels = [label for _, label in _with_agents().threads()]

        assert labels == ["main", "w@edge", "w@shed"]

    def test_a_thread_with_no_running_agent_keeps_its_name(self) -> None:
        context = _with_agents()
        context.log.add(Message("gone", ASSISTANT, "old-agent", "bye"))

        assert context.label_of("gone") == "old-agent"
        assert context.thread_named("old-agent") == "gone"

    async def test_the_list_has_all_first_then_each_agent(self) -> None:
        async with WactorzTUI(_with_agents()).run_test() as pilot:
            await pilot.pause(1.2)
            threads = pilot.app.query_one("#chat-threads", OptionList)
            prompts = [
                str(threads.get_option_at_index(i).prompt) for i in range(threads.option_count)
            ]
            assert prompts == ["All", "main", "w@edge", "w@shed"]

    async def test_a_message_goes_to_the_thread_being_read(self) -> None:
        client = FakeClient(parts=(part("ok"),))
        async with WactorzTUI(_with_agents(client)).run_test() as pilot:
            await _say(pilot, "/thread w@edge")
            await _say(pilot, "status?")
            assert client.chat.addressed == ["w@edge"]

    async def test_a_mention_says_for_itself(self) -> None:
        client = FakeClient(parts=(part("ok"),))
        async with WactorzTUI(_with_agents(client)).run_test() as pilot:
            await _say(pilot, "/thread w@edge")
            await _say(pilot, "@main hi")
            assert client.chat.addressed == [None]

    async def test_the_thread_keys_step_through_and_wrap(self) -> None:
        context = _with_agents()
        async with WactorzTUI(context).run_test() as pilot:
            await pilot.press("ctrl+n")
            assert context.log.selected == "m"
            await pilot.press("ctrl+b", "ctrl+b")
            assert context.log.selected == "w2"

    async def test_a_message_moves_to_the_thread_it_reached(self) -> None:
        reached = Part(text="ok", agent="main", agent_id="m", streaming=False)
        context = _with_agents(FakeClient(parts=(reached,)))
        async with WactorzTUI(context).run_test() as pilot:
            await _say(pilot, "/thread w@edge")
            await _say(pilot, "@main hi")
            assert context.log.selected == "m"
            assert {m.thread for m in context.log.messages} == {"m"}

    async def test_an_unknown_thread_says_so_and_stays_put(self) -> None:
        context = _with_agents()
        async with WactorzTUI(context).run_test() as pilot:
            await _say(pilot, "/thread nobody")
            assert context.log.selected == ALL
            assert any("No thread" in n.message for n in pilot.app._notifications)

    async def test_choosing_from_the_list_opens_the_thread(self) -> None:
        context = _with_agents()
        async with WactorzTUI(context).run_test() as pilot:
            await pilot.pause(1.2)
            pilot.app.query_one("#chat-threads", OptionList).action_cursor_down()
            pilot.app.query_one("#chat-threads", OptionList).action_select()
            await pilot.pause()
            assert context.log.selected == "m"


class TestReplies:
    async def test_a_reply_streams_into_one_message_and_finishes_as_markdown(self) -> None:
        client = FakeClient(parts=(part("Hel"), part("lo")))
        async with WactorzTUI(context_over(client)).run_test() as pilot:
            await _say(pilot, "hi")
            turn = pilot.app.query_one(ChatPane).turns()[-1]
            assert turn.message.text == "Hello"
            assert not turn.message.streaming
            assert any(
                isinstance(p, Markdown) for p in chat_mod._finished(turn.message).renderables
            )

    async def test_two_whole_replies_are_two_messages(self) -> None:
        whole = (
            Part(text="one", agent="main", agent_id="", streaming=False),
            Part(text="two", agent="main", agent_id="", streaming=False),
        )
        async with WactorzTUI(context_over(FakeClient(parts=whole))).run_test() as pilot:
            await _say(pilot, "hi")
            shown = [turn.message.text for turn in pilot.app.query_one(ChatPane).turns()]
            assert shown[-2:] == ["one", "two"]

    async def test_no_answer_says_so(self) -> None:
        async with WactorzTUI(context_over()).run_test() as pilot:
            await _say(pilot, "hi")
            assert pilot.app.query_one(ChatPane).turns()[-1].message.text == "(no response)"

    async def test_brackets_in_a_reply_are_text(self) -> None:
        client = FakeClient(parts=(part("[bold]not markup[/]"),))
        async with WactorzTUI(context_over(client)).run_test() as pilot:
            await _say(pilot, "hi")
            assert "[bold]not markup[/]" in pilot.app.query_one(ChatPane).turns()[-1].shown

    async def test_a_message_from_elsewhere_is_drawn_only_in_its_thread(self) -> None:
        context = _with_agents()
        async with WactorzTUI(context).run_test() as pilot:
            pane = pilot.app.query_one(ChatPane)
            await _say(pilot, "/thread main")
            pane.said(_reply("w1", "in another thread"))
            await pilot.pause()
            assert [turn.message.text for turn in pane.turns()] == []
            assert context.log.unread == {"w1": 1}


class TestPicking:
    CHOICES = Part(
        text="which?", agent="main", agent_id="", streaming=False, choices=("w@edge", "w@shed")
    )

    async def test_a_pick_sends_it_again_to_that_agent(self) -> None:
        client = FakeClient(parts=(self.CHOICES,))
        async with WactorzTUI(context_over(client)).run_test() as pilot:
            await _say(pilot, "@w status")
            await _say(pilot, "/pick 2")
            assert client.chat.asked == ["@w status", "@w@shed status"]

    async def test_a_pick_with_nothing_offered_says_so(self) -> None:
        client = FakeClient()
        async with WactorzTUI(context_over(client)).run_test() as pilot:
            await _say(pilot, "/pick 1")
            assert client.chat.asked == []
            assert any("Nothing to pick" in n.message for n in pilot.app._notifications)

    def test_the_choices_are_drawn_with_how_to_pick(self) -> None:
        message = Message("", ASSISTANT, "main", "which?", choices=("w@edge",))
        drawn = chat_mod._finished(message).renderables

        assert "/pick 1" in str(drawn[-1])


class TestTheBox:
    async def test_up_and_down_walk_what_was_typed_and_return_to_the_draft(self) -> None:
        async with WactorzTUI(context_over()).run_test() as pilot:
            await _say(pilot, "first")
            await _say(pilot, "second")
            box = pilot.app.query_one("#cmd", Input)
            box.value = "draft"
            await pilot.press("up")
            assert box.value == "second"
            await pilot.press("up", "up")
            assert box.value == "first"
            await pilot.press("down", "down")
            assert box.value == "draft"

    async def test_the_servers_commands_are_offered(self) -> None:
        app = WactorzTUI(context_over())
        async with app.run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            assert app._offers.commands == ["/agents", "/plans approve"]


class TestCommands:
    async def test_tab_goes_to_a_tab_and_a_wrong_name_lists_them(self) -> None:
        async with WactorzTUI(context_over()).run_test() as pilot:
            await _say(pilot, "/tab agents")
            assert pilot.app.query_one(TabbedContent).active == "agents"
            await _say(pilot, "/tab nowhere")
            assert any("Tabs:" in n.message for n in pilot.app._notifications)

    async def test_stop_says_it_reached_every_client(self) -> None:
        async with WactorzTUI(context_over()).run_test() as pilot:
            await _say(pilot, "/stop")
            assert any("every client" in n.message for n in pilot.app._notifications)

    async def test_a_stop_that_fails_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        context = context_over()

        async def refused() -> int:
            raise UnavailableError("http://server.test", "gone")

        monkeypatch.setattr(context, "stop_all", refused)
        async with WactorzTUI(context).run_test() as pilot:
            await pilot.press("f8")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert any("Could not stop" in n.message for n in pilot.app._notifications)


def test_home_says_what_closing_does() -> None:
    attached = context_over()
    hosted = TUIContext(cast(Client, FakeClient()), hosted=True)

    assert "keep running" in "\n".join(home._identity_lines(Snapshot(), attached))
    assert "stops the agents" in "\n".join(home._identity_lines(Snapshot(), hosted))
