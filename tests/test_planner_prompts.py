"""The planner's prompt templates and the fields they require.

These render at runtime inside code the suite does not otherwise reach, so a
renamed or dropped field would surface as a KeyError in production rather than
here. Each test pins the exact field set.
"""

import string

import pytest

from wactorz.agents.prompts.planner_prompts import (
    DECOMPOSE_PROMPT,
    HA_FEASIBILITY_PROMPT,
    PIPELINE_DESIGN_PROMPT,
    RULE_CONFLICT_PROMPT,
)

#: Template name -> the fields its call site in the planner passes.
TEMPLATES = {
    "DECOMPOSE_PROMPT": (DECOMPOSE_PROMPT, {"workers_desc", "topic_schema_ctx", "task"}),
    "HA_FEASIBILITY_PROMPT": (HA_FEASIBILITY_PROMPT, {"task", "ha_section"}),
    "RULE_CONFLICT_PROMPT": (RULE_CONFLICT_PROMPT, {"task"}),
}


@pytest.mark.parametrize("name", TEMPLATES)
def test_a_template_declares_exactly_the_fields_its_call_site_passes(name: str) -> None:
    template, fields = TEMPLATES[name]
    present = {f for _, f, _, _ in string.Formatter().parse(template) if f}

    assert present == fields, name


@pytest.mark.parametrize("name", TEMPLATES)
def test_a_template_renders_with_those_fields(name: str) -> None:
    template, fields = TEMPLATES[name]
    rendered = template.format(**dict.fromkeys(fields, "SENTINEL"))

    assert rendered.count("SENTINEL") >= len(fields), name
    assert len(rendered) > 200, name


@pytest.mark.parametrize("name", TEMPLATES)
def test_a_missing_field_is_loud(name: str) -> None:
    """A silently absent field would ship an unfilled placeholder to the model."""
    template, _ = TEMPLATES[name]
    with pytest.raises(KeyError):
        template.format()


def test_the_pipeline_design_prompt_is_raw_text_not_a_format_template() -> None:
    """It carries JSON examples, so its braces are content and not fields.

    The caller concatenates it; nothing may run .format() over it. Doing so
    would raise on the example keys, or quietly rewrite the JSON the model is
    being shown. If this ever needs fields, every literal brace has to be
    doubled first.
    """
    with pytest.raises((KeyError, IndexError, ValueError)):
        PIPELINE_DESIGN_PROMPT.format()


def test_the_pipeline_design_prompt_still_forbids_code_fences() -> None:
    """The reply is parsed with extract_json_array, which fenced output defeats."""
    assert "no code fences" in PIPELINE_DESIGN_PROMPT


def test_the_decompose_prompt_still_demands_a_json_array() -> None:
    """The caller parses the reply with extract_json_array, so this has to hold."""
    assert "JSON array" in DECOMPOSE_PROMPT
