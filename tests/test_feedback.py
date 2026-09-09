"""Tests for the reviewer-feedback revision round.

The failure modes worth pinning are both silent: a report the agent posted
being mistaken for human feedback (which makes the ticket work itself forever),
and the same comment starting a second container on the next poll tick.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bloy_dev_agent import store
from bloy_dev_agent.features import feedback
from bloy_dev_agent.features.twenty import mapping
from bloy_dev_agent.models import BloyFeedbackRound, BloyPipelineRun, BloySetting


@pytest.fixture()
def bloy_db(monkeypatch, tmp_path):
    """An isolated SQLite database bound to this plugin's tables only."""
    engine = create_engine(f"sqlite:///{tmp_path / 'feedback.sqlite3'}")
    for model in (BloyFeedbackRound, BloyPipelineRun, BloySetting):
        model.__table__.create(engine, checkfirst=True)
    monkeypatch.setattr(store, "SessionLocal", sessionmaker(bind=engine))
    return engine


def _comment(body: str, *, author: str = "Cuong", cid: str = "c1", at: str = "2026-09-01T10:00:00"):
    return mapping.NormalizedComment(
        id=cid, author=author, created_at=at, body=body, migrated=False
    )


# ---------------------------------------------------------------------------
# telling our own reports apart from a human's reply
# ---------------------------------------------------------------------------


def test_a_stamped_report_is_recognised_as_ours():
    stamped = feedback.stamp("Dev Agent đã xử lý BLS-1 và mở merge request.")

    assert feedback.REPORT_MARKER in stamped
    assert feedback.is_bot_report(_comment(stamped)) is True


def test_stamping_twice_does_not_duplicate_the_marker():
    once = feedback.stamp("x")

    assert feedback.stamp(once).count(feedback.REPORT_MARKER) == 1


@pytest.mark.parametrize(
    "body",
    [
        "Dev Agent đã xử lý BLS-1 và mở merge request.",
        "Dev Agent đã phân tích BLS-1 — KHÔNG sửa code app.",
        "Dev Agent không hoàn thành được BLS-1 (lần thử 2, dừng ở bước: push).",
        "Dev Agent ĐÃ DỪNG BLS-1 — hết số lần thử.",
    ],
)
def test_reports_posted_before_the_marker_existed_are_still_recognised(body):
    """Otherwise the first feedback pass reads our own history as new feedback."""
    assert feedback.is_bot_report(_comment(body)) is True


def test_a_human_reply_is_not_a_bot_report():
    assert feedback.is_bot_report(_comment("Chỗ này thiếu validate input")) is False


def test_the_jira_migration_bot_is_never_treated_as_a_reviewer():
    """It bulk-imported years of comments; they are context, not instructions."""
    assert feedback.is_actionable(_comment("something old", author="bloy_token")) is False


@pytest.mark.parametrize("body", ["ok", "OK!", "thanks", "Cảm ơn", "👍", "+1", "  đã rõ  "])
def test_a_bare_acknowledgement_does_not_start_a_container(body):
    assert feedback.is_actionable(_comment(body)) is False


def test_an_instruction_that_merely_contains_ok_is_still_actionable():
    assert feedback.is_actionable(_comment("ok nhưng hãy thêm test cho case rỗng")) is True


def test_an_empty_comment_is_not_actionable():
    assert feedback.is_actionable(_comment("   ")) is False


# ---------------------------------------------------------------------------
# which comment is the pending feedback
# ---------------------------------------------------------------------------


def test_feedback_is_only_what_came_after_our_last_report():
    comments = [
        _comment("mô tả thêm từ hồi xưa", cid="old"),
        _comment(feedback.stamp("Dev Agent đã xử lý BLS-1."), cid="report"),
        _comment("Thiếu guard cho archived", cid="new"),
    ]

    pending = feedback.pending(comments)

    assert pending is not None
    assert pending.id == "new"


def test_nothing_is_pending_before_the_agent_has_reported():
    """There is nothing to give feedback on yet."""
    assert feedback.pending([_comment("làm giúp mình cái này", cid="a")]) is None


def test_nothing_is_pending_when_our_report_is_still_the_last_word():
    comments = [
        _comment("Thiếu guard", cid="old-feedback"),
        _comment(feedback.stamp("Dev Agent đã xử lý BLS-1."), cid="report"),
    ]

    assert feedback.pending(comments) is None


def test_the_newest_instruction_wins_over_an_earlier_one():
    """A reviewer who wrote three follow-ups meant the last one."""
    comments = [
        _comment(feedback.stamp("Dev Agent đã xử lý BLS-1."), cid="report"),
        _comment("đổi tên biến", cid="f1"),
        _comment("thôi, bỏ hẳn hàm đó đi", cid="f2"),
    ]

    assert feedback.pending(comments).id == "f2"


def test_noise_after_a_real_instruction_does_not_hide_it():
    comments = [
        _comment(feedback.stamp("Dev Agent đã xử lý BLS-1."), cid="report"),
        _comment("thêm test cho case rỗng", cid="f1"),
        _comment("thanks", cid="f2"),
    ]

    assert feedback.pending(comments).id == "f1"


def test_a_second_round_reads_only_past_the_second_report():
    comments = [
        _comment(feedback.stamp("Dev Agent đã xử lý BLS-1."), cid="r1"),
        _comment("thêm test", cid="f1"),
        _comment(feedback.stamp("Dev Agent đã xử lý BLS-1 lần 2."), cid="r2"),
        _comment("test vẫn thiếu case null", cid="f2"),
    ]

    assert feedback.pending(comments).id == "f2"


# ---------------------------------------------------------------------------
# the revision prompt
# ---------------------------------------------------------------------------


def test_the_revision_prompt_puts_the_feedback_above_the_original_task():
    prompt = feedback.revision_prompt(
        "ORIGINAL TASK", _comment("bỏ hẳn hàm foo", author="Cuong")
    )

    assert prompt.index("bỏ hẳn hàm foo") < prompt.index("ORIGINAL TASK")
    assert "AUTHORITATIVE INSTRUCTION" in prompt
    assert "Cuong" in prompt


def _flat(text: str) -> str:
    """Collapse the template's line wrapping so assertions match phrases."""
    return " ".join(text.split())


def test_the_revision_prompt_tells_the_agent_not_to_start_over():
    """The merge request's history is what the reviewer reads next."""
    prompt = _flat(feedback.revision_prompt("TASK", _comment("sửa chỗ này")))

    assert "Do NOT start over" in prompt
    assert "git diff" in prompt


def test_the_revision_prompt_forbids_silently_skipping_the_feedback():
    prompt = _flat(feedback.revision_prompt("TASK", _comment("làm X")))

    assert "do NOT silently skip it" in prompt


# ---------------------------------------------------------------------------
# claiming — the poll must not start two containers for one comment
# ---------------------------------------------------------------------------


def test_a_comment_can_only_be_claimed_once(bloy_db):
    first = store.claim_feedback(
        comment_id="c1", issue_id="i1", issue_key="BLS-1", feedback="sửa X", author="Cuong"
    )
    second = store.claim_feedback(
        comment_id="c1", issue_id="i1", issue_key="BLS-1", feedback="sửa X", author="Cuong"
    )

    assert first is True
    assert second is False, "the next poll tick must not run it again"


def test_a_comment_with_no_id_is_never_claimed(bloy_db):
    assert store.claim_feedback(comment_id="", issue_id="i", issue_key="K") is False


def test_the_claimed_round_records_what_was_asked_for(bloy_db):
    store.claim_feedback(
        comment_id="c2", issue_id="i1", issue_key="BLS-9", feedback="thêm test", author="An"
    )
    store.attach_feedback_run("c2", "run-42")

    rounds = store.feedback_rounds()

    assert [r.comment_id for r in rounds] == ["c2"]
    assert rounds[0].run_id == "run-42"
    assert rounds[0].feedback == "thêm test"
    assert rounds[0].author == "An"
