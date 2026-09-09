"""End-to-end tests for the loop driver, against a fake sandbox session.

The container path is otherwise only covered by mocks of the *whole* run, so
the parts that decide whether unverified work ships — the order of the turns,
what the retry is told, and whether a failing verify command can be overruled
by a confident evaluator — would never be executed by the suite. Same gap, and
same reasoning, as ``features/local_runner.py`` documents for the shell
assembly.
"""

from __future__ import annotations

import json

import pytest

from bloy_dev_agent.features import sandbox_runner
from bloy_dev_agent.features.coding import controller as ctrl
from bloy_dev_agent.features.coding import evaluator as ev
from bloy_dev_agent.features.coding import loop as coding_loop
from bloy_dev_agent.features.coding.budget import LoopBudget


class FakeSession:
    """Stands in for :class:`sandbox_runner.SandboxSession`.

    Records every turn and command so a test can assert the *order* things
    happened in, which is where the interesting properties live: receipts must
    be collected before the evaluator is prompted, or the evaluator grades
    without them.
    """

    #: Set by each test before ``run_loop`` is called.
    script: list = []
    commands: dict = {}
    setup_error: str = ""
    verdict_file: str = ""
    instance: FakeSession | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.sandbox_id = "sbx-test"
        self.workdir = "/worktrees/bls-1-api"
        self.turns: list[dict] = []
        self.ran: list[tuple[str, str]] = []
        self.pushed: dict[str, str] = {}
        self.events: list[str] = []
        self.closed = False
        FakeSession.instance = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.closed = True

    async def turn(self, prompt, *, mode, log_path=None, append=False):
        self.turns.append({"prompt": prompt, "mode": mode, "append": append})
        self.events.append(f"turn:{mode}")
        step = FakeSession.script[len(self.turns) - 1]
        return sandbox_runner.TurnResult(
            ok=step.get("ok", True),
            output=step.get("output", ""),
            exit_code=0 if step.get("ok", True) else 1,
            tokens=step.get("tokens", 10),
            cost_usd=step.get("cost_usd", 0.01),
            log_path=log_path,
        )

    async def run_as_agent(self, command, *, cwd=""):
        self.ran.append((command, cwd))
        self.events.append(f"cmd:{command}")
        return FakeSession.commands.get(command, (0, "ok"))

    async def push_file(self, path, content):
        self.pushed[path] = content
        self.events.append(f"push:{path}")

    async def read_file(self, path, limit=200_000):
        return FakeSession.verdict_file


@pytest.fixture(autouse=True)
def fake_session(monkeypatch):
    FakeSession.script = []
    FakeSession.commands = {}
    FakeSession.setup_error = ""
    FakeSession.verdict_file = ""
    FakeSession.instance = None

    def build(**kwargs):
        session = FakeSession(**kwargs)
        session.setup_error = FakeSession.setup_error
        return session

    monkeypatch.setattr(sandbox_runner, "SandboxSession", build)
    return FakeSession


def _verdict(kind, score=0.5, missing=""):
    return json.dumps({"verdict": kind, "score": score, "missing": missing})


async def _run(tmp_path, **overrides):
    kwargs = dict(
        base_prompt="ORIGINAL TASK",
        objective="BLS-1: làm X",
        worktree=tmp_path / "wt" / "bls-1-api",
        worktree_root=tmp_path / "wt",
        run_id="run-1",
        budget=LoopBudget(max_attempts=2),
        verify_commands_raw="",
        changed_repos=lambda: [],
    )
    kwargs.update(overrides)
    return await coding_loop.run_loop(**kwargs)


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


async def test_a_pass_on_the_first_attempt_finishes_with_one_attempt(tmp_path):
    FakeSession.script = [
        {"output": "đã sửa translations.service.ts"},
        {"output": f"Verdict: {_verdict('pass', 1.0)}"},
    ]

    outcome = await _run(tmp_path)

    assert outcome.outcome == ctrl.OUTCOME_COMPLETE
    assert outcome.ok is True
    assert len(outcome.attempts) == 1
    assert outcome.answer == "đã sửa translations.service.ts"


async def test_the_generator_writes_and_the_evaluator_only_reads(tmp_path):
    """The turn that judges the work must not be able to repair it."""
    FakeSession.script = [
        {"output": "done"},
        {"output": _verdict("pass", 1.0)},
    ]

    await _run(tmp_path)

    modes = [turn["mode"] for turn in FakeSession.instance.turns]
    assert modes == [sandbox_runner.MODE_IMPLEMENT, sandbox_runner.MODE_EVALUATE]


async def test_the_container_is_always_closed(tmp_path):
    FakeSession.script = [{"output": "done"}, {"output": _verdict("pass", 1.0)}]

    await _run(tmp_path)

    assert FakeSession.instance.closed is True


# ---------------------------------------------------------------------------
# retrying with what the evaluator said
# ---------------------------------------------------------------------------


async def test_a_failed_attempt_retries_carrying_the_evaluator_feedback(tmp_path):
    FakeSession.script = [
        {"output": "attempt 1"},
        {"output": _verdict("fail", 0.4, "thiếu guard cho status=archived")},
        {"output": "attempt 2"},
        {"output": _verdict("pass", 1.0)},
    ]

    outcome = await _run(tmp_path)

    assert outcome.outcome == ctrl.OUTCOME_COMPLETE
    assert len(outcome.attempts) == 2
    retry_prompt = FakeSession.instance.turns[2]["prompt"]
    assert "thiếu guard cho status=archived" in retry_prompt
    assert "ORIGINAL TASK" in retry_prompt, "a fresh claude -p remembers nothing"


async def test_the_reported_answer_is_the_newest_attempts_not_the_first(tmp_path):
    """The code on disk is the last attempt's, so the report must match it."""
    FakeSession.script = [
        {"output": "first try"},
        {"output": _verdict("fail", 0.4, "chưa đủ")},
        {"output": "second try"},
        {"output": _verdict("pass", 1.0)},
    ]

    outcome = await _run(tmp_path)

    assert outcome.answer == "second try"


async def test_generator_turns_append_to_one_log_so_the_page_shows_the_whole_run(tmp_path):
    FakeSession.script = [
        {"output": "a"},
        {"output": _verdict("fail", 0.4, "x")},
        {"output": "b"},
        {"output": _verdict("pass", 1.0)},
    ]

    await _run(tmp_path)

    generator_turns = [
        turn for turn in FakeSession.instance.turns
        if turn["mode"] == sandbox_runner.MODE_IMPLEMENT
    ]
    assert [turn["append"] for turn in generator_turns] == [False, True]


async def test_running_out_of_attempts_reports_capped_and_keeps_the_work(tmp_path):
    FakeSession.script = [
        {"output": "a"},
        {"output": _verdict("fail", 0.4, "vẫn thiếu")},
        {"output": "b"},
        {"output": _verdict("fail", 0.4, "vẫn thiếu")},
    ]

    outcome = await _run(tmp_path)

    assert outcome.outcome == ctrl.OUTCOME_CAPPED
    assert outcome.ok is False
    assert outcome.answer == "b", "the diff is still there for a human to read"


# ---------------------------------------------------------------------------
# trusted receipts outrank the evaluator's opinion
# ---------------------------------------------------------------------------


async def test_a_failing_verify_command_overrules_a_pass_verdict(tmp_path):
    """An agent's confidence cannot beat a command this service ran itself."""
    FakeSession.script = [
        {"output": "đã sửa"},
        {"output": _verdict("pass", 1.0)},
        {"output": "sửa tiếp"},
        {"output": _verdict("pass", 1.0)},
    ]
    FakeSession.commands = {"npm test": (1, "1 test failed")}

    outcome = await _run(
        tmp_path,
        verify_commands_raw="api: npm test\n",
        changed_repos=lambda: ["api"],
    )

    assert outcome.outcome != ctrl.OUTCOME_COMPLETE
    first = outcome.attempts[0]
    assert first.verdict.passed is False
    assert "npm test" in first.verdict.missing
    assert first.receipts is not None and first.receipts.all_ok is False


async def test_a_passing_verify_command_leaves_the_pass_alone(tmp_path):
    FakeSession.script = [{"output": "đã sửa"}, {"output": _verdict("pass", 1.0)}]
    FakeSession.commands = {"npm test": (0, "3 passed")}

    outcome = await _run(
        tmp_path,
        verify_commands_raw="api: npm test\n",
        changed_repos=lambda: ["api"],
    )

    assert outcome.outcome == ctrl.OUTCOME_COMPLETE
    assert outcome.attempts[0].receipts.all_ok is True


async def test_receipts_are_collected_before_the_evaluator_is_prompted(tmp_path):
    """An evaluator prompted first would grade without the evidence."""
    FakeSession.script = [{"output": "x"}, {"output": _verdict("pass", 1.0)}]
    FakeSession.commands = {"npm test": (0, "ok")}

    await _run(
        tmp_path,
        verify_commands_raw="api: npm test\n",
        changed_repos=lambda: ["api"],
    )

    events = FakeSession.instance.events
    assert events.index("cmd:npm test") < events.index(
        f"turn:{sandbox_runner.MODE_EVALUATE}"
    )
    assert events.index(f"push:{ev.RECEIPTS_PATH}") < events.index(
        f"turn:{sandbox_runner.MODE_EVALUATE}"
    )


async def test_only_the_repos_that_changed_are_verified(tmp_path):
    FakeSession.script = [{"output": "x"}, {"output": _verdict("pass", 1.0)}]

    await _run(
        tmp_path,
        verify_commands_raw="api: npm test\ncms: npm run build\n",
        changed_repos=lambda: ["api"],
    )

    assert [command for command, _ in FakeSession.instance.ran] == ["npm test"]


async def test_a_verify_command_runs_in_its_own_repo_worktree(tmp_path):
    FakeSession.script = [{"output": "x"}, {"output": _verdict("pass", 1.0)}]

    await _run(
        tmp_path,
        verify_commands_raw="api: npm test\n",
        changed_repos=lambda: ["api"],
    )

    assert FakeSession.instance.ran[0][1] == f"{sandbox_runner.WORKTREE_MOUNT}/api"


async def test_a_malformed_command_list_means_no_trusted_runner_not_a_crash(tmp_path):
    FakeSession.script = [{"output": "x"}, {"output": _verdict("pass", 1.0)}]

    outcome = await _run(
        tmp_path,
        verify_commands_raw="api: npm test && npm run lint\n",
        changed_repos=lambda: ["api"],
    )

    assert outcome.attempts[0].receipts is None
    assert FakeSession.instance.ran == []
    # And the evaluator is told so, rather than left to assume checks ran.
    evaluator_prompt = FakeSession.instance.turns[1]["prompt"]
    assert "could not produce trusted command receipts" in evaluator_prompt


# ---------------------------------------------------------------------------
# reading the verdict
# ---------------------------------------------------------------------------


async def test_the_verdict_file_beats_the_reply_text(tmp_path):
    """Stdout is narration; the file is what the evaluator decided to write."""
    FakeSession.script = [
        {"output": "x"},
        {"output": f"I think it is fine: {_verdict('pass', 1.0)}"},
    ]
    FakeSession.verdict_file = _verdict("fail", 0.2, "thiếu test")

    outcome = await _run(tmp_path, budget=LoopBudget(max_attempts=1))

    assert outcome.outcome == ctrl.OUTCOME_CAPPED
    assert outcome.attempts[0].verdict.missing == "thiếu test"


async def test_an_evaluator_that_graded_nothing_is_a_fail_not_a_pass(tmp_path):
    FakeSession.script = [{"output": "x"}, {"output": "Trông ổn đấy."}]

    outcome = await _run(tmp_path, budget=LoopBudget(max_attempts=1))

    assert outcome.outcome == ctrl.OUTCOME_CAPPED
    assert outcome.attempts[0].verdict.unparsed is True


async def test_a_malformed_verdict_file_falls_back_to_the_reply(tmp_path):
    FakeSession.script = [{"output": "x"}, {"output": _verdict("pass", 1.0)}]
    FakeSession.verdict_file = "{not json at all"

    outcome = await _run(tmp_path)

    assert outcome.outcome == ctrl.OUTCOME_COMPLETE


# ---------------------------------------------------------------------------
# failure paths
# ---------------------------------------------------------------------------


async def test_a_container_that_could_not_be_prepared_asks_for_a_human(tmp_path):
    FakeSession.setup_error = "Sandbox setup failed: no credentials"
    FakeSession.script = []

    outcome = await _run(tmp_path)

    assert outcome.outcome == ctrl.OUTCOME_NEEDS_HUMAN
    assert outcome.setup_error
    assert FakeSession.instance.turns == [], "never prompt a container with no login"


async def test_a_generator_turn_that_failed_is_not_graded(tmp_path):
    """There is nothing to evaluate, and a wasted evaluator turn costs money."""
    FakeSession.script = [
        {"output": "rate limited", "ok": False},
        {"output": "rate limited", "ok": False},
    ]

    outcome = await _run(tmp_path)

    modes = [turn["mode"] for turn in FakeSession.instance.turns]
    assert sandbox_runner.MODE_EVALUATE not in modes
    assert outcome.attempts[0].verdict is None


async def test_repeated_generator_failures_stall_instead_of_burning_the_cap(tmp_path):
    FakeSession.script = [{"output": "boom", "ok": False}] * 5

    outcome = await _run(
        tmp_path, budget=LoopBudget(max_attempts=5, max_zero_streak=2)
    )

    assert outcome.outcome == ctrl.OUTCOME_STALLED
    assert len(outcome.attempts) == 2


# ---------------------------------------------------------------------------
# staging stays exactly as it was
# ---------------------------------------------------------------------------


async def test_a_staging_run_gets_the_chromium_image_and_the_captured_session(tmp_path):
    FakeSession.script = [{"output": "x"}, {"output": _verdict("pass", 1.0)}]

    await _run(tmp_path, staging=True, staging_token="tok-1")

    kwargs = FakeSession.instance.kwargs
    assert kwargs["image"] == sandbox_runner.STAGING_IMAGE
    assert kwargs["shopify_auth_dir"] == sandbox_runner.SHOPIFY_AUTH_DIR
    assert kwargs["staging"] is True
    assert kwargs["staging_token"] == "tok-1"


async def test_an_ordinary_run_gets_neither(tmp_path):
    FakeSession.script = [{"output": "x"}, {"output": _verdict("pass", 1.0)}]

    await _run(tmp_path)

    kwargs = FakeSession.instance.kwargs
    assert kwargs["image"] == sandbox_runner.DEFAULT_IMAGE
    assert kwargs["shopify_auth_dir"] is None


# ---------------------------------------------------------------------------
# the report a reviewer reads
# ---------------------------------------------------------------------------


async def test_the_report_names_every_attempt_and_what_it_scored(tmp_path):
    FakeSession.script = [
        {"output": "a"},
        {"output": _verdict("fail", 0.4, "thiếu X")},
        {"output": "b"},
        {"output": _verdict("pass", 0.95)},
    ]

    outcome = await _run(tmp_path)
    report = "\n".join(outcome.report_lines())

    assert "Lần 1" in report and "Lần 2" in report
    assert "fail" in report and "pass" in report
    assert "thiếu X" in report


async def test_the_report_says_whether_the_verify_commands_passed(tmp_path):
    FakeSession.script = [{"output": "x"}, {"output": _verdict("pass", 1.0)}]
    FakeSession.commands = {"npm test": (0, "ok")}

    outcome = await _run(
        tmp_path, verify_commands_raw="api: npm test\n", changed_repos=lambda: ["api"]
    )
    report = "\n".join(outcome.report_lines())

    assert "1/1 lệnh verify PASS" in report
    assert "service tự chạy" in report
