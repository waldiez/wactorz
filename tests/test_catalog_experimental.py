from wactorz.agents.catalog_agent import (
    BETA_WARNING,
    CatalogAgent,
    _chat_message_with_beta_warning,
    _wants_experimental,
)

EXPECTED_EXPERIMENTAL = {
    "code-agent",
    "news-agent",
    "qa-agent",
    "chron-agent",
    "wif-agent",
    "wiz-agent",
}

PRUNED_EXPERIMENTAL = {
    "ml-agent",
    "yolo-detector",
    "nautilus-agent",
    "udx-agent",
    "experimental-weather-agent",
}


def test_catalog_lists_curated_experimental_agents_with_warning():
    catalog = CatalogAgent(name="catalog-test")

    listed = catalog._action_list()
    by_name = {agent["name"]: agent for agent in listed["agents"]}

    assert listed["ok"] is True
    for name in EXPECTED_EXPERIMENTAL:
        assert by_name[name]["stability"] == "beta"
        assert by_name[name]["experimental"] is True
        assert by_name[name]["warning"] == BETA_WARNING

    for name in PRUNED_EXPERIMENTAL:
        assert name not in by_name


def test_action_list_hides_experimental_by_default():
    catalog = CatalogAgent(name="catalog-test")

    default = catalog._action_list()
    revealed = catalog._action_list(include_experimental=True)

    # The full set is always returned; only the display flag changes.
    assert default["show_experimental"] is False
    assert revealed["show_experimental"] is True
    assert {a["name"] for a in default["agents"]} == {a["name"] for a in revealed["agents"]}


def test_wants_experimental_detects_reveal_words():
    assert _wants_experimental("experimental") is True
    assert _wants_experimental("show me the beta ones") is True
    assert _wants_experimental("all") is True
    assert _wants_experimental("") is False
    assert _wants_experimental("just the normal agents") is False


def test_catalog_beta_info_hides_factory_and_mentions_beta():
    catalog = CatalogAgent(name="catalog-test")

    info = catalog._action_info("code-agent")

    assert info["ok"] is True
    assert "beta" in info["message"]
    assert info["recipe"]["stability"] == "beta"
    assert info["recipe"]["experimental"] is True
    assert info["recipe"]["warning"] == BETA_WARNING
    assert "factory" not in info["recipe"]
    assert "code" not in info["recipe"]


def _grouping_payload(show_experimental: bool) -> dict:
    return {
        "show_experimental": show_experimental,
        "agents": [
            {
                "name": "weather-agent",
                "description": "Weather lookup",
                "experimental": False,
            },
            {
                "name": "code-agent",
                "description": "Sandboxed code helper",
                "experimental": True,
                "warning": BETA_WARNING,
            },
        ],
    }


def test_catalog_response_hides_experimental_by_default():
    from wactorz.monitor_server import _format_catalog_agents_response

    text = _format_catalog_agents_response(_grouping_payload(show_experimental=False))

    assert "**Catalog agents**" in text
    assert "`2` total - `1` recommended, `1` experimental beta" in text
    assert "### Recommended" in text
    assert "- `weather-agent` - Weather lookup" in text
    # Beta agents are hidden behind a hint, not listed.
    assert "### Experimental / Beta" not in text
    assert "- `code-agent`" not in text
    assert "list experimental" in text


def test_catalog_response_shows_experimental_when_opted_in():
    from wactorz.monitor_server import _format_catalog_agents_response

    text = _format_catalog_agents_response(_grouping_payload(show_experimental=True))

    assert "### Recommended" in text
    assert "### Experimental / Beta" in text
    assert "- `code-agent` - Sandboxed code helper" in text
    assert BETA_WARNING not in text
    assert text.index("### Recommended") < text.index("### Experimental / Beta")
    assert "list experimental" not in text


def test_catalog_spawn_message_includes_beta_warning_in_chat():
    text = _chat_message_with_beta_warning("'code-agent' spawned and running", BETA_WARNING)

    assert text == f"'code-agent' spawned and running\n\nWarning: {BETA_WARNING}"


def test_catalog_spawn_message_leaves_recommended_agents_plain():
    text = _chat_message_with_beta_warning("'weather-agent' spawned and running", "")

    assert text == "'weather-agent' spawned and running"


def test_experimental_first_use_banner_warns_once(monkeypatch):
    import wactorz.monitor_server as ms

    class _FakeMain:
        def __init__(self):
            self._agent_manifests = {
                "code-agent": {"experimental": True, "warning": BETA_WARNING},
                "weather-agent": {"experimental": False},
            }

    monkeypatch.setattr(ms, "_find_main", lambda: _FakeMain())
    ms._beta_warned_agents.clear()

    first = ms._experimental_first_use_banner("code-agent")
    second = ms._experimental_first_use_banner("code-agent")
    plain = ms._experimental_first_use_banner("weather-agent")
    unknown = ms._experimental_first_use_banner("no-such-agent")

    assert first is not None
    assert "experimental/beta" in first and BETA_WARNING in first
    assert second is None  # only warns on the first message, not every turn
    assert plain is None  # non-experimental agent
    assert unknown is None  # unknown agent
