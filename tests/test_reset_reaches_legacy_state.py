"""A wipe has to reach the legacy pickle copy, in memory and on disk.

`Actor._load_persistent_state` loads a legacy `state.pkl` into
`_persistent_state` **even on the new store path** — "will migrate on first
persist" — and `Actor.recall` falls back to that dict whenever the new store
returns `None`. Nothing ever removed it, so a key that exists in a legacy pickle
survived a factory reset for as long as the process lived: the wipe emptied the
database, `recall` then found nothing there, fell through, and handed back the
value the user had just asked to destroy.

Found from the other end. A test constructing a `MainActor` without a
`persistence_dir` wrote its scripted conversation into the developer's real
`state/main/state.pkl`, and it then showed up in the running dashboard as a chat
nobody had had — surviving every wipe.

Note `None` and `[]` are not the same to that fallback, which is why a *chat*
reset appeared to work: it leaves an empty list in the store, and an empty list
is a value, so the fallback is never reached. A full wipe deletes the row
outright, so it is.
"""

from pathlib import Path
from typing import Any

from wactorz import reset as _reset
from wactorz.core.actor import Actor
from wactorz.web import api_reset

CHAT = "conversation_history"
TURNS = [{"role": "user", "content": "turn on the living room light"}]


class _Actor(Actor):
    """The real Actor, minus the one abstract method a reset never calls."""

    async def handle_message(self, message: Any) -> Any:  # pragma: no cover - never invoked
        return None


def _actor_with_legacy_state(tmp_path: Path, **state: Any) -> Actor:
    """An actor whose new store knows nothing and whose legacy dict knows all.

    The state a wipe leaves behind: the pickle file is gone from disk, its
    contents are still in the process.
    """
    actor = _Actor.__new__(_Actor)
    actor.name = "main"
    actor._persistence_dir = tmp_path
    actor._persistent_state = dict(state)
    actor._persistence_api = _StoreThatKnowsNothing()
    return actor


class _StoreThatKnowsNothing:
    """A wiped store: every key returns None, which is what triggers the fallback."""

    def get(self, _key: str, default: Any = None) -> Any:
        return None

    def set(self, _key: str, _value: Any) -> None:
        return None


class TestAFactoryResetClearsTheLegacyCopy:
    def test_recall_no_longer_returns_the_wiped_conversation(self, tmp_path: Path) -> None:
        actor = _actor_with_legacy_state(tmp_path, conversation_history=TURNS)
        assert actor.recall(CHAT) == TURNS  # the state before the wipe

        api_reset.forget_legacy_state(actor)

        assert actor.recall(CHAT, []) == []

    def test_it_clears_every_key_not_only_the_chat_ones(self, tmp_path: Path) -> None:
        # A factory reset deletes the whole pickle from disk, so leaving any of
        # it in memory would put the two back out of step — and the next legacy
        # persist writes the file back from that dict.
        actor = _actor_with_legacy_state(tmp_path, conversation_history=TURNS, _user_facts={"a": 1})

        api_reset.forget_legacy_state(actor)

        assert not actor._persistent_state

    def test_an_actor_without_the_legacy_dict_is_left_alone(self, tmp_path: Path) -> None:
        # Belt and braces: the reset loops over every kept actor, including ones
        # that never had a pickle.
        actor = _actor_with_legacy_state(tmp_path)
        del actor._persistent_state

        api_reset.forget_legacy_state(actor)  # must not raise


class TestAChatResetClearsOnlyTheChatKeys:
    def test_it_drops_the_conversation(self, tmp_path: Path) -> None:
        actor = _actor_with_legacy_state(tmp_path, conversation_history=TURNS)

        api_reset.forget_legacy_state(actor, api_reset.CHAT_STATE_KEYS)

        assert actor.recall(CHAT, []) == []

    def test_it_keeps_everything_else(self, tmp_path: Path) -> None:
        # A chat reset is not a factory reset: it clears the conversation and
        # nothing else, so anything else in the legacy dict has to survive.
        actor = _actor_with_legacy_state(tmp_path, conversation_history=TURNS, _user_facts={"a": 1})

        api_reset.forget_legacy_state(actor, api_reset.CHAT_STATE_KEYS)

        assert actor._persistent_state == {"_user_facts": {"a": 1}}


class TestTheChatResetReachesTheFileToo:
    """Clearing the database alone let a restart bring the conversation back.

    `Actor` loads a legacy `state.pkl` at start, so a conversation left in one
    is read again on the next boot however thoroughly the database was cleared.
    """

    def test_the_conversation_is_removed_from_the_legacy_file(self, tmp_path: Path) -> None:
        store = _seeded_store(tmp_path, "main", {"conversation_history": TURNS})

        _reset._strip_chat_from_pickles(None, str(tmp_path))

        assert CHAT not in store.load("main")

    def test_everything_else_in_the_file_survives(self, tmp_path: Path) -> None:
        store = _seeded_store(
            tmp_path, "main", {"conversation_history": TURNS, "_user_facts": {"a": 1}}
        )

        _reset._strip_chat_from_pickles(None, str(tmp_path))

        assert store.load("main") == {"_user_facts": {"a": 1}}

    def test_it_finds_an_agent_with_a_file_and_no_database_rows(self, tmp_path: Path) -> None:
        # The case this was found from: the file was written directly, so a loop
        # driven off agents with kv rows would never have visited it.
        store = _seeded_store(tmp_path, "never-in-the-db", {"conversation_history": TURNS})

        _reset._strip_chat_from_pickles(None, str(tmp_path))

        assert CHAT not in store.load("never-in-the-db")

    def test_naming_one_agent_leaves_the_others_alone(self, tmp_path: Path) -> None:
        store = _seeded_store(tmp_path, "main", {"conversation_history": TURNS})
        _seeded_store(tmp_path, "other", {"conversation_history": TURNS})

        _reset._strip_chat_from_pickles("main", str(tmp_path))

        assert CHAT not in store.load("main")
        assert store.load("other")[CHAT] == TURNS

    def test_an_unreadable_file_is_not_replaced_with_an_empty_one(self, tmp_path: Path) -> None:
        # PickleStore.load quarantines it and returns {}, which holds none of
        # these keys — so it is skipped rather than overwritten. Writing the
        # empty dict back would destroy whatever the quarantined copy holds.
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "state.pkl").write_bytes(b"not a pickle at all")

        _reset._strip_chat_from_pickles(None, str(tmp_path))

        assert not (broken / "state.pkl").exists()  # quarantined by load, not rewritten
        assert list(broken.glob("state.pkl.*"))


def _seeded_store(tmp_path: Path, agent: str, state: dict[str, Any]) -> Any:
    from wactorz.core.persistence import PickleStore

    store = PickleStore(str(tmp_path))
    store.save(agent, state)
    return store
