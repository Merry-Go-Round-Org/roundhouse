"""The usage guide chat answers from, and how the chat profile carries it."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from harness.document_template import DOCUMENT_SECTIONS
from harness.profiles import CHAT_PROFILE, DEFAULT_PROFILES
from harness.usage_guide import USAGE_GUIDE, USAGE_GUIDE_PATH

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_guide_ships_with_the_package():
    assert USAGE_GUIDE_PATH.is_file()
    config = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text())
    assert "guides/*.md" in config["tool"]["setuptools"]["package-data"]["harness"]


def test_guide_covers_every_lifecycle_step():
    headings = re.findall(r"^## (.+)$", USAGE_GUIDE, re.MULTILINE)
    assert headings == [
        "Roles and signing in",
        "The lifecycle at a glance",
        "1. Datasets",
        "2. Uploading a model",
        "3. The inventory",
        "4. Model documents",
        "5. Rebuild and parity",
        "6. Validation reports",
        "7. Reviews",
        "8. Monitoring",
        "9. The assistant",
    ]


def test_guide_matches_the_document_template():
    # The guide lists the template's sections, so the two must not drift apart.
    for title in DOCUMENT_SECTIONS:
        assert title.lower() in USAGE_GUIDE.lower(), title


def test_guide_states_the_fixed_rules():
    for rule in (
        "Agents only ever get the development data",
        "Nothing typed in chat counts as approval",
        "cannot be the person who started the run",
        "never a pass",
        "Not provided in upload",
        "agent self-check",
    ):
        assert rule in USAGE_GUIDE, rule


def test_unsettled_details_are_marked_not_guessed():
    # Upload file formats are still unsettled.
    assert "Exact upload file format rules and upload error messages: *To be confirmed.*" in USAGE_GUIDE
    # Gate roles and the assistant's operations are settled and written out.
    assert "Which role may decide at which review gate" not in USAGE_GUIDE
    assert "The list of operations the assistant can perform" not in USAGE_GUIDE
    assert "| 3 (validation report) | An approver or administrator" in USAGE_GUIDE
    assert "Approving does not start anything" in USAGE_GUIDE


def test_guide_describes_official_parity_as_one_check():
    assert "The agent and a person run the same check" in USAGE_GUIDE
    assert "at most 1e-8" in USAGE_GUIDE
    assert "parity button is not available yet" not in USAGE_GUIDE


def test_chat_profile_carries_the_guide_and_its_rules():
    profile = DEFAULT_PROFILES["chat"]
    assert profile is CHAT_PROFILE
    assert profile.name == "chat-v2"
    assert profile.effort == "low"
    assert not profile.sandbox
    prompt = profile.system_prompt
    assert prompt.endswith(USAGE_GUIDE)
    assert "to be confirmed" in prompt
    assert "never invent" in prompt
    assert "call the tools first" in prompt
    assert "get_datasets" in prompt
    assert "applies only to the exact topic" in prompt
