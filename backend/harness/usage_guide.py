"""The platform usage guide that chat answers from.

The guide is part of the chat profile's system prompt, so changing it changes the profile
hash recorded with every task. It covers how to use the platform, not its live state: chat
gets that from the backend's read-only tools.
"""
from __future__ import annotations

from pathlib import Path

USAGE_GUIDE_PATH = Path(__file__).parent / "guides" / "usage_guide.md"
USAGE_GUIDE = USAGE_GUIDE_PATH.read_text(encoding="utf-8")

# How the guide marks a detail that is not settled yet. Chat must not fill these in.
TO_BE_CONFIRMED = "*To be confirmed"
