"""Tests for the worktree -> sandbox -> merge request pipeline.

The behaviours worth pinning are the ones that would quietly produce a wrong
result rather than an exception: an issue reported as done when the agent
changed nothing, and a push whose merge-request URL never made it back to the
reviewer.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from bloy_dev_agent.features import pipeline, workspace

# ---------------------------------------------------------------------------
# workspace
# ---------------------------------------------------------------------------


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture()
def monorepo(tmp_path: Path, monkeypatch) -> Path:
    """A monorepo directory holding one real git sub-project."""
    root = tmp_path / "BLOY"
    repo = root / "shopify-app-loyalty-api"
    repo.mkdir(parents=True)
    _git(["init", "-b", "master"], repo)
    _git(["config", "user.email", "dev@example.com"], repo)
    _git(["config", "user.name", "Dev Agent Test"], repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "init"], repo)
    return root


def test_prepare_creates_a_branch_per_issue(monorepo: Path, tmp_path: Path):
    space = workspace.prepare(
        "BLOY-2", monorepo=monorepo, repo="shopify-app-loyalty-api", root=tmp_path / "wt"
    )

    assert space.branch == "bloy/bloy-2"
    assert space.base_branch == "master"
    assert (space.path / "README.md").exists()
    assert space.path.is_relative_to(tmp_path / "wt"), "must stay out of the checkout"


def test_prepare_reuses_an_existing_worktree(monorepo: Path, tmp_path: Path):
    """A second pass must continue the branch, not lose the first attempt."""
    root = tmp_path / "wt"
    first = workspace.prepare(
        "BLOY-2", monorepo=monorepo, repo="shopify-app-loyalty-api", root=root
    )
    (first.path / "work.txt").write_text("in progress\n", encoding="utf-8")

    second = workspace.prepare(
        "BLOY-2", monorepo=monorepo, repo="shopify-app-loyalty-api", root=root
    )

    assert second.path == first.path
    assert (second.path / "work.txt").exists()


def test_prepare_rejects_an_unknown_repo(monorepo: Path, tmp_path: Path):
    with pytest.raises(workspace.WorkspaceError):
        workspace.prepare(
            "BLOY-2", monorepo=monorepo, repo="../etc", root=tmp_path / "wt"
        )


def test_has_changes_and_diffstat_follow_the_worktree(monorepo: Path, tmp_path: Path):
    space = workspace.prepare(
        "BLOY-3", monorepo=monorepo, repo="shopify-app-loyalty-api", root=tmp_path / "wt"
    )
    assert workspace.has_changes(space) is False

    (space.path / "README.md").write_text("hello world\n", encoding="utf-8")

    assert workspace.has_changes(space) is True
    assert "README.md" in workspace.diffstat(space)


def test_diffstat_reports_files_the_agent_created(monorepo: Path, tmp_path: Path):
    """A new file is the typical implement result and git diff ignores it."""
    space = workspace.prepare(
        "BLOY-6", monorepo=monorepo, repo="shopify-app-loyalty-api", root=tmp_path / "wt"
    )

    (space.path / "NEW_FEATURE.ts").write_text("export const x = 1\n", encoding="utf-8")

    assert workspace.has_changes(space) is True
    assert "NEW_FEATURE.ts" in workspace.diffstat(space)


def test_commit_and_push_refuses_an_empty_run(monorepo: Path, tmp_path: Path):
    """No changes means no merge request — and no misleading success."""
    space = workspace.prepare(
        "BLOY-4", monorepo=monorepo, repo="shopify-app-loyalty-api", root=tmp_path / "wt"
    )

    result = workspace.commit_and_push(space, title="BLOY-4: nothing")

    assert result["ok"] is False


def test_push_carries_the_merge_request_options(monorepo: Path, tmp_path: Path, monkeypatch):
    """GitLab opens the MR from push options, so those flags must be present."""
    space = workspace.prepare(
        "BLOY-5", monorepo=monorepo, repo="shopify-app-loyalty-api", root=tmp_path / "wt"
    )
    (space.path / "README.md").write_text("changed\n", encoding="utf-8")

    seen: list[list[str]] = []
    real = workspace._git

    def fake_git(args, cwd):
        if args[0] == "push":
            seen.append(args)
            return subprocess.CompletedProcess(
                args, 0, "remote: https://gitlab/x/-/merge_requests/7\n", ""
            )
        return real(args, cwd)

    monkeypatch.setattr(workspace, "_git", fake_git)

    result = workspace.commit_and_push(space, title="BLOY-5: change")

    assert result["ok"] is True
    assert result["merge_request_url"] == "https://gitlab/x/-/merge_requests/7"
    (push,) = seen
    assert "merge_request.create" in push
    assert f"merge_request.target={space.base_branch}" in push


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


class FakeTwenty:
    """Records what the pipeline would have written back to Twenty."""

    def __init__(self):
        self.updates: list[tuple[str, dict]] = []
        self.comments: list[str] = []

    def update_record(self, obj, record_id, payload):
        self.updates.append((record_id, payload))
        return {}

    def create_record(self, obj, payload):
        self.comments.append(str(payload))
        return {}


ISSUE = {"id": "issue-1", "issueKey": "BLOY-2", "name": "Sửa lỗi tính điểm"}
STATUSES = {"In Progress": "s-wip", "In Review": "s-review", "Todo": "s-todo"}


def _run(monkeypatch, monorepo, tmp_path, *, sandbox_result, changed: bool):
    client = FakeTwenty()
    monkeypatch.setattr(
        pipeline.sandbox_runner, "run_in_sandbox", lambda *a, **k: sandbox_result
    )
    monkeypatch.setattr(pipeline.workspace, "has_changes", lambda space: changed)
    monkeypatch.setattr(pipeline.workspace, "diffstat", lambda space: " README.md | 1 +")
    monkeypatch.setattr(
        pipeline.workspace,
        "commit_and_push",
        lambda space, **kw: {"ok": True, "merge_request_url": "https://gitlab/mr/9"},
    )
    outcome = pipeline.run_issue(
        client,
        ISSUE,
        monorepo=monorepo,
        target_repo="shopify-app-loyalty-api",
        worktree_root=tmp_path / "wt",
        statuses=STATUSES,
    )
    return client, outcome


def test_a_successful_run_reports_the_merge_request(monkeypatch, monorepo, tmp_path):
    ok = pipeline.sandbox_runner.SandboxResult(True, "đã sửa", "sb-1", 0)

    client, outcome = _run(monkeypatch, monorepo, tmp_path, sandbox_result=ok, changed=True)

    assert (outcome.ok, outcome.stage) == (True, "done")
    assert outcome.merge_request_url == "https://gitlab/mr/9"
    assert outcome.moved_to == "In Review"
    assert "https://gitlab/mr/9" in client.comments[0]


def test_the_issue_is_claimed_before_the_agent_runs(monkeypatch, monorepo, tmp_path):
    """The claim is the only lock; it has to land before any work starts."""
    ok = pipeline.sandbox_runner.SandboxResult(True, "đã sửa", "sb-1", 0)

    client, _ = _run(monkeypatch, monorepo, tmp_path, sandbox_result=ok, changed=True)

    assert client.updates[0] == ("issue-1", {"statusId": "s-wip"})


def test_a_run_that_changed_nothing_is_not_a_success(monkeypatch, monorepo, tmp_path):
    """The regression this guards: an empty run reported as a finished task."""
    ok = pipeline.sandbox_runner.SandboxResult(True, "tôi đã xem qua", "sb-1", 0)

    client, outcome = _run(monkeypatch, monorepo, tmp_path, sandbox_result=ok, changed=False)

    assert (outcome.ok, outcome.stage) == (False, "no-change")
    assert outcome.moved_to == "Todo", "must go back to the source column, not to review"


def test_a_failed_sandbox_never_reaches_git(monkeypatch, monorepo, tmp_path):
    bad = pipeline.sandbox_runner.SandboxResult(False, "container died", "sb-2", 1)
    monkeypatch.setattr(
        pipeline.workspace,
        "commit_and_push",
        lambda *a, **k: pytest.fail("git must not run after a failed sandbox"),
    )
    monkeypatch.setattr(pipeline.sandbox_runner, "run_in_sandbox", lambda *a, **k: bad)

    outcome = pipeline.run_issue(
        FakeTwenty(),
        ISSUE,
        monorepo=monorepo,
        target_repo="shopify-app-loyalty-api",
        worktree_root=tmp_path / "wt",
        statuses=STATUSES,
    )

    assert (outcome.ok, outcome.stage) == (False, "sandbox")


# ---------------------------------------------------------------------------
# Attempt cap — the cost guard
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def cap_db(monkeypatch, tmp_path):
    """Bind the store to a throwaway database for *every* test in this module.

    Autouse on purpose. ``run_issue`` records a run on every path, so without
    this the suite writes rows straight into the running instance's database —
    which it did, and those rows then counted as real failed attempts against
    real issues.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from bloy_dev_agent import store
    from bloy_dev_agent.models import BloyPipelineRun, BloySetting

    engine = create_engine(f"sqlite:///{tmp_path / 'cap.sqlite3'}")
    for model in (BloyPipelineRun, BloySetting):
        model.__table__.create(engine, checkfirst=True)
    monkeypatch.setattr(store, "SessionLocal", sessionmaker(bind=engine))
    return store


def test_the_cap_stops_the_run_before_any_money_is_spent(monkeypatch, monorepo, tmp_path, cap_db):
    """Blocked means: no claim, no worktree, no container — nothing billable."""
    cap_db.save_settings({cap_db.SETTING_MAX_ATTEMPTS: "2"})
    for _ in range(2):
        run_id = cap_db.start_run(
            issue_id="issue-1", issue_key="BLOY-2", project_id="p", attempt=1
        )
        cap_db.finish_run(run_id, state="failed", stage="sandbox")

    monkeypatch.setattr(
        pipeline.sandbox_runner,
        "run_in_sandbox",
        lambda *a, **k: pytest.fail("a blocked issue must not reach the sandbox"),
    )
    monkeypatch.setattr(
        pipeline.workspace,
        "prepare",
        lambda *a, **k: pytest.fail("a blocked issue must not get a worktree"),
    )
    client = FakeTwenty()

    outcome = pipeline.run_issue(
        client,
        ISSUE,
        monorepo=monorepo,
        target_repo="shopify-app-loyalty-api",
        worktree_root=tmp_path / "wt",
        statuses={**STATUSES, "Backlog": "s-backlog"},
    )

    assert (outcome.blocked, outcome.ok, outcome.stage) == (True, False, "blocked")
    assert outcome.moved_to == "Backlog", "must leave the source column or it re-blocks forever"
    assert "Reset attempts" in client.comments[0]


def test_a_blocked_issue_is_never_claimed(monkeypatch, monorepo, tmp_path, cap_db):
    """Claiming would move it to In Progress and strand it there."""
    cap_db.save_settings({cap_db.SETTING_MAX_ATTEMPTS: "1"})
    run_id = cap_db.start_run(
        issue_id="issue-1", issue_key="BLOY-2", project_id="p", attempt=1
    )
    cap_db.finish_run(run_id, state="failed", stage="sandbox")
    client = FakeTwenty()

    pipeline.run_issue(
        client,
        ISSUE,
        monorepo=monorepo,
        target_repo="shopify-app-loyalty-api",
        worktree_root=tmp_path / "wt",
        statuses={**STATUSES, "Backlog": "s-backlog"},
    )

    moved_to = [payload["statusId"] for _, payload in client.updates]
    assert "s-wip" not in moved_to


def test_a_failed_run_increments_the_counter(monkeypatch, monorepo, tmp_path, cap_db):
    """If failures did not count, the cap would never trigger."""
    bad = pipeline.sandbox_runner.SandboxResult(False, "container died", "sb", 1)
    monkeypatch.setattr(pipeline.sandbox_runner, "run_in_sandbox", lambda *a, **k: bad)

    pipeline.run_issue(
        FakeTwenty(),
        ISSUE,
        monorepo=monorepo,
        target_repo="shopify-app-loyalty-api",
        worktree_root=tmp_path / "wt",
        statuses=STATUSES,
    )

    assert cap_db.attempt_status("issue-1", "BLOY-2").failed == 1


def test_a_successful_run_leaves_the_counter_alone(monkeypatch, monorepo, tmp_path, cap_db):
    ok = pipeline.sandbox_runner.SandboxResult(True, "đã sửa", "sb", 0)
    monkeypatch.setattr(pipeline.sandbox_runner, "run_in_sandbox", lambda *a, **k: ok)
    monkeypatch.setattr(pipeline.workspace, "has_changes", lambda space: True)
    monkeypatch.setattr(pipeline.workspace, "diffstat", lambda space: " a | 1 +")
    monkeypatch.setattr(
        pipeline.workspace,
        "commit_and_push",
        lambda space, **kw: {"ok": True, "merge_request_url": "https://gitlab/mr/1"},
    )

    outcome = pipeline.run_issue(
        FakeTwenty(),
        ISSUE,
        monorepo=monorepo,
        target_repo="shopify-app-loyalty-api",
        worktree_root=tmp_path / "wt",
        statuses=STATUSES,
    )

    assert outcome.ok is True
    assert cap_db.attempt_status("issue-1", "BLOY-2").failed == 0


def test_the_run_record_follows_the_pipeline(monkeypatch, monorepo, tmp_path, cap_db):
    """The dashboard reads these rows, so each run has to leave one behind."""
    ok = pipeline.sandbox_runner.SandboxResult(True, "đã sửa", "sb-7", 0)
    monkeypatch.setattr(pipeline.sandbox_runner, "run_in_sandbox", lambda *a, **k: ok)
    monkeypatch.setattr(pipeline.workspace, "has_changes", lambda space: True)
    monkeypatch.setattr(pipeline.workspace, "diffstat", lambda space: " a | 1 +")
    monkeypatch.setattr(
        pipeline.workspace,
        "commit_and_push",
        lambda space, **kw: {"ok": True, "merge_request_url": "https://gitlab/mr/2"},
    )

    outcome = pipeline.run_issue(
        FakeTwenty(),
        ISSUE,
        monorepo=monorepo,
        target_repo="shopify-app-loyalty-api",
        worktree_root=tmp_path / "wt",
        statuses=STATUSES,
    )

    run = cap_db.get_run(outcome.run_id)
    assert run is not None
    assert (run.state, run.stage) == ("success", "done")
    assert run.merge_request_url == "https://gitlab/mr/2"
    assert run.log_path and run.log_path.endswith(f"{outcome.run_id}.jsonl")


def test_the_sandbox_is_told_the_run_id_so_it_can_stream(monkeypatch, monorepo, tmp_path, cap_db):
    """Without the run id there is no log file, and no live view."""
    seen = {}

    def fake_sandbox(prompt, worktree, **kwargs):
        seen.update(kwargs)
        return pipeline.sandbox_runner.SandboxResult(False, "x" * 300, "sb", 1)

    monkeypatch.setattr(pipeline.sandbox_runner, "run_in_sandbox", fake_sandbox)

    outcome = pipeline.run_issue(
        FakeTwenty(),
        ISSUE,
        monorepo=monorepo,
        target_repo="shopify-app-loyalty-api",
        worktree_root=tmp_path / "wt",
        statuses=STATUSES,
    )

    assert seen.get("run_id") == outcome.run_id


def test_the_pipeline_needs_no_bam_to_run():
    """The pipeline moved into the standalone service, so it must not need BAM.

    Replaces the old check that a routine offered a "sandbox" execution mode:
    that choice no longer exists, because the sandbox path is now the only one.
    """
    from bloy_dev_agent.features import pipeline as module

    source = Path(module.__file__).read_text(encoding="utf-8")

    assert not re.search(r"^\s*from\s+core\.", source, re.M)
    assert module.DEFAULT_BLOCKED_STATUS != module.DEFAULT_SOURCE_STATUS, (
        "a blocked issue returned to the source column would be re-blocked forever"
    )


# ---------------------------------------------------------------------------
# A timed-out move means "unknown", not "no"
# ---------------------------------------------------------------------------


class TimingOutTwenty(FakeTwenty):
    """Applies the change server-side, then reports a timeout to the caller."""

    def __init__(self, applied_status="s-wip"):
        super().__init__()
        self.applied_status = applied_status

    def update_record(self, obj, record_id, payload):
        from bloy_dev_agent.features.twenty.client import TwentyError

        self.updates.append((record_id, payload))
        raise TwentyError("Could not reach Twenty: timed out")

    def get_record(self, obj, record_id, **kw):
        return {"id": record_id, "statusId": self.applied_status}


def test_a_timed_out_move_is_confirmed_by_reading_it_back():
    """The bug this fixes stranded BLS-1077 in the working column.

    Twenty applied the PATCH and merely answered too late. Treating that as a
    failure left the issue where the source-column query could never see it.
    """
    client = TimingOutTwenty(applied_status="s-wip")

    assert pipeline._move(client, "issue-1", "s-wip") is True


def test_a_move_that_really_failed_is_still_reported_as_failed():
    client = TimingOutTwenty(applied_status="s-todo")

    assert pipeline._move(client, "issue-1", "s-wip") is False


class StrandedBoard(FakeTwenty):
    """A board with one issue parked in the working column."""

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def list_records(self, obj, **kw):
        if obj == "issueStatuses":
            return [
                {"id": "s-wip", "name": "In Progress", "projectId": "p-1"},
                {"id": "s-todo", "name": "Todo", "projectId": "p-1"},
            ]
        return self.rows


def test_the_sweep_returns_a_stranded_issue_to_the_source_column():
    """A lock nobody holds is a deadlock: the board silently stops feeding work."""
    client = StrandedBoard([{"id": "i-9", "issueKey": "BLS-1077"}])

    released = pipeline.release_stranded_issues(client, project_id="p-1")

    assert released == ["BLS-1077"]
    assert client.updates == [("i-9", {"statusId": "s-todo"})]


def test_the_sweep_leaves_a_live_run_alone():
    """Releasing an issue a running pass owns would hand it to a second pass."""
    client = StrandedBoard([{"id": "i-9", "issueKey": "BLS-1077"}])

    released = pipeline.release_stranded_issues(
        client, project_id="p-1", active_issue_ids={"i-9"}
    )

    assert released == []
    assert client.updates == []


# ---------------------------------------------------------------------------
# Advice mode: the answer is the deliverable, not a merge request
# ---------------------------------------------------------------------------


ADVICE_ISSUE = {
    "id": "issue-adv",
    "issueKey": "BLOY-9",
    "title": "BLS-1077: bỏ viền card VIP tier, căn giữa",
    "description": "Deliverable: snippet\nKhách muốn bỏ line quanh text và căn giữa.",
}


def _advice_run(monkeypatch, monorepo, tmp_path, *, output):
    client = FakeTwenty()
    seen = {}

    def fake_sandbox(prompt, worktree, **kwargs):
        seen["implement"] = kwargs.get("implement")
        seen["prompt"] = prompt
        return pipeline.sandbox_runner.SandboxResult(True, output, "sb-adv", 0)

    monkeypatch.setattr(pipeline.sandbox_runner, "run_in_sandbox", fake_sandbox)
    monkeypatch.setattr(
        pipeline.workspace,
        "commit_and_push",
        lambda *a, **k: pytest.fail("advice mode must never push or open an MR"),
    )
    outcome = pipeline.run_issue(
        client,
        ADVICE_ISSUE,
        monorepo=monorepo,
        target_repo="shopify-app-loyalty-api",
        worktree_root=tmp_path / "wt",
        statuses=STATUSES,
    )
    return client, outcome, seen


def test_a_snippet_task_succeeds_without_changing_any_file(monkeypatch, monorepo, tmp_path):
    """Editing the app's CSS would restyle every merchant, so no diff is correct.

    Before this mode existed the pipeline scored such a run as `no-change` →
    failed, which punished the agent for doing the right thing.
    """
    answer = "```css\n.bloy-page__card { border: none; text-align: center; }\n```"

    client, outcome, _ = _advice_run(monkeypatch, monorepo, tmp_path, output=answer)

    assert (outcome.ok, outcome.stage) == (True, "done")
    assert outcome.advice is True
    assert outcome.merge_request_url == ""
    assert outcome.moved_to == "In Review"


def test_the_snippet_reaches_the_reviewer_in_the_comment(monkeypatch, monorepo, tmp_path):
    answer = "```css\n.bloy-page__card { border: none; }\n```"

    client, _, _ = _advice_run(monkeypatch, monorepo, tmp_path, output=answer)

    # The stored payload is blocknote JSON, which escapes non-ASCII; decode it
    # rather than asserting against the escaped form.
    from bloy_dev_agent.features.twenty import mapping

    posted = mapping.blocknote_to_text(ast.literal_eval(client.comments[0])["bodyV2"])
    assert "border: none" in posted
    assert "dán vào theme khách" in posted


def test_advice_mode_runs_the_agent_read_only(monkeypatch, monorepo, tmp_path):
    """It must not be able to edit the repo even by accident."""
    _, _, seen = _advice_run(monkeypatch, monorepo, tmp_path, output="x" * 50)

    assert seen["implement"] is False
    assert "Do NOT edit any file" in seen["prompt"]


def test_an_empty_answer_is_still_a_failure(monkeypatch, monorepo, tmp_path):
    """No diff AND no answer means the run produced nothing at all."""
    _, outcome, _ = _advice_run(monkeypatch, monorepo, tmp_path, output="   ")

    assert outcome.ok is False


def test_a_normal_ticket_is_unaffected(monkeypatch, monorepo, tmp_path):
    """Only the marker switches modes; everything else still opens an MR."""
    ok = pipeline.sandbox_runner.SandboxResult(True, "đã sửa", "sb-1", 0)

    _, outcome = _run(monkeypatch, monorepo, tmp_path, sandbox_result=ok, changed=True)

    assert (outcome.advice, outcome.merge_request_url) == (False, "https://gitlab/mr/9")
