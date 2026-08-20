"""The full path: Twenty issue -> worktree -> sandbox -> merge request.

This is the *implement* pipeline. :mod:`runner` remains the read-only one — it
runs the agent against the host checkout and posts a plan. The difference is
not the prompt but the containment: nothing here writes to a repository you
have open, and nothing here can reach a credential.

Order of operations, and why:

1. **Claim** the issue by moving it out of the source column, before any work.
   A second pass then cannot pick up the same issue; the claim is the lock.
2. **Worktree** on a fresh branch, so the change is isolated and abandonable.
3. **Sandbox** writes code with only that worktree mounted read-write.
4. **Host** commits, pushes and opens the merge request over the existing SSH
   key — the one thing the container is never given.
5. **Report** back on the issue, then move it to the review or the error column.

Step 4 stays on the host even though it would be one line shorter inside the
container. That single line is the whole security boundary.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from bloy_dev_agent import store
from bloy_dev_agent.features import agent_log, sandbox_runner, skill_packs, workspace
from bloy_dev_agent.features.twenty import mapping
from bloy_dev_agent.features.twenty.client import TwentyClient, TwentyError
from bloy_dev_agent.models import BloyPipelineRun

logger = logging.getLogger(__name__)


def default_monorepo() -> Path:
    """Monorepo root, overridable per host via ``BLOY_MONOREPO``.

    Read at call time rather than baked in as a module constant: this module
    is imported before ``service.load_env()`` fills ``os.environ`` from
    ``BLOY_DEV_AGENT/.env``, so a plain constant would miss that file.
    """
    return Path(os.environ.get("BLOY_MONOREPO", "/home/bss-group/BLOY"))


DEFAULT_TARGET_REPO = "shopify-app-loyalty-api"

DEFAULT_SOURCE_STATUS = "Todo"
DEFAULT_WORKING_STATUS = "In Progress"
DEFAULT_DONE_STATUS = "In Review"

#: Where a blocked issue goes. Deliberately *not* the source column: putting it
#: back where it came from would let the next pass pick it up, run the cap check
#: again and re-block it forever, which is a loop with no work in it.
DEFAULT_BLOCKED_STATUS = "Backlog"

#: Where a failed (non-blocked) run goes. Used to be the same column as
#: DEFAULT_SOURCE_STATUS, which meant a failed issue was picked straight back
#: up by the very next pass and the whole pipeline reran from scratch — a
#: fresh run_id, a fresh comment, every time. That silent auto-retry-of-the-
#: whole-issue is what turned into comment spam. Retrying is now a human
#: decision (drag the card back to the source column, "Reset attempts" first
#: if the cap was hit) — the only retry that still happens automatically is
#: the in-place sandbox-stage loop inside run_issue.
DEFAULT_ERROR_STATUS = DEFAULT_BLOCKED_STATUS

#: Temporary: paused because the bug above made every silent auto-retry post
#: its own comment to Twenty ("spam"). Flip back to True once the fix above
#: has been confirmed to actually stop the spam in production.
COMMENTS_ENABLED = False


def project_id_of(record: dict) -> str:
    return str(record.get("projectId") or "")


@dataclass
class PipelineOutcome:
    key: str
    ok: bool
    stage: str = ""
    branch: str = ""
    merge_request_url: str = ""
    sandbox_id: str = ""
    changed: str = ""
    moved_to: str = ""
    detail: str = ""
    #: Row id in ``plugin_bloy_pipeline_run``; empty when bookkeeping failed.
    run_id: str = ""
    #: True when the attempt cap refused to start this issue at all.
    blocked: bool = False
    #: True when the run gave up before the sandbox started, so it cost nothing
    #: and must not count against the cap.
    aborted: bool = False
    #: True when the deliverable was an answer to hand over, not a merge request.
    advice: bool = False
    #: Lockfiles the run touched and that were reverted before committing.
    discarded: tuple[str, ...] = ()
    #: One entry per sub-project that produced a merge request. A customer
    #: change often spans the API rule and the CMS screen, and half a fix is
    #: worse than none — the reviewer needs both links side by side.
    merge_requests: tuple[tuple[str, str], ...] = ()
    attempt: int = 0


def statuses_by_name(client: TwentyClient, project_id: str) -> dict[str, str]:
    records = client.list_records("issueStatuses", limit=60)
    return {
        str(record.get("name") or ""): str(record.get("id") or "")
        for record in records
        if str(record.get("projectId") or "") == project_id
    }


def _move(client: TwentyClient, issue_id: str, status_id: str) -> bool:
    """Move an issue to a status, confirming the result when the call errors.

    A timed-out PATCH means *unknown*, not *no*: Twenty may have applied the
    change and merely answered too late. Treating it as a failure stranded
    BLS-1077 in the working column, where the source-column query could never
    see it again — so the outcome is read back rather than assumed.
    """
    try:
        client.update_record("issues", issue_id, {"statusId": status_id})
        return True
    except TwentyError as exc:
        logger.warning("bloy_dev_agent: could not move %s: %s", issue_id, exc.message)

    try:
        record = client.get_record("issues", issue_id)
    except TwentyError:
        return False

    applied = str((record or {}).get("statusId") or "") == status_id
    if applied:
        logger.info(
            "bloy_dev_agent: move of %s did apply despite the error; continuing",
            issue_id,
        )
    return applied


def release_stranded_issues(
    client: TwentyClient,
    *,
    project_id: str,
    working_status: str = DEFAULT_WORKING_STATUS,
    source_status: str = DEFAULT_SOURCE_STATUS,
    active_issue_ids: set[str] | None = None,
) -> list[str]:
    """Return issues sitting in the working column with nothing working on them.

    The working column is a lock, and a lock nobody holds is a deadlock: the
    source-column query cannot see those issues, so they are never picked up
    again and the board silently stops feeding the agent. This has bitten three
    times now — a killed process, a restarted service, and a timed-out claim —
    so the sweep runs on every boot rather than being fixed case by case.

    ``active_issue_ids`` are the issues a live run still owns; they are skipped.
    """
    statuses = statuses_by_name(client, project_id)
    working_id = statuses.get(working_status)
    source_id = statuses.get(source_status)
    if not working_id or not source_id:
        return []

    owned = active_issue_ids or set()
    released: list[str] = []
    for record in client.list_records(
        "issues", filter_expression=f'statusId[eq]:"{working_id}"', limit=60, depth=1
    ):
        issue_id = str(record.get("id") or "")
        if not issue_id or issue_id in owned:
            continue
        if _move(client, issue_id, source_id):
            key = str(record.get("issueKey") or issue_id)
            released.append(key)
            logger.info(
                "bloy_dev_agent: %s bị kẹt ở %s, đã trả về %s",
                key, working_status, source_status,
            )
    return released


def _comment(client: TwentyClient, issue_id: str, text: str) -> None:
    if not COMMENTS_ENABLED:
        return
    try:
        client.create_record(
            "issueComments",
            {"issueId": issue_id, "bodyV2": mapping.text_to_blocknote(text)},
        )
    except TwentyError as exc:
        logger.warning("bloy_dev_agent: could not comment on %s: %s", issue_id, exc.message)


def _report(outcome: PipelineOutcome, issue_key: str, answer: str = "") -> str:
    """The comment a reviewer reads on the issue."""
    if outcome.advice and outcome.ok:
        return (
            f"Dev Agent đã phân tích {issue_key}. Deliverable là đoạn code gửi dev "
            f"dán vào theme khách — KHÔNG sửa code app, vì sửa app sẽ đổi cho mọi "
            f"merchant.\n\n{answer}\n\nSandbox: {outcome.sandbox_id}"
        )

    if outcome.ok:
        lines = [
            f"Dev Agent đã xử lý {issue_key} và mở merge request.",
            "",
            f"Branch: {outcome.branch}",
        ]
        if outcome.merge_requests:
            lines.append("Merge request:")
            lines += [f"  {repo}: {url}" for repo, url in outcome.merge_requests]
        elif outcome.merge_request_url:
            lines.append(f"Merge request: {outcome.merge_request_url}")
        if outcome.changed:
            lines += ["", "Thay đổi:", outcome.changed]
        if outcome.discarded:
            lines += [
                "",
                "Đã bỏ khỏi commit (lockfile không được vào MR): "
                + ", ".join(outcome.discarded),
            ]
        if answer:
            # The agent's own report — the business rule it derived and the
            # assumptions it could not verify. Without this the reviewer sees a
            # diffstat and a link, and has no idea where the risk is. One ticket
            # shipped an unworkable fix built on a confidently wrong premise that
            # was stated in exactly this report and never reached anyone.
            lines += ["", "--- Báo cáo của agent ---", answer.strip()]
        lines += ["", f"Sandbox: {outcome.sandbox_id}", "", "Cần người review trước khi merge."]
        return "\n".join(lines)

    if outcome.blocked:
        return (
            f"Dev Agent ĐÃ DỪNG {issue_key} — hết số lần thử.\n\n{outcome.detail}"
        )

    return (
        f"Dev Agent không hoàn thành được {issue_key} "
        f"(lần thử {outcome.attempt}, dừng ở bước: {outcome.stage}).\n\n"
        f"{outcome.detail}"
    )


def run_issue(
    client: TwentyClient,
    record: dict,
    *,
    monorepo: Path | None = None,
    target_repo: str = DEFAULT_TARGET_REPO,
    worktree_root: Path | None = None,
    statuses: dict[str, str],
    working_status: str = DEFAULT_WORKING_STATUS,
    done_status: str = DEFAULT_DONE_STATUS,
    error_status: str = DEFAULT_ERROR_STATUS,
    timeout_minutes: int = sandbox_runner.DEFAULT_TIMEOUT_MINUTES,
    create_mr: bool = True,
    blocked_status: str = DEFAULT_BLOCKED_STATUS,
    skill_packs_root: Path = skill_packs.DEFAULT_SKILLS_ROOT,
    enabled_skill_names: tuple[str, ...] = (),
) -> PipelineOutcome:
    """Take one issue all the way to a merge request."""
    monorepo = monorepo if monorepo is not None else default_monorepo()
    worktree_root = (
        worktree_root if worktree_root is not None else workspace.default_worktree_root()
    )
    issue = mapping.normalize_issue(record)

    # --- attempt cap ------------------------------------------------------
    # Checked before the claim, so a blocked issue is not even moved out of its
    # column: an agent that has failed the ceiling number of times will fail
    # again, and each pass costs a container plus a full agent session.
    attempts = store.attempt_status(issue.id, issue.key)
    if attempts.exhausted:
        detail = (
            f"Đã thử {attempts.failed} lần và đều thất bại (giới hạn "
            f"{attempts.limit}). Dừng lại để không tốn thêm chi phí — cần người "
            f"xem lại rồi bấm Reset attempts."
        )
        run_id = store.record_blocked(
            issue_id=issue.id,
            issue_key=issue.key,
            issue_title=issue.title,
            project_id=project_id_of(record),
            target_repo=target_repo,
            attempt=attempts.failed + 1,
            detail=detail,
        )
        outcome = PipelineOutcome(
            issue.key,
            False,
            stage="blocked",
            detail=detail,
            run_id=run_id,
            blocked=True,
            attempt=attempts.failed + 1,
        )
        return _finish(client, issue, outcome, statuses, blocked_status or error_status)

    attempt_no = attempts.failed + 1
    run_id = store.start_run(
        issue_id=issue.id,
        issue_key=issue.key,
        issue_title=issue.title,
        project_id=project_id_of(record),
        target_repo=target_repo,
        attempt=attempt_no,
    )
    # The log is named after the run, so its path is only knowable once the row
    # exists. Stamping it now lets the page find the stream while it is written.
    store.set_stage(
        run_id, "claim", log_path=str(agent_log.host_log_path(worktree_root, run_id))
    )

    claimed = statuses.get(working_status)
    if claimed and not _move(client, issue.id, claimed):
        outcome = PipelineOutcome(
            issue.key,
            False,
            stage="claim",
            detail="không claim được issue",
            run_id=run_id,
            attempt=attempt_no,
        )
        store.finish_run(
            run_id, state=BloyPipelineRun.STATE_ABORTED, stage="claim", detail=outcome.detail
        )
        return outcome

    # --- worktree ---------------------------------------------------------
    # A ticket may name several sub-projects; the first one is where the agent
    # starts and the rest are prepared beside it. They all live under the same
    # mounted root, so the container sees every one of them without extra
    # plumbing — only the prompt has to say they exist.
    store.set_stage(run_id, "worktree")
    repos = mapping.wanted_repos(issue, workspace.KNOWN_REPOS) or [target_repo]
    try:
        spaces = [
            workspace.prepare(
                issue.key, monorepo=monorepo, repo=name, root=worktree_root
            )
            for name in repos
        ]
        space = spaces[0]
    except workspace.WorkspaceError as exc:
        outcome = PipelineOutcome(
            issue.key, False, stage="worktree", detail=str(exc),
            run_id=run_id, attempt=attempt_no, aborted=True,
        )
        return _finish(client, issue, outcome, statuses, error_status)

    # --- sandbox ----------------------------------------------------------
    # The prompt must name the path *inside* the container. Handing over the
    # host path sends the agent looking for a directory that does not exist
    # there, and it gives up in seconds without touching a file.
    store.set_stage(run_id, "sandbox", branch=space.branch)
    advice = mapping.wants_advice(issue)
    workdirs = [
        sandbox_runner.container_path(s.path, worktree_root) for s in spaces
    ]
    # Resolved from the catalog each run, rather than trusting stale names in
    # settings: a pack removed or renamed from the store since it was enabled
    # should quietly drop out, not break the run.
    enabled_packs = [
        pack
        for pack in skill_packs.list_packs(skill_packs_root)
        if pack.name in enabled_skill_names
    ]
    prompt = mapping.build_prompt(
        issue,
        workdirs[0],
        implement=not advice,
        monorepo=sandbox_runner.MONOREPO_MOUNT,
        advice=advice,
        extra_workdirs=workdirs[1:],
        enabled_skills=[(p.name, p.description) for p in enabled_packs],
    )
    # Retry *this stage*, in place, same run_id — not a new attempt, not a new
    # comment. A transient sandbox failure ("AI gặp lỗi ở process 2") used to
    # mean the whole issue got requeued and rerun from scratch; now only this
    # stage retries, and only the final result of the loop is reported.
    stage_retries = store.sandbox_stage_retries()
    for stage_attempt in range(1, stage_retries + 1):
        result = sandbox_runner.run_in_sandbox(
            prompt,
            space.path,
            worktree_root=worktree_root,
            timeout_minutes=timeout_minutes,
            implement=not advice,
            run_id=run_id,
            monorepo=monorepo,
            enabled_skills=enabled_packs,
            skill_packs_root=skill_packs_root,
        )
        if result.ok or stage_attempt == stage_retries:
            break
        logger.info(
            "bloy_dev_agent: sandbox thất bại ở %s (lần %d/%d trong cùng run "
            "%s), thử lại ngay tại chỗ",
            issue.key, stage_attempt, stage_retries, run_id,
        )
    store.set_stage(run_id, "verify", sandbox_id=result.sandbox_id, output=result.output)
    if not result.ok:
        outcome = PipelineOutcome(
            issue.key,
            False,
            stage="sandbox",
            branch=space.branch,
            sandbox_id=result.sandbox_id,
            detail=result.output[:2000] or "sandbox không trả về gì",
            run_id=run_id,
            attempt=attempt_no,
        )
        return _finish(client, issue, outcome, statuses, error_status)

    if advice:
        # The deliverable is the answer, not a diff. Skipping the change check
        # here is the whole point: a cosmetic, per-merchant request must not edit
        # the app, so "changed nothing" is the correct outcome, not a failure.
        outcome = PipelineOutcome(
            issue.key,
            bool(result.output.strip()),
            stage="done" if result.output.strip() else "sandbox",
            branch=space.branch,
            sandbox_id=result.sandbox_id,
            detail="" if result.output.strip() else "agent không trả về nội dung nào",
            run_id=run_id,
            attempt=attempt_no,
            advice=True,
        )
        return _finish(
            client, issue, outcome, statuses,
            done_status if outcome.ok else error_status,
            answer=result.output,
        )

    # Drop lockfile churn before judging whether the run changed anything, so a
    # run whose ONLY output was a lockfile is correctly reported as no-change.
    discarded: list[str] = []
    for candidate in spaces:
        discarded += [f"{candidate.repo}/{name}"
                      for name in workspace.discard_lockfile_changes(candidate)]

    touched = [candidate for candidate in spaces if workspace.has_changes(candidate)]

    if not touched:
        outcome = PipelineOutcome(
            issue.key,
            False,
            stage="no-change",
            branch=space.branch,
            sandbox_id=result.sandbox_id,
            detail=(
                (
                    "Agent chỉ thay đổi lockfile ("
                    + ", ".join(discarded)
                    + "), đã bỏ — lockfile không được vào MR.\n\n"
                    if discarded
                    else "Agent chạy xong nhưng không sửa file nào.\n\n"
                )
                + "Nội dung agent trả về:\n\n"
                + result.output[:1500]
            ),
            run_id=run_id,
            attempt=attempt_no,
        )
        return _finish(client, issue, outcome, statuses, error_status)

    changed = "\n".join(
        f"[{candidate.repo}]\n{workspace.diffstat(candidate)}" for candidate in touched
    )

    # --- host: commit, push, merge request --------------------------------
    store.set_stage(run_id, "commit", changed=changed)
    merge_requests: list[tuple[str, str]] = []
    failures: list[str] = []
    for candidate in touched:
        try:
            push = workspace.commit_and_push(
                candidate,
                title=f"{issue.key}: {issue.title}"[:120],
                body=(
                    f"Dev Agent tự động thực hiện {issue.key}.\n\n"
                    f"Sandbox: {result.sandbox_id}"
                ),
                create_mr=create_mr,
            )
        except workspace.WorkspaceError as exc:
            failures.append(f"{candidate.repo}: {exc}")
            continue
        if push.get("ok"):
            merge_requests.append(
                (candidate.repo, str(push.get("merge_request_url") or ""))
            )
        else:
            failures.append(f"{candidate.repo}: {push.get('detail') or 'push thất bại'}")

    # Partial success is still a failure to report: a reviewer who sees one MR
    # and no warning would merge half a change spanning two repositories.
    if failures:
        outcome = PipelineOutcome(
            issue.key,
            False,
            stage="push",
            branch=space.branch,
            changed=changed,
            sandbox_id=result.sandbox_id,
            detail="; ".join(failures),
            run_id=run_id,
            attempt=attempt_no,
            merge_requests=tuple(merge_requests),
            discarded=tuple(discarded),
        )
        return _finish(client, issue, outcome, statuses, error_status)

    outcome = PipelineOutcome(
        issue.key,
        True,
        stage="done",
        branch=space.branch,
        merge_request_url=merge_requests[0][1] if merge_requests else "",
        sandbox_id=result.sandbox_id,
        changed=changed,
        run_id=run_id,
        attempt=attempt_no,
        discarded=tuple(discarded),
        merge_requests=tuple(merge_requests),
    )
    return _finish(client, issue, outcome, statuses, done_status, answer=result.output)


def _finish(client, issue, outcome: PipelineOutcome, statuses, status_name: str,
            answer: str = ""):
    """Report to Twenty, move the issue, and close the run record out.

    Closing the record here rather than at each call site is what keeps the
    attempt counter honest: every path that ends a run goes through this
    function, so no failure can slip by uncounted and quietly reset the cap.
    """
    _comment(client, issue.id, _report(outcome, issue.key, answer))
    target = statuses.get(status_name)
    if target and _move(client, issue.id, target):
        outcome.moved_to = status_name

    if outcome.run_id and not outcome.blocked:
        store.finish_run(
            outcome.run_id,
            state=(
                BloyPipelineRun.STATE_SUCCESS
                if outcome.ok
                else (
                    BloyPipelineRun.STATE_ABORTED
                    if outcome.aborted
                    else BloyPipelineRun.STATE_FAILED
                )
            ),
            stage=outcome.stage,
            detail=outcome.detail,
            branch=outcome.branch or None,
            merge_request_url=outcome.merge_request_url or None,
            merge_requests_json=(
                json.dumps([list(pair) for pair in outcome.merge_requests])
                if outcome.merge_requests
                else None
            ),
            sandbox_id=outcome.sandbox_id or None,
            changed=outcome.changed or None,
        )
    return outcome


def run_pass(
    client: TwentyClient,
    *,
    project_id: str,
    monorepo: Path | None = None,
    target_repo: str = DEFAULT_TARGET_REPO,
    source_status: str = DEFAULT_SOURCE_STATUS,
    working_status: str = DEFAULT_WORKING_STATUS,
    done_status: str = DEFAULT_DONE_STATUS,
    error_status: str = DEFAULT_ERROR_STATUS,
    max_issues: int = 1,
    timeout_minutes: int = sandbox_runner.DEFAULT_TIMEOUT_MINUTES,
    create_mr: bool = True,
    blocked_status: str = DEFAULT_BLOCKED_STATUS,
    skill_packs_root: Path = skill_packs.DEFAULT_SKILLS_ROOT,
    enabled_skill_names: tuple[str, ...] = (),
) -> dict:
    """Pick issues from the source column and take each to a merge request."""
    monorepo = monorepo if monorepo is not None else default_monorepo()
    statuses = statuses_by_name(client, project_id)
    source_id = statuses.get(source_status)
    if not source_id:
        return {
            "error": f"Project has no status named {source_status!r}",
            "available": sorted(statuses),
        }

    candidates = client.list_records(
        "issues",
        filter_expression=f'statusId[eq]:"{source_id}"',
        order_by="createdAt",
        limit=max(1, min(max_issues, 3)),  # concurrency cap for this agent is 3
        depth=1,
    )

    outcomes = [
        run_issue(
            client,
            record,
            monorepo=monorepo,
            target_repo=target_repo,
            statuses=statuses,
            working_status=working_status,
            done_status=done_status,
            error_status=error_status,
            timeout_minutes=timeout_minutes,
            create_mr=create_mr,
            blocked_status=blocked_status,
            skill_packs_root=skill_packs_root,
            enabled_skill_names=enabled_skill_names,
        )
        for record in candidates
    ]
    return {
        "project_id": project_id,
        "target_repo": target_repo,
        "mode": "implement (sandbox + MR)",
        "picked": len(outcomes),
        "succeeded": sum(1 for o in outcomes if o.ok),
        "blocked": sum(1 for o in outcomes if o.blocked),
        "max_attempts": store.max_attempts(),
        "issues": [asdict(o) for o in outcomes],
    }
