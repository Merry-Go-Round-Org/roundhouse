"""The message that resumes a task after a person decides on a card."""

from harness.schemas import Resolution
from harness.store import resolution_text


def test_resolution_says_how_the_decision_was_made_not_that_it_happened_elsewhere():
    # Flow C retest: after Approve operation on a card in the same chat, the assistant
    # said the action "was approved outside this chat".
    text = resolution_text(Resolution(pending_id="a1", outcome="approved", detail="Looks right."))
    assert "outside" not in text
    assert text.startswith("Pending action a1 was approved by a person on its decision card.")
    assert text.endswith("Looks right.")
