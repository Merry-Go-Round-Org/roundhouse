"""The validator agent's narrative, and the check that it states no number of its own.

The scoring service produces every official number in a metrics bundle. The validator agent may
quote those numbers but never make one up, so `narrative_problems` rejects a draft with a number
that has no match in the bundle. The backend runs it before storing the draft, as it runs
`document_problems` for model documents.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Iterable

from harness.outputs import GUIDE_PREFIX, _sections_problems

TEMPLATE_PATH = Path(__file__).parent / "templates" / "validation_narrative.md"
NARRATIVE_TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")
NARRATIVE_FILE = "validation_narrative.md"
METRICS_BUNDLE_FILE = "metrics_bundle.json"

RECOMMENDATIONS: tuple[str, ...] = ("approve", "approve with conditions", "reject")
_SECTION = re.compile(r"^## (\d+)\. (.+?)\s*$", re.MULTILINE)
NARRATIVE_SECTIONS: tuple[tuple[int, str], ...] = tuple(
    (int(n), t) for n, t in _SECTION.findall(NARRATIVE_TEMPLATE)
)
_RECOMMENDATION = re.compile(r"^Recommendation:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)

# References that are numbers but not claims, such as "section 4" or "Principle 4.1(d)".
_REFERENCE = re.compile(
    r"\b(?:sections?|principles?|paragraphs?|gates?|layers?|steps?|tables?|class(?:es)?|ss1/23|sr)"
    r"\s+\d+(?:\.\d+)*(?:\([a-z]\))?"
    r"(?:\s*(?:[-–,]|and|or|to)\s*\d+(?:\.\d+)*(?:\([a-z]\))?)*",
    re.IGNORECASE,
)
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_CODE_BLOCK = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_SOURCE = re.compile(r"\[source:[^\]]*\]", re.IGNORECASE)
_NUMBERED_HEADING = re.compile(r"^#+\s+\d+\.\s", re.MULTILINE)
_LIST_NUMBER = re.compile(r"^\s*\d+\.\s", re.MULTILINE)
# An enumeration marker inside a sentence, such as "(1) linearity, (2) independence".
# Only where a list item can start: a line start, after ":", ";" or ",", or after "and"/"or".
_INLINE_ENUMERATION = re.compile(
    r"(?:^[ \t]*|(?<=[:;,] )|(?<=\band )|(?<=\bor ))\(\d{1,2}\)(?=\s+[A-Za-z])",
    re.MULTILINE,
)
# A number standing alone: not part of an identifier like `ks_1`, `v2`, `DISC-01` or `SS1/23`.
_NUMBER = re.compile(r"(?<![\w./-])[-−]?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?(?![\w/])|(?<![\w./-])[-−]?\d+(?:\.\d+)?%?(?![\w/])")


def narrative_problems(markdown: str, metrics_bundle: Any) -> list[str]:
    """What stops the narrative from going into the report. Empty means it may go."""
    problems = _sections_problems(
        markdown,
        tuple(t for _, t in NARRATIVE_SECTIONS),
        numbered=True,
        numbers=tuple(n for n, _ in NARRATIVE_SECTIONS),
    )
    if "MODEL_NAME" in markdown:
        problems.append("the title still has the MODEL_NAME placeholder")
    match = _RECOMMENDATION.search(markdown)
    if match is None:
        problems.append("missing the Recommendation line")
    elif match.group(1).strip().lower().rstrip(".") not in RECOMMENDATIONS:
        problems.append("the recommendation must be one of: " + ", ".join(RECOMMENDATIONS))
    missing = unquoted_limitations(markdown, metrics_bundle)
    if missing:
        problems.append(
            "these required checks are REVIEW or NOT_ASSESSABLE, and their note must be quoted "
            "word for word so the narrative keeps what was verified and what was not: "
            + "; ".join(f'{check_id}: "{note}"' for check_id, note in missing)
        )
    # A quoted note is the platform's own wording, so its numbers are not the writer's claims.
    located = unsupported_number_locations(
        _blank_notes(markdown, limitation_notes(metrics_bundle)), metrics_bundle
    )
    if located:
        shown = "; ".join(
            f'{token} (line {line}: "{snippet}")' for token, line, snippet in located[:10]
        )
        more = f"; and {len(located) - 10} more" if len(located) > 10 else ""
        problems.append(
            f"numbers not in the metrics bundle: {shown}{more}. Quote the bundle value, write "
            "a count or list marker in words, or put an identifier in backticks"
        )
    return problems


def limitation_notes(metrics_bundle: Any) -> list[tuple[str, str]]:
    """(check_id, note) for each required check that is REVIEW or NOT_ASSESSABLE.

    These are the limitations a person accepts with conditions at Gate 3, so the narrative must
    carry their exact meaning, such as "Uploaded split is verified; original fit membership
    remains unverified" rather than "the split could not be confirmed".
    """
    if hasattr(metrics_bundle, "model_dump"):
        metrics_bundle = metrics_bundle.model_dump(mode="json")
    checks = metrics_bundle.get("checks") if isinstance(metrics_bundle, dict) else None
    found = []
    for check in checks or []:
        note = (check.get("note") or "").strip()
        if (
            note
            and check.get("status") in ("REVIEW", "NOT_ASSESSABLE")
            and (check.get("settings") or {}).get("required") is True
        ):
            found.append((check["check_id"], note))
    return found


def _normalised(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[`*_\"“”]", "", text)).strip().lower().rstrip(".")


def unquoted_limitations(markdown: str, metrics_bundle: Any) -> list[tuple[str, str]]:
    """The limitation notes the narrative does not quote word for word (case and emphasis aside)."""
    text = _normalised(markdown)
    return [(c, n) for c, n in limitation_notes(metrics_bundle) if _normalised(n) not in text]


def _blank_notes(markdown: str, notes: list[tuple[str, str]]) -> str:
    for _, note in notes:
        pattern = re.compile(r"\s+".join(re.escape(w) for w in note.rstrip(".").split()), re.IGNORECASE)
        markdown = pattern.sub(_blank, markdown)
    return markdown


def unsupported_numbers(text: str, metrics_bundle: Any) -> list[str]:
    """Numbers in `text` that match no number in the bundle, in order, without repeats.

    A decimal or percentage matches a bundle value that rounds to it at the precision written,
    and a percentage also matches a proportion: 72.1% matches 0.7213. A whole number without a
    percent sign must equal a bundle value exactly, so "1" never passes for an AUC of 0.6. A
    number written without its sign matches the bundle's negative value.
    """
    allowed = list(_bundle_numbers(metrics_bundle))
    seen: list[str] = []
    for token in claimed_numbers(text):
        if token not in seen and not any(_matches(token, v) for v in allowed):
            seen.append(token)
    return seen


def unsupported_number_locations(text: str, metrics_bundle: Any) -> list[tuple[str, int, str]]:
    """Each unsupported number with its line and the text around it, in order.

    The writer needs the place, not only the digit: a bare "1" can come from many sentences.
    """
    allowed = list(_bundle_numbers(metrics_bundle))
    found: list[tuple[str, int, str]] = []
    for match in _claims(text):
        token = match.group(0)
        if any(_matches(token, v) for v in allowed):
            continue
        line = text.count("\n", 0, match.start()) + 1
        start = max(text.rfind("\n", 0, match.start()) + 1, match.start() - 40)
        end = text.find("\n", match.end())
        end = min(len(text) if end == -1 else end, match.end() + 40)
        snippet = text[start:end].strip()
        found.append((token, line, snippet))
    return found


def claimed_numbers(text: str) -> list[str]:
    """The number tokens in `text` that count as claims."""
    return [m.group(0) for m in _claims(text)]


def _claims(text: str) -> list[re.Match[str]]:
    # Blank out what is not a claim without moving anything, so offsets stay those of `text`.
    masked = text
    for pattern in (
        _CODE_BLOCK,
        _INLINE_CODE,
        _SOURCE,
        _NUMBERED_HEADING,
        _LIST_NUMBER,
        _INLINE_ENUMERATION,
        _DATE,
        _REFERENCE,
    ):
        masked = pattern.sub(_blank, masked)
    return list(_NUMBER.finditer(masked))


def _blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def _bundle_numbers(value: Any) -> Iterable[float]:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        if math.isfinite(value):
            yield float(value)
        return
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        for item in value.values():
            yield from _bundle_numbers(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _bundle_numbers(item)


def _matches(token: str, value: float) -> bool:
    percent = token.endswith("%")
    raw = token.rstrip("%").replace(",", "").replace("−", "-")
    number = float(raw)
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    candidates = (value * 100, value) if percent else (value,)
    for candidate in candidates:
        for target in (number, -number):
            if decimals == 0 and not percent:
                if candidate == target:
                    return True
            elif abs(candidate - target) <= 0.5 * 10**-decimals + 1e-9:
                return True
    return False


__all__ = [
    "GUIDE_PREFIX",
    "METRICS_BUNDLE_FILE",
    "NARRATIVE_FILE",
    "NARRATIVE_SECTIONS",
    "NARRATIVE_TEMPLATE",
    "RECOMMENDATIONS",
    "claimed_numbers",
    "narrative_problems",
    "unsupported_number_locations",
    "unsupported_numbers",
]
