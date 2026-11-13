"""Document generation: the template, the profile's rules and the structure check."""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from harness.document_template import (
    CONFLICTS_HEADING,
    DOCUMENT_FILE,
    DOCUMENT_SECTIONS,
    DOCUMENT_TEMPLATE,
    MISSING_HEADING,
    TEMPLATE_PATH,
    document_problems,
)
from harness.profiles import DEFAULT_PROFILES, DOCUMENT_PROFILE

BACKEND_ROOT = Path(__file__).resolve().parents[2]

# The nine sections agreed in tasks/ss123_requirements.md, section 3.
AGREED_SECTIONS = (
    "Model overview",
    "Purpose and use",
    "Data and preprocessing",
    "Methodology",
    "Implementation and reproducibility",
    "Development testing",
    "Limitations and mitigants",
    "Monitoring and changes",
    "Evidence and review",
)


def filled_document(sections=AGREED_SECTIONS, extra: str = "") -> str:
    parts = ["# Model document: credit_logistic_r", "", "| Field | Value |", "| --- | --- |", ""]
    for number, title in enumerate(sections, 1):
        parts += [f"## {number}. {title}", "", f"Content [source: /inputs/model/x/manifest.json].", ""]
    parts += [
        CONFLICTS_HEADING,
        "",
        "| Old documentation says | Evidence shows | Source |",
        "| --- | --- | --- |",
        "| Model is not trained | model.rds exists with a checksum | /inputs/model/x/manifest.json |",
        "",
        MISSING_HEADING,
        "",
        "None found.",
        extra,
    ]
    return "\n".join(parts)


def test_template_has_the_agreed_sections_in_order():
    assert DOCUMENT_SECTIONS == AGREED_SECTIONS
    assert CONFLICTS_HEADING in DOCUMENT_TEMPLATE
    assert MISSING_HEADING in DOCUMENT_TEMPLATE


def test_template_is_plain_markdown_with_guides_to_replace():
    assert DOCUMENT_TEMPLATE.startswith("# Model document: MODEL_NAME")
    assert "```" not in DOCUMENT_TEMPLATE  # it is quoted into the prompt
    # The raw template is not a finished document: only its guide lines are left to fill.
    assert document_problems(DOCUMENT_TEMPLATE) == ["template guide lines are still present"]


def test_template_ships_with_the_package():
    assert TEMPLATE_PATH.is_file()
    config = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text())
    assert "templates/*.md" in config["tool"]["setuptools"]["package-data"]["harness"]


def test_document_profile_carries_template_and_rules():
    profile = DEFAULT_PROFILES["document"]
    assert profile is DOCUMENT_PROFILE
    assert profile.name == "document-v2"
    assert profile.effort == "low"
    assert profile.sandbox
    assert profile.tool_policy == (
        "sandbox_read_file", "sandbox_write_file", "sandbox_list_files", "sandbox_exec",
        "export_artifact",
    )
    prompt = profile.system_prompt
    assert prompt.endswith(DOCUMENT_TEMPLATE)
    # Old documentation is an input to check, not the answer.
    assert "old documentation" in prompt
    assert "(old documentation, not verified)" in prompt
    assert "conflicts table" in prompt
    # Markdown output, written and exported under one known name.
    assert "Markdown" in prompt and "no HTML" in prompt
    assert f"export_artifact with path {DOCUMENT_FILE}" in prompt
    assert profile.default_prompt


def test_template_change_changes_the_profile_hash():
    changed = DOCUMENT_PROFILE.model_copy(
        update={"system_prompt": DOCUMENT_PROFILE.system_prompt + "\nextra"}
    )
    assert changed.hash != DOCUMENT_PROFILE.hash


def test_a_complete_document_passes():
    assert document_problems(filled_document()) == []


def test_html_in_code_is_allowed():
    extra = "\n```\n<div>legacy output</div>\n```\nThe tag `<br>` appears in an old file."
    assert document_problems(filled_document(extra=extra)) == []


@pytest.mark.parametrize(
    ("document", "problem"),
    [
        ("", "the document is empty"),
        (filled_document().replace("# Model document", "Model document", 1), "level-1 title"),
        (filled_document(AGREED_SECTIONS[:-1]), "missing sections: 9. Evidence and review"),
        (filled_document((*AGREED_SECTIONS, "Appendix")), "unexpected sections: 10. Appendix"),
        (filled_document().replace(MISSING_HEADING, "### Gaps"), "missing table heading"),
        (filled_document(extra="> Guide: write this"), "guide lines"),
        (filled_document(extra="<b>bold</b>"), "contains HTML"),
    ],
)
def test_problems_are_reported(document, problem):
    problems = document_problems(document)
    assert any(problem in p for p in problems), problems


def test_sections_out_of_order_are_reported():
    swapped = filled_document().replace("## 1. Model overview", "## 1. TEMP")
    swapped = swapped.replace("## 2. Purpose and use", "## 1. Model overview")
    swapped = swapped.replace("## 1. TEMP", "## 2. Purpose and use")
    assert document_problems(swapped) == ["sections are out of order or repeated"]
