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
from bloy_dev_agent.features import feedback as feedback_mod
from bloy_dev_agent.features.coding import loop as coding_loop
from bloy_dev_agent.features.twenty import mapping
from bloy_dev_agent.features.twenty.client import TwentyClient, TwentyError
from bloy_dev_agent.models import BloyPipelineRun
from bloy_dev_agent.staging_control import tokens as staging_tokens

logger = logging.getLogger(__name__)


def default_monorepo() -> Path:
    """Monorepo root, overridable per host via ``BLOY_MONOREPO``.

    Read at call time rather than baked in as a module constant: this module
    is imported before ``service.load_env()`` fills ``os.environ`` from
    ``BLOY_DEV_AGENT/.env``, so a plain constant would miss that file.
    """
    return Path(os.environ.get("BLOY_MONOREPO", "/home/bss-group/BLOY"))


def default_staging_control_url() -> str:
    """Base URL a sandbox uses to reach staging_control — the public tunnel
    hostname, not the ``172.17.0.1`` bind address staging_control listens on
    (that address is only reachable from inside a sandbox's own network
    namespace via the host's iptables pinhole, never something to hand to the
    agent as a URL to call). Same env-at-call-time reasoning as
    :func:`default_monorepo`.
    """
    return os.environ.get(
        "BLOY_STAGING_CONTROL_URL",
        "https://dev-bloy-staging-control.dev-bsscommerce.com",
    )


def default_storefront_url() -> str:
    """The real dev storefront a staging-verify run may open directly.

    Password-gated but with none of the Admin session-cookie complexity (see
    default_storefront_password) — a plain public dev-store gate, not a real
    credential, so a fixed fallback here is fine unlike anything Admin-auth
    related. Same env-at-call-time reasoning as default_monorepo.
    """
    return os.environ.get("BLOY_STOREFRONT_URL", "https://test-bloy-loyalty.myshopify.com")


def default_storefront_password() -> str:
    """The storefront password gate above — confirmed live to be a trivial,
    static, non-secret string with no bot-detection at all (unlike
    admin.shopify.com), so it is safe to hand to the agent directly in the
    prompt rather than treating it like a real credential.
    """
    return os.environ.get("BLOY_STOREFRONT_PASSWORD", "1")


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

#: Was paused because the bug above made every silent auto-retry post its own
#: comment to Twenty ("spam"). The fix above (separating error_status from
#: source_status, plus the in-place sandbox-stage retry that reuses one
#: run_id) has since run clean across many real passes with no re-pickup and
#: no spam — re-enabled so a reviewer actually sees why an issue moved.
COMMENTS_ENABLED = True


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


def _fetch_comments(client: TwentyClient, issue_id: str) -> list[mapping.NormalizedComment]:
    """Best-effort: a comment-fetch failure must not fail the whole run.

    The agent still has the ticket description without them — worse context,
    not no context — so this degrades to an empty thread rather than raising.
    """
    try:
        records = client.list_records(
            "issueComments", filter_expression=f'issueId[eq]:"{issue_id}"', limit=200
        )
    except TwentyError:
        logger.warning(
            "bloy_dev_agent: could not fetch comments for %s", issue_id, exc_info=True
        )
        return []
    return mapping.normalize_comments(records)


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


def _comment(client: TwentyClient, issue_id: str, text: str) -> str:
    """Post the report; return the new comment's id, or "" if none was posted."""
    if not COMMENTS_ENABLED or store.comments_disabled():
        return ""
    try:
        record = client.create_record(
            "issueComments",
            {
                "issueId": issue_id,
                # Marked so a later feedback pass can tell this service's own
                # reports from a reviewer's reply. Without the marker the report
                # posted at the end of a revision round would itself read as new
                # feedback, and the ticket would work itself forever — see
                # features/feedback.py.
                "bodyV2": mapping.text_to_blocknote(feedback_mod.stamp(text)),
            },
        )
        return str(record.get("id") or "")
    except TwentyError as exc:
        logger.warning("bloy_dev_agent: could not comment on %s: %s", issue_id, exc.message)
        return ""


def _attach_screenshots(
    client: TwentyClient,
    issue_id: str,
    comment_id: str,
    worktree_root: Path,
    run_id: str,
) -> None:
    """Upload whatever staging-verify screenshots this run actually saved.

    Best-effort, like ``_comment``: a run that changed real code and got a
    real merge request must never be reported as failed just because
    attaching evidence afterward hit a transient error.
    """
    names = agent_log.list_artifacts(worktree_root, run_id)
    if not names:
        return
    try:
        field_id = client.attachment_file_field_id()
    except TwentyError as exc:
        logger.warning("bloy_dev_agent: could not find attachments.file field: %s", exc.message)
        return
    directory = agent_log.host_artifacts_dir(worktree_root, run_id)
    for name in names:
        try:
            content = (directory / name).read_bytes()
            uploaded = client.upload_file(content, name, field_id)
            values = {"name": name, "targetIssueId": issue_id, "file": [
                {"fileId": uploaded.get("id"), "label": name}
            ]}
            if comment_id:
                values["targetIssueCommentId"] = comment_id
            client.create_record("attachments", values)
        except (OSError, TwentyError) as exc:
            logger.warning(
                "bloy_dev_agent: could not attach screenshot %s for run %s: %s",
                name, run_id, exc,
            )


def _report(outcome: PipelineOutcome, issue_key: str, answer: str = "") -> str:
    """The comment a reviewer reads on the issue."""
    if outcome.advice and outcome.ok:
        return (
            f"Dev Agent đã phân tích {issue_key} — KHÔNG sửa code app. Có thể là "
            f"đoạn code gửi dev dán vào theme khách (không đổi cho mọi merchant), "
            f"hoặc ticket đã giải quyết được bằng tính năng có sẵn — xem chi tiết "
            f"bên dưới.\n\n{answer}\n\nSandbox: {outcome.sandbox_id}"
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

    lines = [
        f"Dev Agent không hoàn thành được {issue_key} "
        f"(lần thử {outcome.attempt}, dừng ở bước: {outcome.stage}).",
        "",
        outcome.detail,
    ]
    # A loop that ran out of attempts still wrote code and still opened a merge
    # request; reporting only the stage name would leave the reviewer who has to
    # judge it with no link to what was actually produced.
    if outcome.merge_requests or outcome.merge_request_url:
        lines += ["", f"Branch: {outcome.branch}"]
        if outcome.merge_requests:
            lines.append("Merge request (CHƯA được evaluator xác nhận):")
            lines += [f"  {repo}: {url}" for repo, url in outcome.merge_requests]
        else:
            lines.append(
                f"Merge request (CHƯA được evaluator xác nhận): "
                f"{outcome.merge_request_url}"
            )
        if outcome.changed:
            lines += ["", "Thay đổi:", outcome.changed]
        if answer:
            lines += ["", "--- Báo cáo của agent ---", answer.strip()]
        lines += ["", "Cần người xem và quyết định: sửa tay, hoặc comment yêu "
                  "cầu thay đổi để Dev Agent làm lại."]
    return "\n".join(lines)


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
    feedback: mapping.NormalizedComment | None = None,
) -> PipelineOutcome:
    """Take one issue all the way to a merge request.

    ``feedback`` turns this into a **revision round**: a reviewer commented on
    a ticket this pipeline already reported, and their comment becomes the
    authoritative instruction. The worktree and branch are reused (``prepare``
    already does that), so the round continues the existing merge request
    instead of opening a second one for the same ticket — see
    :func:`run_feedback_pass` and :mod:`bloy_dev_agent.features.feedback`.
    """
    monorepo = monorepo if monorepo is not None else default_monorepo()
    worktree_root = (
        worktree_root if worktree_root is not None else workspace.default_worktree_root()
    )
    issue = mapping.normalize_issue(record)
    comments = _fetch_comments(client, issue.id)

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
        return _finish(
            client, issue, outcome, statuses, blocked_status or error_status,
            worktree_root=worktree_root,
        )

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
    # Every repo is always prepared, write access and all — never just the
    # one the ticket happened to name or the configured default. Found live,
    # repeatedly: a ticket whose fix belonged in shopify-app-loyalty-cms kept
    # burning attempts because the sandbox only had shopify-app-loyalty-api
    # mounted read-write; the agent could already SEE the right file through
    # the read-only monorepo mount, it just could not write there, so each
    # attempt correctly diagnosed "wrong repo" and still had to stop and
    # report it as a failure instead of just fixing it. A human then had to
    # notice the pattern and manually add a `Repos:` line to unblock the next
    # attempt. Preparing every KNOWN_REPOS worktree unconditionally removes
    # that whole failure mode — an unneeded worktree costs one cheap `git
    # worktree add`, not a full clone, and an empty merge request for a repo
    # the agent never touched is already a correct, silent no-op (see
    # build_prompt's own "if a sub-project turns out not to need changing,
    # leave it untouched" instruction and the has_changes-per-repo check
    # below `_finish` relies on either way).
    store.set_stage(run_id, "worktree")
    declared = mapping.wanted_repos(issue, workspace.KNOWN_REPOS)
    primary = declared[0] if declared else target_repo
    repos = [primary] + [name for name in workspace.KNOWN_REPOS if name != primary]
    # Staging (real deploy + a live Playwright session) stays opt-in and
    # narrow on purpose — unlike write access, it is a real cost (the single
    # shared staging slot, minutes of deploy time) and a real structural
    # privilege (network egress, a deploy token) that a ticket's own text
    # must never be able to grant itself; see touches_ui_repo's own
    # docstring. Basing it on `declared` (the ticket's own `Repos:` line, or
    # nothing) rather than the now-always-multi `repos` list keeps a plain
    # backend ticket from silently paying for a deploy+browser session it
    # never asked for just because every worktree is now mounted.
    staging_requested = mapping.touches_ui_repo(declared or [target_repo])
    try:
        space = workspace.prepare(
            issue.key, monorepo=monorepo, repo=repos[0], root=worktree_root
        )
        # The primary repo is required — nothing runs without it, so its own
        # failure still aborts the whole attempt below. Every OTHER repo is
        # best-effort: since they are now always requested rather than only
        # when a ticket names them, one being unpreparable (a bad checkout, a
        # repo missing on this particular host) must not take down a run that
        # never needed it — it just quietly has one fewer worktree available,
        # same as if it had never been in `repos` at all.
        spaces = [space]
        for name in repos[1:]:
            try:
                spaces.append(
                    workspace.prepare(issue.key, monorepo=monorepo, repo=name, root=worktree_root)
                )
            except workspace.WorkspaceError:
                logger.warning(
                    "bloy_dev_agent: could not prepare %s for %s; continuing without it",
                    name, issue.key, exc_info=True,
                )
    except workspace.WorkspaceError as exc:
        outcome = PipelineOutcome(
            issue.key, False, stage="worktree", detail=str(exc),
            run_id=run_id, attempt=attempt_no, aborted=True,
        )
        return _finish(client, issue, outcome, statuses, error_status, worktree_root=worktree_root)

    # --- sandbox ----------------------------------------------------------
    # The prompt must name the path *inside* the container. Handing over the
    # host path sends the agent looking for a directory that does not exist
    # there, and it gives up in seconds without touching a file.
    store.set_stage(run_id, "sandbox", branch=space.branch)
    workdirs = [
        sandbox_runner.container_path(s.path, worktree_root) for s in spaces
    ]

    # Staging is one shared environment — at most one run may hold it at a
    # time, so a second UI-touching ticket started while another is still
    # deploying is refused outright rather than silently racing it. Checked
    # here (not earlier) because it costs nothing before this point anyway,
    # and refusing after the claim would strand the issue in "In Progress"
    # were it checked any earlier than the stage that actually needs it.
    staging_ctx: mapping.StagingContext | None = None
    staging_token = ""
    if staging_requested:
        existing = staging_tokens.active()
        if existing is not None and existing.run_id != run_id:
            # aborted, not blocked: staging being busy is not this ticket's
            # fault and must not burn one of its attempt-cap slots (only
            # STATE_FAILED rows count there) — and "blocked" specifically
            # means "attempt cap exhausted" in the report text, which would
            # be a misleading message here.
            outcome = PipelineOutcome(
                issue.key,
                False,
                stage="staging-busy",
                branch=space.branch,
                detail=(
                    f"Staging đang bị giữ bởi run khác (issue {existing.issue_key}) "
                    "— thử lại sau."
                ),
                run_id=run_id,
                attempt=attempt_no,
                aborted=True,
            )
            return _finish(
                client, issue, outcome, statuses, error_status,
                worktree_root=worktree_root,
            )
        staging_token = staging_tokens.mint(
            run_id,
            issue_key=issue.key,
            worktrees={s.repo: s.path for s in spaces},
            ttl_minutes=timeout_minutes,
        )
        staging_ctx = mapping.StagingContext(
            control_base_url=default_staging_control_url(),
            artifacts_dir=agent_log.container_artifacts_dir(
                sandbox_runner.WORKTREE_MOUNT, run_id
            ),
            storefront_url=default_storefront_url(),
            storefront_password=default_storefront_password(),
        )

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
        implement=True,
        monorepo=sandbox_runner.MONOREPO_MOUNT,
        extra_workdirs=workdirs[1:],
        enabled_skills=[(p.name, p.description) for p in enabled_packs],
        staging=staging_ctx,
        comments=comments,
    )
    # A revision round puts the reviewer's own words in front of everything
    # else — see features/feedback.py for why their comment outranks the
    # description it contradicts.
    if feedback is not None:
        prompt = feedback_mod.revision_prompt(prompt, feedback)

    loop_result: coding_loop.LoopOutcome | None = None
    if store.loop_enabled():
        loop_result = _run_coding_loop(
            prompt=prompt,
            issue=issue,
            space=space,
            spaces=spaces,
            worktree_root=worktree_root,
            run_id=run_id,
            timeout_minutes=timeout_minutes,
            monorepo=monorepo,
            enabled_packs=enabled_packs,
            skill_packs_root=skill_packs_root,
            staging_requested=staging_requested,
            staging_token=staging_token,
        )
        result = sandbox_runner.SandboxResult(
            # "ok" here means the container produced work, not that the
            # evaluator accepted it: a capped or stalled loop still leaves a
            # real diff a reviewer should see, and treating that as a failed
            # sandbox would throw the work away and report nothing but a stage
            # name. Whether the evaluator accepted it decides the *column* the
            # ticket lands in, further down.
            ok=bool(loop_result.answer) and not loop_result.setup_error,
            output=loop_result.answer or loop_result.detail,
            sandbox_id=loop_result.sandbox_id,
        )
    else:
        # Legacy single-shot path, kept behind the switch: retry *this stage*,
        # in place, same run_id — not a new attempt, not a new comment. Note it
        # reruns the identical prompt, which is exactly the limitation the loop
        # above exists to fix (the evaluator tells the retry what was wrong).
        stage_retries = store.sandbox_stage_retries()
        for stage_attempt in range(1, stage_retries + 1):
            result = sandbox_runner.run_in_sandbox(
                prompt,
                space.path,
                worktree_root=worktree_root,
                timeout_minutes=timeout_minutes,
                implement=True,
                run_id=run_id,
                monorepo=monorepo,
                enabled_skills=enabled_packs,
                skill_packs_root=skill_packs_root,
                staging=staging_requested,
                staging_token=staging_token,
                agent_repos_root=workspace.default_agent_repos_root(),
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
        return _finish(client, issue, outcome, statuses, error_status, worktree_root=worktree_root)

    # Drop lockfile/generated-output churn before judging whether the run
    # changed anything, so a run whose only diff was a lockfile or a rebuilt
    # cdn-dist bundle is correctly reported as no-change instead of shipping
    # that churn into a real merge request.
    discarded: list[str] = []
    for candidate in spaces:
        discarded += [f"{candidate.repo}/{name}"
                      for name in workspace.discard_generated_changes(candidate)]

    touched = [candidate for candidate in spaces if workspace.has_changes(candidate)]

    if not touched and (
        mapping.is_snippet_deliverable(result.output)
        or mapping.is_no_change_needed(result.output)
    ):
        # The agent judged this ticket solvable without touching this repo —
        # either a theme-level snippet, or (found live) the ask was already
        # achievable with an existing feature — and declared so explicitly
        # (see IMPLEMENT_INSTRUCTIONS). "Changed nothing" is the correct
        # outcome here, not a failure. The deliverable is the answer itself,
        # not a diff.
        outcome = PipelineOutcome(
            issue.key,
            True,
            stage="done",
            branch=space.branch,
            sandbox_id=result.sandbox_id,
            run_id=run_id,
            attempt=attempt_no,
            advice=True,
        )
        return _finish(
            client, issue, outcome, statuses, done_status,
            answer=result.output, worktree_root=worktree_root,
        )

    if not touched:
        outcome = PipelineOutcome(
            issue.key,
            False,
            stage="no-change",
            branch=space.branch,
            sandbox_id=result.sandbox_id,
            detail=(
                (
                    "Agent chỉ thay đổi lockfile/generated-output ("
                    + ", ".join(discarded)
                    + "), đã bỏ — không được vào MR.\n\n"
                    if discarded
                    else "Agent chạy xong nhưng không sửa file nào.\n\n"
                )
                + "Nội dung agent trả về:\n\n"
                + result.output[:1500]
            ),
            run_id=run_id,
            attempt=attempt_no,
        )
        return _finish(client, issue, outcome, statuses, error_status, worktree_root=worktree_root)

    changed = "\n".join(
        f"[{candidate.repo}]\n{workspace.diffstat(candidate)}" for candidate in touched
    )

    # --- host: commit, push, merge request --------------------------------
    store.set_stage(run_id, "commit", changed=changed)
    merge_requests: list[tuple[str, str]] = []
    failures: list[str] = []
    # A revision round pushes to a branch that already has an open merge
    # request, so it must not ask GitLab to open another one for the same work.
    # The existing links are read back from the run that opened them, because
    # the report this round posts still has to give the reviewer something to
    # click (see store.last_merge_requests).
    existing = dict(store.last_merge_requests(issue.id)) if feedback is not None else {}
    open_mr = create_mr and feedback is None
    for candidate in touched:
        try:
            push = workspace.commit_and_push(
                candidate,
                title=f"{issue.key}: {issue.title}"[:120],
                body=(
                    (
                        f"Dev Agent sửa lại {issue.key} theo feedback của "
                        f"{feedback.author or 'reviewer'}.\n\n"
                        if feedback is not None
                        else f"Dev Agent tự động thực hiện {issue.key}.\n\n"
                    )
                    + f"Sandbox: {result.sandbox_id}"
                ),
                create_mr=open_mr,
            )
        except workspace.WorkspaceError as exc:
            failures.append(f"{candidate.repo}: {exc}")
            continue
        if push.get("ok"):
            merge_requests.append(
                (
                    candidate.repo,
                    # A revision push prints no URL (it opened nothing), so the
                    # one this branch already has is carried forward rather than
                    # reported as an empty link.
                    str(push.get("merge_request_url") or "")
                    or existing.get(candidate.repo, ""),
                )
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
        return _finish(client, issue, outcome, statuses, error_status, worktree_root=worktree_root)

    # The evaluator's verdict decides the column, not whether code was written.
    # An attempt that produced a real diff the evaluator would not accept still
    # opens its merge request — throwing the work away would leave a reviewer
    # with a stage name and nothing to look at — but it must not land in the
    # same column as verified work, or that column stops meaning anything.
    accepted = loop_result is None or loop_result.ok
    outcome = PipelineOutcome(
        issue.key,
        accepted,
        stage="done" if accepted else f"loop-{loop_result.outcome}",
        branch=space.branch,
        merge_request_url=merge_requests[0][1] if merge_requests else "",
        sandbox_id=result.sandbox_id,
        changed=changed,
        detail="" if accepted else "\n".join(loop_result.report_lines()),
        run_id=run_id,
        attempt=attempt_no,
        discarded=tuple(discarded),
        merge_requests=tuple(merge_requests),
    )
    answer = result.output
    if mapping.is_snippet_deliverable(answer) or mapping.is_no_change_needed(answer):
        # A real diff shipped AND the agent declared "no code needed" — a
        # contradiction (it should be one or the other). Surface it loudly
        # rather than let the heading sit as inert prose in a comment that
        # otherwise reads as an ordinary "opened a merge request" report.
        answer = (
            "⚠️ Agent tự khai không cần sửa code (\"DELIVERABLE: SNIPPET\" hoặc "
            "\"DELIVERABLE: NO CHANGE NEEDED\") nhưng lại sửa code thật trong "
            "repo — có thể mâu thuẫn, reviewer cần tự kiểm tra kỹ trước khi "
            "merge.\n\n"
        ) + answer
    if loop_result is not None:
        # Prepend what the loop actually did — how many attempts, what each
        # scored, which verify commands this service ran and whether they
        # passed. A reviewer deciding how carefully to read a merge request
        # needs that before the agent's own account of its work.
        answer = "\n".join(loop_result.report_lines()) + "\n\n" + answer
    return _finish(
        client, issue, outcome, statuses,
        done_status if accepted else (blocked_status or error_status),
        answer=answer, worktree_root=worktree_root,
    )


def _run_coding_loop(
    *,
    prompt: str,
    issue,
    space,
    spaces,
    worktree_root: Path,
    run_id: str,
    timeout_minutes: int,
    monorepo: Path,
    enabled_packs,
    skill_packs_root: Path,
    staging_requested: bool,
    staging_token: str,
) -> coding_loop.LoopOutcome:
    """Drive the coding loop for one run and persist what each attempt did.

    Synchronous on purpose: every caller in this module is, and
    ``sandbox_runner._run_coroutine`` already owns the "drive a coroutine from a
    thread that may or may not have a loop" problem — BAM's routine scheduler
    calls actions directly on its own event loop, which is how the very first
    live run failed with "cannot be called from a running event loop".
    """
    budget = store.loop_budget()

    def changed_repos() -> list[str]:
        """Which sub-projects this attempt actually touched.

        Read from the host side of the bind mount, so it sees the container's
        uncommitted work immediately. Narrows the verify commands to the repos
        that changed — an API-only ticket must not pay for the CMS test suite.
        """
        return [
            candidate.repo
            for candidate in spaces
            if workspace.has_changes(candidate)
        ]

    def on_stage(name: str, fields: dict) -> None:
        store.set_stage(run_id, name, **{
            key: value for key, value in fields.items()
            if key in {"sandbox_id"} and value
        })

    outcome = sandbox_runner._run_coroutine(
        lambda: coding_loop.run_loop(
            base_prompt=prompt,
            # The evaluator grades against the ticket, not against the whole
            # prompt: the prompt also carries repo layout, skills and staging
            # instructions, and an evaluator told to check all of that starts
            # failing tickets for not exercising a skill nobody asked for.
            objective=f"{issue.key}: {issue.title}\n\n{issue.body or ''}".strip(),
            worktree=space.path,
            worktree_root=worktree_root,
            run_id=run_id,
            budget=budget,
            per_attempt_minutes=timeout_minutes,
            monorepo=monorepo,
            enabled_skills=enabled_packs,
            skill_packs_root=skill_packs_root,
            staging=staging_requested,
            staging_token=staging_token,
            agent_repos_root=workspace.default_agent_repos_root(),
            verify_commands_raw=store.verify_commands_raw(),
            changed_repos=changed_repos,
            on_stage=on_stage,
        )
    )

    for record in outcome.attempts:
        store.record_attempt(
            run_id,
            attempt=record.attempt,
            verdict=record.verdict.verdict.value if record.verdict else "",
            score=record.score,
            missing=record.verdict.missing if record.verdict else "",
            generator_ok=record.generator_ok,
            tokens=record.tokens,
            cost_usd=record.cost_usd,
        )
        store.record_receipts(run_id, attempt=record.attempt, batch=record.receipts)

    logger.info(
        "bloy_dev_agent: vòng lặp %s kết thúc %r sau %d lần thử (%s)",
        issue.key, outcome.outcome, len(outcome.attempts), outcome.spend,
    )
    return outcome


def _finish(client, issue, outcome: PipelineOutcome, statuses, status_name: str,
            answer: str = "", worktree_root: Path | None = None):
    """Report to Twenty, move the issue, and close the run record out.

    Closing the record here rather than at each call site is what keeps the
    attempt counter honest: every path that ends a run goes through this
    function, so no failure can slip by uncounted and quietly reset the cap.
    The same reasoning covers the staging token: revoking it here, for every
    outcome, means no exit path (success, failure, or an early return before
    the sandbox even started) can leave one live past its own run.
    """
    staging_tokens.revoke(outcome.run_id)
    comment_id = _comment(client, issue.id, _report(outcome, issue.key, answer))
    if worktree_root is not None and outcome.run_id:
        _attach_screenshots(client, issue.id, comment_id, worktree_root, outcome.run_id)
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


def run_feedback_pass(
    client: TwentyClient,
    *,
    project_id: str,
    monorepo: Path | None = None,
    target_repo: str = DEFAULT_TARGET_REPO,
    review_statuses: tuple[str, ...] = (DEFAULT_DONE_STATUS, DEFAULT_BLOCKED_STATUS),
    working_status: str = DEFAULT_WORKING_STATUS,
    done_status: str = DEFAULT_DONE_STATUS,
    error_status: str = DEFAULT_ERROR_STATUS,
    blocked_status: str = DEFAULT_BLOCKED_STATUS,
    max_issues: int = 1,
    timeout_minutes: int = sandbox_runner.DEFAULT_TIMEOUT_MINUTES,
    skill_packs_root: Path = skill_packs.DEFAULT_SKILLS_ROOT,
    enabled_skill_names: tuple[str, ...] = (),
) -> dict:
    """Find reviewer feedback on already-reported tickets and act on it.

    This is the only place a human steers the pipeline, and it uses the thing
    reviewers already do: they comment on the ticket. A comment written *after*
    this service's own last report, by someone who is not this service, becomes
    the instruction for a revision round on the same branch and the same merge
    request.

    Both the review column and the blocked column are scanned. A reviewer
    correcting work the evaluator rejected is exactly as much a revision request
    as one correcting work it accepted — arguably more — and a ticket parked in
    the blocked column with a human explaining what went wrong is the single
    most useful input this pipeline can get.

    Every candidate is *claimed* in the database before any work starts, keyed
    by the comment id, so the poll running again five minutes later cannot start
    a second container for the same comment (see ``store.claim_feedback``).
    """
    if not store.feedback_enabled():
        return {"enabled": False, "picked": 0, "issues": []}

    monorepo = monorepo if monorepo is not None else default_monorepo()
    statuses = statuses_by_name(client, project_id)
    wanted = [statuses[name] for name in review_statuses if name in statuses]
    if not wanted:
        return {
            "error": f"Project has no status named any of {list(review_statuses)!r}",
            "available": sorted(statuses),
        }

    outcomes: list[PipelineOutcome] = []
    skipped = 0
    for status_id in wanted:
        if len(outcomes) >= max(1, max_issues):
            break
        try:
            candidates = client.list_records(
                "issues",
                filter_expression=f'statusId[eq]:"{status_id}"',
                order_by="updatedAt",
                limit=40,
                depth=1,
            )
        except TwentyError:
            logger.warning(
                "bloy_dev_agent: could not list issues in status %s", status_id,
                exc_info=True,
            )
            continue

        for record in candidates:
            if len(outcomes) >= max(1, max_issues):
                break
            issue = mapping.normalize_issue(record)
            comment = feedback_mod.pending(_fetch_comments(client, issue.id))
            if comment is None:
                continue
            if not store.claim_feedback(
                comment_id=comment.id,
                issue_id=issue.id,
                issue_key=issue.key,
                feedback=comment.body or "",
                author=comment.author or "",
            ):
                # Another pass (or an earlier tick of this one) already took it.
                skipped += 1
                continue

            logger.info(
                "bloy_dev_agent: %s có feedback mới từ %s, bắt đầu vòng sửa lại",
                issue.key, comment.author or "không rõ người",
            )
            outcome = run_issue(
                client,
                record,
                monorepo=monorepo,
                target_repo=target_repo,
                statuses=statuses,
                working_status=working_status,
                done_status=done_status,
                error_status=error_status,
                timeout_minutes=timeout_minutes,
                # Never on a revision round: the branch already has an open
                # merge request, and asking GitLab for a second one on the same
                # branch is how a ticket ends up with two.
                create_mr=False,
                blocked_status=blocked_status,
                skill_packs_root=skill_packs_root,
                enabled_skill_names=enabled_skill_names,
                feedback=comment,
            )
            if outcome.run_id:
                store.attach_feedback_run(comment.id, outcome.run_id)
            outcomes.append(outcome)

    return {
        "project_id": project_id,
        "mode": "feedback (revision round)",
        "picked": len(outcomes),
        "succeeded": sum(1 for o in outcomes if o.ok),
        "already_claimed": skipped,
        "issues": [asdict(o) for o in outcomes],
    }
