"""Tests for run bookkeeping and the attempt cap.

The cap is a spending control, so the tests here are about money as much as
correctness: a miscount either lets an agent retry forever or blocks an issue
that still had tries left.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bloy_dev_agent import store
from bloy_dev_agent.models import BloyPipelineRun, BloySetting


@pytest.fixture()
def db(monkeypatch, tmp_path):
    """An isolated SQLite database bound to this plugin's tables only."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    for model in (BloyPipelineRun, BloySetting):
        model.__table__.create(engine, checkfirst=True)
    monkeypatch.setattr(store, "SessionLocal", sessionmaker(bind=engine))
    return engine


def _fail(issue_id="i-1", issue_key="BLOY-9", **kw):
    run_id = store.start_run(
        issue_id=issue_id,
        issue_key=issue_key,
        project_id="p-1",
        attempt=kw.pop("attempt", 1),
        **kw,
    )
    store.finish_run(run_id, state=BloyPipelineRun.STATE_FAILED, stage="sandbox")
    return run_id


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_max_attempts_defaults_when_unset(db):
    assert store.max_attempts() == store.DEFAULT_MAX_ATTEMPTS


def test_max_attempts_reads_the_saved_value(db):
    store.save_settings({store.SETTING_MAX_ATTEMPTS: "3"})

    assert store.max_attempts() == 3


@pytest.mark.parametrize("bad", ["", "abc", "0", "-4", None])
def test_a_nonsense_ceiling_never_blocks_everything(db, bad):
    """A zero or unparsable ceiling would block every issue on its first try."""
    store.save_settings({store.SETTING_MAX_ATTEMPTS: bad or ""})

    assert store.max_attempts() >= 1


def test_saving_one_setting_leaves_the_others_alone(db):
    store.save_settings({store.SETTING_PROJECT_ID: "proj", store.SETTING_MAX_ATTEMPTS: "4"})
    store.save_settings({store.SETTING_TARGET_REPO: "shopify-app-loyalty-api"})

    saved = store.get_settings()
    assert saved[store.SETTING_PROJECT_ID] == "proj"
    assert saved[store.SETTING_MAX_ATTEMPTS] == "4"


def test_comments_disabled_defaults_to_off(db):
    assert store.comments_disabled() is False


@pytest.mark.parametrize("value", ["on", "1", "true", "YES"])
def test_comments_disabled_reads_the_saved_value(db, value):
    store.save_settings({store.SETTING_COMMENTS_DISABLED: value})

    assert store.comments_disabled() is True


def test_comments_disabled_turns_back_off_when_cleared(db):
    """An unchecked checkbox submits nothing — the caller must be able to save
    an empty string back and have that actually mean "off" again."""
    store.save_settings({store.SETTING_COMMENTS_DISABLED: "on"})
    store.save_settings({store.SETTING_COMMENTS_DISABLED: ""})

    assert store.comments_disabled() is False


# ---------------------------------------------------------------------------
# Attempt cap
# ---------------------------------------------------------------------------


def test_a_fresh_issue_has_all_its_tries(db):
    status = store.attempt_status("i-new", "BLOY-1")

    assert (status.failed, status.exhausted) == (0, False)
    assert status.remaining == store.DEFAULT_MAX_ATTEMPTS


def test_failures_accumulate_until_the_ceiling(db):
    store.save_settings({store.SETTING_MAX_ATTEMPTS: "3"})

    for expected in (1, 2):
        _fail()
        status = store.attempt_status("i-1", "BLOY-9")
        assert (status.failed, status.exhausted) == (expected, False)

    _fail()

    assert store.attempt_status("i-1", "BLOY-9").exhausted is True


def test_success_does_not_count_against_the_cap(db):
    run_id = store.start_run(
        issue_id="i-2", issue_key="BLOY-8", project_id="p-1", attempt=1
    )
    store.finish_run(run_id, state=BloyPipelineRun.STATE_SUCCESS, stage="done")

    assert store.attempt_status("i-2", "BLOY-8").failed == 0


def test_a_running_run_does_not_count_yet(db):
    """Counting an in-flight run would block a retry that never happened."""
    store.start_run(issue_id="i-3", issue_key="BLOY-7", project_id="p-1", attempt=1)

    assert store.attempt_status("i-3", "BLOY-7").failed == 0


def test_a_blocked_record_does_not_shift_the_ceiling(db):
    """The refusal is a record of itself; counting it would ratchet the cap down."""
    store.save_settings({store.SETTING_MAX_ATTEMPTS: "2"})
    _fail(issue_id="i-4", issue_key="BLOY-6")
    _fail(issue_id="i-4", issue_key="BLOY-6")

    store.record_blocked(
        issue_id="i-4",
        issue_key="BLOY-6",
        project_id="p-1",
        attempt=3,
        detail="hết lượt",
    )

    assert store.attempt_status("i-4", "BLOY-6").failed == 2


def test_attempts_are_counted_per_issue(db):
    store.save_settings({store.SETTING_MAX_ATTEMPTS: "1"})
    _fail(issue_id="i-5", issue_key="BLOY-5")

    assert store.attempt_status("i-5", "BLOY-5").exhausted is True
    assert store.attempt_status("i-6", "BLOY-4").exhausted is False


def test_reset_releases_a_blocked_issue(db):
    store.save_settings({store.SETTING_MAX_ATTEMPTS: "1"})
    _fail(issue_id="i-7", issue_key="BLOY-3")
    store.record_blocked(
        issue_id="i-7", issue_key="BLOY-3", project_id="p-1", attempt=2, detail="x"
    )
    assert store.attempt_status("i-7", "BLOY-3").exhausted is True

    dropped = store.reset_attempts("i-7")

    assert dropped == 2, "both the failure and the refusal should be cleared"
    assert store.attempt_status("i-7", "BLOY-3").exhausted is False


def test_reset_does_not_touch_a_successful_run(db):
    """History of work that landed must survive a cap reset."""
    ok = store.start_run(issue_id="i-8", issue_key="BLOY-2", project_id="p-1", attempt=1)
    store.finish_run(ok, state=BloyPipelineRun.STATE_SUCCESS, stage="done")
    _fail(issue_id="i-8", issue_key="BLOY-2")

    store.reset_attempts("i-8")

    keys = [run.state for run in store.recent_runs()]
    assert BloyPipelineRun.STATE_SUCCESS in keys


# ---------------------------------------------------------------------------
# Run records
# ---------------------------------------------------------------------------


def test_stages_and_fields_are_stamped_as_the_run_moves(db):
    run_id = store.start_run(
        issue_id="i-9", issue_key="BLOY-10", project_id="p-1", attempt=1
    )
    store.set_stage(run_id, "sandbox", branch="bloy/bloy-10")
    store.finish_run(
        run_id,
        state=BloyPipelineRun.STATE_SUCCESS,
        stage="done",
        merge_request_url="https://gitlab/mr/5",
    )

    run = store.get_run(run_id)
    assert (run.stage, run.state) == ("done", BloyPipelineRun.STATE_SUCCESS)
    assert run.branch == "bloy/bloy-10"
    assert run.merge_request_url == "https://gitlab/mr/5"
    assert run.finished_at is not None


def test_active_runs_lists_only_what_is_still_going(db):
    running = store.start_run(
        issue_id="i-10", issue_key="BLOY-11", project_id="p-1", attempt=1
    )
    _fail(issue_id="i-11", issue_key="BLOY-12")

    assert [run.id for run in store.active_runs()] == [running]


def test_blocked_issues_reports_one_row_per_issue(db):
    for _ in range(3):
        store.record_blocked(
            issue_id="i-12", issue_key="BLOY-13", project_id="p-1", attempt=6, detail="x"
        )

    assert len(store.blocked_issues()) == 1


# ---------------------------------------------------------------------------
# A blip must not spend a paid attempt
# ---------------------------------------------------------------------------


def test_an_aborted_run_does_not_count_against_the_cap(db):
    """Observed live: one 15s Twenty timeout on the claim burned an attempt.

    The cap limits spending. A run that never reached the sandbox consumed no
    container and no agent session, so counting it would let a network blip eat
    a paid try.
    """
    store.save_settings({store.SETTING_MAX_ATTEMPTS: "2"})
    for _ in range(5):
        run_id = store.start_run(
            issue_id="i-blip", issue_key="BLS-1077", project_id="p", attempt=1
        )
        store.finish_run(
            run_id, state=BloyPipelineRun.STATE_ABORTED, stage="claim",
            detail="không claim được issue",
        )

    status = store.attempt_status("i-blip", "BLS-1077")

    assert (status.failed, status.exhausted) == (0, False)


def test_minutes_since_last_pass_is_none_when_nothing_ran(db):
    assert store.minutes_since_last_pass() is None


def test_minutes_since_last_pass_tracks_the_newest_run(db):
    store.start_run(issue_id="i-x", issue_key="BLS-1", project_id="p", attempt=1)

    idle = store.minutes_since_last_pass()

    assert idle is not None and idle < 1
