from wactorz.agents.catalog_agent import BETA_WARNING, CatalogAgent


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


def test_catalog_agent_response_groups_recommended_and_experimental():
    from wactorz.monitor_server import _format_catalog_agents_response

    payload = {
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
        ]
    }

    text = _format_catalog_agents_response(payload)

    assert "**Catalog agents**" in text
    assert "`2` total - `1` recommended, `1` experimental beta" in text
    assert "### Recommended" in text
    assert "Use these first for normal workflows." in text
    assert "- `weather-agent` - Weather lookup" in text
    assert "### Experimental / Beta" in text
    assert BETA_WARNING in text
    assert "- `code-agent` - Sandboxed code helper" in text
    assert text.index("### Recommended") < text.index("### Experimental / Beta")
    assert text.count(BETA_WARNING) == 1
