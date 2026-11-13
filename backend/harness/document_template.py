"""The model document template and checks on a finished document.

The template is part of the document profile's system prompt, so changing it changes the
profile hash recorded with every task.
"""
from __future__ import annotations

import re
from pathlib import Path

TEMPLATE_PATH = Path(__file__).parent / "templates" / "model_document.md"
DOCUMENT_TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")

# The file the agent writes in its workspace and exports.
DOCUMENT_FILE = "model_document.md"

GUIDE_PREFIX = "> Guide:"
CONFLICTS_HEADING = "### Conflicts with the old documentation"
MISSING_HEADING = "### Missing information"

_SECTION = re.compile(r"^## (\d+)\. (.+?)\s*$", re.MULTILINE)
_HTML_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*(\s[^<>]*)?/?>")

DOCUMENT_SECTIONS: tuple[str, ...] = tuple(title for _, title in _SECTION.findall(DOCUMENT_TEMPLATE))


def document_problems(markdown: str) -> list[str]:
    """What makes a document not follow the template. An empty list means it does.

    This checks structure only. It cannot tell whether the content is true; that is the
    human review.
    """
    if not markdown.strip():
        return ["the document is empty"]
    problems: list[str] = []
    if not markdown.lstrip().startswith("# "):
        problems.append("the document does not start with a level-1 title")
    found = [(int(number), title) for number, title in _SECTION.findall(markdown)]
    expected = list(enumerate(DOCUMENT_SECTIONS, 1))
    if found != expected:
        missing = [f"{n}. {t}" for n, t in expected if (n, t) not in found]
        if missing:
            problems.append("missing sections: " + "; ".join(missing))
        extra = [f"{n}. {t}" for n, t in found if (n, t) not in expected]
        if extra:
            problems.append("unexpected sections: " + "; ".join(extra))
        if not missing and not extra:
            problems.append("sections are out of order or repeated")
    for heading in (CONFLICTS_HEADING, MISSING_HEADING):
        if not re.search(rf"^{re.escape(heading)}\s*$", markdown, re.MULTILINE):
            problems.append(f"missing table heading: {heading}")
    if any(line.lstrip().startswith(GUIDE_PREFIX) for line in markdown.splitlines()):
        problems.append("template guide lines are still present")
    if _HTML_TAG.search(_without_code(markdown)):
        problems.append("the document contains HTML")
    return problems


def _without_code(markdown: str) -> str:
    """HTML-like text inside code blocks or inline code is allowed."""
    markdown = re.sub(r"^```.*?^```", "", markdown, flags=re.MULTILINE | re.DOTALL)
    return re.sub(r"`[^`\n]*`", "", markdown)
