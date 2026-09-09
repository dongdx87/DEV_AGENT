"""Tests for the unattended coding loop.

The behaviours worth pinning here are the ones that would silently ship
unverified work rather than raise: an evaluator whose verdict could not be read
counting as success, a "pass" standing while a verify command this service ran
was still failing, and a retry that never learns what was wrong.
"""

from __future__ import annotations

import json

import pytest

from bloy_dev_agent.features import agent_log, sandbox_runner
from bloy_dev_agent.features.coding import controller as ctrl
from bloy_dev_agent.features.coding import evaluator as ev
from bloy_dev_agent.features.coding import loop as coding_loop
from bloy_dev_agent.features.coding import receipts as rc
from bloy_dev_agent.features.coding.budget import LoopBudget, LoopLedger
from bloy_dev_agent.features.coding.verdict import (
    LoopVerdict,
    parse_verdict,
    unparsable,
)

# ---------------------------------------------------------------------------
# verdict parsing — the evaluator answers in prose with JSON somewhere in it
# ---------------------------------------------------------------------------


def test_a_plain_verdict_object_is_read():
    verdict = parse_verdict('{"verdict": "pass", "score": 0.9}')

    assert verdict is not None
    assert verdict.verdict is LoopVerdict.PASS
    assert verdict.score == 0.9


def test_the_last_object_wins_so_a_draft_never_beats_the_final_one():
    """An evaluator that reconsiders must be graded on its conclusion."""
    text = (
        'First I thought {"verdict": "pass", "score": 1.0} but on reading the '
        'diff again: {"verdict": "fail", "score": 0.3, "missing": "thiếu guard"}'
    )

    verdict = parse_verdict(text)

    assert verdict.verdict is LoopVerdict.FAIL
    assert verdict.missing == "thiếu guard"


def test_a_brace_inside_a_string_does_not_end_the_object():
    text = '{"verdict": "fail", "missing": "sửa hàm f() { return 1 } trong a.ts"}'

    verdict = parse_verdict(text)

    assert verdict.verdict is LoopVerdict.FAIL
    assert "return 1" in verdict.missing


def test_prose_with_no_json_yields_no_verdict():
    assert parse_verdict("Tôi nghĩ là xong rồi, khá ổn.") is None


def test_an_unknown_verdict_word_is_not_accepted():
    """Anything but the three known values is no verdict at all, not a pass."""
    assert parse_verdict('{"verdict": "probably-fine", "score": 1.0}') is None


def test_a_score_outside_the_range_is_clamped():
    assert parse_verdict('{"verdict": "fail", "score": 7}').score == 1.0
    assert parse_verdict('{"verdict": "fail", "score": -3}').score == 0.0


def test_an_unreadable_verdict_is_a_fail_never_a_pass():
    """A broken evaluator must not be able to mark work complete."""
    verdict = unparsable("test")

    assert verdict.verdict is LoopVerdict.FAIL
    assert verdict.passed is False
    assert verdict.unparsed is True


# ---------------------------------------------------------------------------
# controller — pure continue-vs-stop
# ---------------------------------------------------------------------------


def _verdict(kind: str, score: float = 0.5, missing: str = "") -> object:
    return parse_verdict(
        json.dumps({"verdict": kind, "score": score, "missing": missing})
    )


def test_a_pass_finishes_the_loop():
    control = ctrl.LoopController(LoopBudget(max_attempts=3))
    control.first()

    done = control.after(_verdict("pass", 1.0), LoopLedger())

    assert isinstance(done, ctrl.Done)
    assert done.outcome == ctrl.OUTCOME_COMPLETE
    assert done.needs_human is False


def test_needs_human_is_never_retried():
    """A second attempt cannot answer a question nobody answered."""
    control = ctrl.LoopController(LoopBudget(max_attempts=5))
    control.first()

    done = control.after(_verdict("needs_human", 0.4, "cần quyết định schema"), LoopLedger())

    assert done.outcome == ctrl.OUTCOME_NEEDS_HUMAN
    assert done.needs_human is True
    assert "schema" in done.detail


def test_a_fail_continues_and_carries_the_missing_text_to_the_next_attempt():
    control = ctrl.LoopController(LoopBudget(max_attempts=3))
    control.first()

    step = control.after(_verdict("fail", 0.5, "thiếu test cho case archived"), LoopLedger())

    assert isinstance(step, ctrl.Continue)
    assert step.attempt == 2
    assert "case archived" in step.feedback


def test_a_missing_verdict_keeps_going_rather_than_passing():
    """Fail open: a broken evaluator costs an attempt, never a false success."""
    control = ctrl.LoopController(LoopBudget(max_attempts=3, max_zero_streak=0))
    control.first()

    step = control.after(None, LoopLedger())

    assert isinstance(step, ctrl.Continue)


def test_running_out_of_attempts_is_capped_and_routed_to_a_human():
    control = ctrl.LoopController(LoopBudget(max_attempts=1))
    control.first()

    done = control.after(_verdict("fail", 0.5, "vẫn thiếu X"), LoopLedger())

    assert done.outcome == ctrl.OUTCOME_CAPPED
    assert done.needs_human is True
    assert "vẫn thiếu X" in done.detail


def test_repeated_zero_progress_stops_early_instead_of_burning_the_cap():
    control = ctrl.LoopController(LoopBudget(max_attempts=10, max_zero_streak=2))
    control.first()

    assert isinstance(control.after(_verdict("fail", 0.0), LoopLedger()), ctrl.Continue)
    done = control.after(_verdict("fail", 0.0), LoopLedger())

    assert done.outcome == ctrl.OUTCOME_STALLED


def test_any_progress_resets_the_stall_streak():
    control = ctrl.LoopController(LoopBudget(max_attempts=10, max_zero_streak=2))
    control.first()
    control.after(_verdict("fail", 0.0), LoopLedger())

    step = control.after(_verdict("fail", 0.4), LoopLedger())

    assert isinstance(step, ctrl.Continue)
    assert control.zero_streak == 0


def test_a_crossed_budget_stops_even_with_attempts_left():
    control = ctrl.LoopController(LoopBudget(max_attempts=10))
    control.first()
    ledger = LoopLedger(budget=LoopBudget(max_tokens=100))
    ledger.add(tokens=150)

    done = control.after(_verdict("fail", 0.5), ledger)

    assert done.outcome == ctrl.OUTCOME_BUDGET
    assert "tokens" in done.detail


def test_a_pass_still_wins_over_a_crossed_budget():
    """Work that is finished and verified must not be reported as a budget stop."""
    control = ctrl.LoopController(LoopBudget(max_attempts=10))
    control.first()
    ledger = LoopLedger(budget=LoopBudget(max_tokens=100))
    ledger.add(tokens=150)

    done = control.after(_verdict("pass", 1.0), ledger)

    assert done.outcome == ctrl.OUTCOME_COMPLETE


def test_every_non_complete_outcome_routes_to_a_human():
    """There is deliberately no "gave up quietly" branch."""
    assert ctrl.OUTCOME_COMPLETE not in ctrl.HUMAN_OUTCOMES
    for outcome in (
        ctrl.OUTCOME_CAPPED,
        ctrl.OUTCOME_NEEDS_HUMAN,
        ctrl.OUTCOME_STALLED,
        ctrl.OUTCOME_BUDGET,
    ):
        assert outcome in ctrl.HUMAN_OUTCOMES
        assert outcome in ctrl.OUTCOME_LABELS


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------


def test_a_negative_usage_report_cannot_buy_extra_budget():
    ledger = LoopLedger(budget=LoopBudget(max_tokens=100))
    ledger.add(tokens=90)
    ledger.add(tokens=-500)

    assert ledger.total_tokens == 90
    assert ledger.exceeded() is None


def test_zero_caps_mean_unbounded():
    ledger = LoopLedger(budget=LoopBudget(max_tokens=0, max_cost_usd=0.0))
    ledger.add(tokens=10_000_000, cost_usd=999.0)

    assert ledger.exceeded() is None


# ---------------------------------------------------------------------------
# verification receipts — the agent never gets to claim a command ran
# ---------------------------------------------------------------------------


def test_commands_are_parsed_per_repo():
    commands = rc.parse_commands(
        "# tests\n"
        "shopify-app-loyalty-api: npm test\n"
        "\n"
        "shopify-app-loyalty-api: npm run lint\n"
        "shopify-app-loyalty-cms: npm run build\n"
    )

    assert [(c.repo, c.command) for c in commands] == [
        ("shopify-app-loyalty-api", "npm test"),
        ("shopify-app-loyalty-api", "npm run lint"),
        ("shopify-app-loyalty-cms", "npm run build"),
    ]


@pytest.mark.parametrize(
    "line",
    [
        "npm test",                              # no repo
        "api: npm test && npm run lint",         # two commands hiding as one
        "api: npm test; rm -rf /",               # chained
        "api: echo `whoami`",                    # substitution
        "api: npm test > out.txt",               # redirect
    ],
)
def test_a_command_that_is_not_one_command_is_rejected(line):
    """One configured line must be exactly one recorded receipt."""
    with pytest.raises(rc.VerifyContractError):
        rc.parse_commands(line)


def test_only_the_repos_this_run_touched_are_verified():
    raw = "api: npm test\ncms: npm run build\n"

    commands = rc.commands_for(raw, ["api"])

    assert [c.repo for c in commands] == ["api"]


def test_a_zero_exit_is_a_pass_and_a_nonzero_one_is_not():
    command = rc.VerifyCommand("api", "npm test")

    ok = rc.make_receipt(command, exit_code=0, output="1 passed", duration_ms=5)
    bad = rc.make_receipt(command, exit_code=1, output="1 failed", duration_ms=5)

    assert (ok.ran, ok.ok) == (True, True)
    assert (bad.ran, bad.ok) == (True, False)


def test_a_command_that_never_ran_is_distinct_from_one_that_failed():
    """"The check did not happen" and "the check failed" are different facts."""
    receipt = rc.make_receipt(
        rc.VerifyCommand("api", "npm test"),
        exit_code=-1,
        output="",
        duration_ms=0,
        error="RuntimeError: container died",
    )

    assert receipt.ran is False
    assert receipt.ok is False
    assert "container died" in receipt.error


def test_a_truncated_output_still_hashes_the_whole_thing():
    full = "x" * (rc.MAX_OUTPUT_CHARS + 500)

    receipt = rc.make_receipt(
        rc.VerifyCommand("api", "npm test"), exit_code=0, output=full, duration_ms=1
    )

    assert receipt.truncated is True
    assert len(receipt.output) == rc.MAX_OUTPUT_CHARS
    assert receipt.output_sha256 == rc._digest(full)


def test_a_batch_is_only_all_ok_when_every_command_passed():
    command = rc.VerifyCommand("api", "npm test")
    batch = rc.new_batch()
    batch.receipts.append(rc.make_receipt(command, exit_code=0, output="", duration_ms=1))
    assert batch.all_ok is True

    batch.receipts.append(rc.make_receipt(command, exit_code=1, output="", duration_ms=1))
    assert batch.all_ok is False
    assert len(batch.failed) == 1


def test_an_empty_batch_is_not_all_ok():
    """No checks is not the same as checks that passed."""
    assert rc.new_batch().all_ok is False


def test_the_projection_says_who_produced_it_and_carries_receipt_ids():
    batch = rc.new_batch()
    batch.receipts.append(
        rc.make_receipt(
            rc.VerifyCommand("api", "npm test"), exit_code=0, output="ok", duration_ms=1
        )
    )

    payload = json.loads(rc.projection(batch))

    assert "not the agent" in payload["produced_by"]
    assert payload["all_ok"] is True
    assert payload["receipts"][0]["id"] == batch.receipts[0].id


def test_run_batch_records_a_raising_executor_as_a_did_not_run():
    def boom(_command):
        raise RuntimeError("no container")

    batch = rc.run_batch([rc.VerifyCommand("api", "npm test")], execute=boom)

    assert batch.receipts[0].ran is False
    assert batch.all_ok is False


# ---------------------------------------------------------------------------
# turn flags — the evaluator must not be able to fix what it judges
# ---------------------------------------------------------------------------


def test_the_evaluator_gets_bash_but_no_edit_tool():
    flags = sandbox_runner.claude_flags(False, mode=sandbox_runner.MODE_EVALUATE)

    assert "Bash" in flags, "an evaluator that cannot run tests can only read"
    assert "--dangerously-skip-permissions" not in flags
    assert "Edit" not in flags and "Write" not in flags
    # plan mode would block the very Bash calls this mode exists to allow
    assert "--permission-mode plan" not in flags


def test_the_generator_still_gets_write_access():
    flags = sandbox_runner.claude_flags(True, mode=sandbox_runner.MODE_IMPLEMENT)

    assert flags == "--dangerously-skip-permissions"


def test_the_analysis_mode_is_unchanged_by_the_new_modes():
    assert sandbox_runner.claude_flags(False) == (
        "--allowedTools Read Grep Glob --permission-mode plan"
    )


# ---------------------------------------------------------------------------
# evaluator prompt
# ---------------------------------------------------------------------------


def test_without_receipts_the_evaluator_is_told_not_to_claim_a_command_passed():
    prompt = ev.build_prompt(
        objective="BLS-1: làm X",
        generator_answer="đã xong",
        workdir="/worktrees/x",
        receipts_available=False,
    )

    assert "could not produce trusted command receipts" in prompt
    assert "Do NOT" in prompt


def test_with_receipts_the_evaluator_is_pointed_at_the_file_and_told_it_is_authoritative():
    prompt = ev.build_prompt(
        objective="BLS-1: làm X",
        generator_answer="đã xong",
        workdir="/worktrees/x",
        receipts_available=True,
    )

    assert ev.RECEIPTS_PATH in prompt
    assert "may NOT replace a receipt" in prompt


def test_a_staging_run_with_no_screenshots_is_told_that_absence_is_a_finding():
    prompt = ev.build_prompt(
        objective="BLS-1",
        generator_answer="verified",
        workdir="/w",
        receipts_available=True,
        staging=True,
        screenshots=[],
    )

    assert "but saved NO" in prompt  # wraps in the template


def test_a_staging_run_lists_the_screenshots_found_on_the_host():
    prompt = ev.build_prompt(
        objective="BLS-1",
        generator_answer="verified",
        workdir="/w",
        receipts_available=True,
        staging=True,
        screenshots=["after.png", "before.png"],
    )

    assert "after.png" in prompt and "before.png" in prompt
    assert "observed fact" in prompt


def test_a_non_staging_run_never_mentions_staging_evidence():
    prompt = ev.build_prompt(
        objective="BLS-1", generator_answer="x", workdir="/w", receipts_available=True
    )

    assert "Staging verification evidence" not in prompt


def test_the_verdict_file_is_outside_every_worktree():
    """Anything under a worktree is swept into the merge request by git add -A."""
    assert ev.VERDICT_PATH.startswith("/tmp/")
    assert ev.RECEIPTS_PATH.startswith("/tmp/")


def test_a_retry_prompt_carries_both_the_feedback_and_the_whole_original_task():
    retry = ev.build_retry_prompt(
        "ORIGINAL TASK BODY", attempt=2, feedback="thiếu guard cho archived"
    )

    assert "attempt 2" in retry
    assert "thiếu guard cho archived" in retry
    assert "ORIGINAL TASK BODY" in retry, "a fresh claude -p remembers nothing"


# ---------------------------------------------------------------------------
# session timeout — attempts share one container's lifetime
# ---------------------------------------------------------------------------


def test_the_container_lifetime_scales_with_the_attempt_cap():
    assert coding_loop.session_timeout_minutes(30, 3) == 90


def test_the_container_lifetime_is_capped_however_many_attempts_are_configured():
    assert coding_loop.session_timeout_minutes(30, 100) == coding_loop.MAX_SESSION_MINUTES


# ---------------------------------------------------------------------------
# usage parsing off the stream log
# ---------------------------------------------------------------------------


def test_usage_comes_from_the_newest_result_event(tmp_path):
    log = tmp_path / "run.jsonl"
    log.write_text(
        json.dumps(
            {"type": "result", "usage": {"input_tokens": 1, "output_tokens": 2},
             "total_cost_usd": 0.1}
        )
        + "\n"
        + json.dumps(
            {"type": "result",
             "usage": {"input_tokens": 10, "output_tokens": 20,
                       "cache_read_input_tokens": 5},
             "total_cost_usd": 0.5}
        )
        + "\n",
        encoding="utf-8",
    )

    usage = agent_log.last_usage(log)

    assert usage["total_tokens"] == 35, "cache reads are billed input too"
    assert usage["cost_usd"] == 0.5


def test_a_log_with_no_result_yet_reports_zeros_not_a_guess(tmp_path):
    log = tmp_path / "run.jsonl"
    log.write_text(json.dumps({"type": "assistant", "message": {}}) + "\n", encoding="utf-8")

    assert agent_log.last_usage(log)["total_tokens"] == 0


def test_a_missing_log_does_not_raise(tmp_path):
    assert agent_log.last_usage(tmp_path / "absent.jsonl")["cost_usd"] == 0.0
