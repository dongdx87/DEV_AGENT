"""The loop driver: generator -> trusted verify -> evaluator -> decide, in one container.

This is the I/O half of the loop; :mod:`bloy_dev_agent.features.coding.controller`
holds the decision rules and touches nothing. The split exists so every rule
about when to stop is testable without a container or a model.

What one attempt is::

    generator turn      writes code in the worktree
    verify commands     THIS SERVICE runs the operator's command list in the
                        same container and stores receipts (the agent never
                        reports these — see receipts.py)
    evaluator turn      reads the diff, the receipts and the staging
                        screenshots, and returns a JSON verdict
    controller          pass -> done | fail -> another attempt carrying the
                        evaluator's "missing" text | out of budget -> stop

There is deliberately **no human approval step anywhere in here**. The pipeline
this belongs to is unattended: it claims a ticket, works it, and reports. A
person enters at the end, reading a merge request that an independent evaluator
already tried to reject — and, when they disagree, through the feedback round
in :mod:`bloy_dev_agent.features.feedback`, which starts a fresh loop with
their comment as the instruction.

One container for the whole loop, not one per attempt, for three reasons: the
evaluator has to see the generator's *uncommitted* diff, a retry has to
continue from work already on disk, and re-mounting plus re-installing the
login for every turn is pure overhead. The consequence is that the container's
own timeout now bounds the whole loop, so :func:`session_timeout_minutes`
scales it by the attempt cap — otherwise adding attempts would silently shrink
the time each one gets.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from bloy_dev_agent.features import agent_log, sandbox_runner
from bloy_dev_agent.features.coding import controller as ctrl
from bloy_dev_agent.features.coding import evaluator as ev
from bloy_dev_agent.features.coding import receipts as rc
from bloy_dev_agent.features.coding.budget import LoopBudget, LoopLedger
from bloy_dev_agent.features.coding.verdict import (
    Verdict,
    parse_verdict,
    unparsable,
    verdict_from_payload,
)

logger = logging.getLogger(__name__)

#: Ceiling on the container's lifetime however many attempts are configured. A
#: sandbox that lives for hours holds its mounts, its share of the OpenSandbox
#: server and (for staging) the single shared staging slot — the attempt cap is
#: meant to bound cost, not to become a way to book a machine all afternoon.
MAX_SESSION_MINUTES = 180


def session_timeout_minutes(per_attempt_minutes: int, max_attempts: int) -> int:
    """How long the container may live for a whole loop.

    Scaled by the attempt cap so each attempt keeps the wall time a single-shot
    run used to get; capped by :data:`MAX_SESSION_MINUTES` so a generous
    attempt setting cannot turn into an all-day container.
    """
    per_attempt = max(1, int(per_attempt_minutes or 1))
    attempts = max(1, int(max_attempts or 1))
    return min(MAX_SESSION_MINUTES, per_attempt * attempts)


@dataclass
class AttemptRecord:
    """What one attempt did, for the report and the run detail page."""

    attempt: int
    generator_ok: bool
    generator_answer: str = ""
    verdict: Verdict | None = None
    receipts: rc.ReceiptBatch | None = None
    tokens: int = 0
    cost_usd: float = 0.0

    @property
    def score(self) -> float:
        return self.verdict.score if self.verdict else 0.0


@dataclass
class LoopOutcome:
    """The result of a whole loop, for the pipeline to act on."""

    outcome: str
    detail: str = ""
    sandbox_id: str = ""
    #: The generator's own final answer from the attempt that decided the loop.
    #: This is what a reviewer reads under "Báo cáo của agent", so it must come
    #: from the attempt whose code is actually on disk — not from an earlier one.
    answer: str = ""
    attempts: list[AttemptRecord] = field(default_factory=list)
    spend: str = ""
    #: Set only when the container itself could not be prepared.
    setup_error: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == ctrl.OUTCOME_COMPLETE

    @property
    def needs_human(self) -> bool:
        return self.outcome in ctrl.HUMAN_OUTCOMES

    @property
    def last(self) -> AttemptRecord | None:
        return self.attempts[-1] if self.attempts else None

    def report_lines(self) -> list[str]:
        """Vietnamese summary of the loop, for the comment on the ticket.

        Written here rather than in the pipeline's ``_report`` because only the
        loop knows what each attempt scored and which receipts backed it — and
        a reviewer deciding whether to trust a merge request needs exactly that,
        not just "done".
        """
        label = ctrl.OUTCOME_LABELS.get(self.outcome, self.outcome)
        lines = [f"Vòng lặp AI: {label} sau {len(self.attempts)} lần thử."]
        if self.spend:
            lines.append(f"Chi phí: {self.spend}")
        for record in self.attempts:
            verdict = record.verdict
            grade = (
                f"{verdict.verdict.value} (score {verdict.score:.2f})"
                if verdict
                else "không có verdict"
            )
            note = "" if record.generator_ok else " — generator lỗi"
            lines.append(f"  Lần {record.attempt}: {grade}{note}")
            if record.receipts is not None:
                lines.append(f"    Verify: {record.receipts.summary()}")
            if verdict and verdict.missing and not verdict.passed:
                lines.append(f"    Còn thiếu: {verdict.missing[:400]}")
        if self.detail:
            lines += ["", self.detail]
        return lines


async def _collect_receipts(
    session: sandbox_runner.SandboxSession,
    *,
    verify_commands_raw: str,
    repos: list[str],
) -> rc.ReceiptBatch | None:
    """Run the operator's verify commands ourselves and push a projection in.

    ``None`` means no trusted runner was available for this attempt — either
    nothing is configured for the repos that changed, or the configured list is
    malformed. The evaluator is told which of those it is, and told not to claim
    a command passed either way.
    """
    if not repos:
        return None
    try:
        commands = rc.commands_for(verify_commands_raw, repos)
    except rc.VerifyContractError as exc:
        logger.warning("bloy_dev_agent: verify command list is unusable: %s", exc)
        return None
    if not commands:
        return None

    batch = rc.new_batch()
    for command in commands:
        # Each verify command runs in its own repo's worktree, as the agent
        # user, with the agent's PATH — a check that only passes as root, or
        # from the wrong directory, would never have reflected the run.
        workdir = f"{sandbox_runner.WORKTREE_MOUNT}/{command.repo}"
        started = time.monotonic()
        try:
            code, output = await session.run_as_agent(command.command, cwd=workdir)
            error = ""
        except Exception as exc:  # noqa: BLE001 — a failed check is data, not a crash
            code, output = -1, ""
            error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "bloy_dev_agent: verify %r did not run: %s", command.command, error
            )
        batch.receipts.append(
            rc.make_receipt(
                command,
                exit_code=code,
                output=output,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=error,
            )
        )

    await session.push_file(ev.RECEIPTS_PATH, rc.projection(batch))
    return batch


async def _read_verdict(
    session: sandbox_runner.SandboxSession, evaluator_answer: str
) -> Verdict:
    """The evaluator's verdict: the file first, its reply second, fail open third.

    File before text because a CLI agent's reply is narration with a verdict
    somewhere in it, while the file is exactly what it decided to write. Neither
    working is a ``fail`` and not a ``pass`` — see ``verdict.unparsable``.
    """
    raw = await session.read_file(ev.VERDICT_PATH)
    if raw.strip():
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            from_file = verdict_from_payload(payload)
            if from_file is not None:
                return from_file
        from_file_text = parse_verdict(raw)
        if from_file_text is not None:
            return from_file_text

    from_reply = parse_verdict(evaluator_answer)
    if from_reply is not None:
        return from_reply
    return unparsable(
        "không có file verdict đọc được và trong câu trả lời cũng không có JSON"
    )


async def run_loop(
    *,
    base_prompt: str,
    objective: str,
    worktree: Path,
    worktree_root: Path,
    run_id: str,
    budget: LoopBudget,
    image: str = sandbox_runner.DEFAULT_IMAGE,
    per_attempt_minutes: int = sandbox_runner.DEFAULT_TIMEOUT_MINUTES,
    monorepo: Path | None = None,
    enabled_skills: list | None = None,
    skill_packs_root: Path | None = None,
    staging: bool = False,
    staging_token: str = "",
    agent_repos_root: Path | None = None,
    verify_commands_raw: str = "",
    changed_repos: Callable[[], list[str]] | None = None,
    on_stage: Callable[[str, dict], None] | None = None,
) -> LoopOutcome:
    """Work one ticket to a verified stop, inside a single container.

    ``changed_repos`` is a host-side callable because only the host can run git
    against the bind-mounted worktrees — and it is what narrows the verify
    commands to the sub-projects this attempt actually touched, so an API-only
    ticket never pays for the CMS test suite.

    ``on_stage`` is how the run detail page follows along; it is called with a
    stage name and a dict of fields, and any exception it raises is swallowed —
    a reporting failure must not end a run that is working.
    """
    ledger = LoopLedger(budget=budget)
    control = ctrl.LoopController(budget)
    outcome = LoopOutcome(outcome=ctrl.OUTCOME_CAPPED)

    def stage(name: str, **fields) -> None:
        if on_stage is None:
            return
        try:
            on_stage(name, fields)
        except Exception:  # noqa: BLE001 — never fail a run over reporting
            logger.warning("bloy_dev_agent: stage callback failed", exc_info=True)

    timeout = session_timeout_minutes(per_attempt_minutes, budget.max_attempts)
    log_path = agent_log.host_log_path(worktree_root, run_id) if run_id else None

    async with sandbox_runner.SandboxSession(
        worktree=worktree,
        worktree_root=worktree_root,
        # Same image and same captured-session rules as the single-shot path:
        # staging swaps in the Chromium image and mounts the Shopify session, so
        # screenshot verification behaves identically inside the loop.
        image=sandbox_runner.image_for(image, staging),
        timeout_minutes=timeout,
        run_id=run_id,
        monorepo=monorepo,
        enabled_skills=enabled_skills,
        skill_packs_root=skill_packs_root,
        staging=staging,
        shopify_auth_dir=sandbox_runner.auth_dir_for(None, staging),
        staging_token=staging_token,
        agent_repos_root=agent_repos_root,
    ) as session:
        outcome.sandbox_id = session.sandbox_id
        if session.setup_error:
            outcome.outcome = ctrl.OUTCOME_NEEDS_HUMAN
            outcome.setup_error = session.setup_error
            outcome.detail = session.setup_error
            return outcome

        step = control.first()
        prompt = base_prompt
        while True:
            stage("generate", attempt=step.attempt, sandbox_id=session.sandbox_id)
            generator = await session.turn(
                prompt,
                mode=sandbox_runner.MODE_IMPLEMENT,
                log_path=log_path,
                # Every generator turn lands in the one run log the detail page
                # tails, so a reviewer sees the whole run rather than only its
                # last attempt. The evaluator gets its own file (below) so the
                # implementation narrative stays readable.
                append=step.attempt > 1,
            )
            ledger.add(tokens=generator.tokens, cost_usd=generator.cost_usd)
            record = AttemptRecord(
                attempt=step.attempt,
                generator_ok=generator.ok,
                generator_answer=generator.output,
                tokens=generator.tokens,
                cost_usd=generator.cost_usd,
            )
            outcome.attempts.append(record)
            # Keep the answer of the newest attempt: its code is what is on disk.
            outcome.answer = generator.output

            if not generator.ok:
                # No verdict at all: the controller counts this as zero progress,
                # which is what trips the stall guard when a container or a
                # provider limit keeps killing the turn.
                decision = control.after(None, ledger)
            else:
                stage("verify", attempt=step.attempt)
                repos = list(changed_repos() if changed_repos else [])
                record.receipts = await _collect_receipts(
                    session,
                    verify_commands_raw=verify_commands_raw,
                    repos=repos,
                )

                stage("evaluate", attempt=step.attempt)
                screenshots = (
                    agent_log.list_artifacts(worktree_root, run_id) if run_id else []
                )
                eval_log = (
                    agent_log.host_log_path(worktree_root, f"{run_id}-eval{step.attempt}")
                    if run_id
                    else None
                )
                evaluator_turn = await session.turn(
                    ev.build_prompt(
                        objective=objective,
                        generator_answer=generator.output,
                        workdir=session.workdir,
                        receipts_available=record.receipts is not None,
                        screenshots=screenshots,
                        staging=staging,
                    ),
                    mode=sandbox_runner.MODE_EVALUATE,
                    log_path=eval_log,
                )
                ledger.add(
                    tokens=evaluator_turn.tokens, cost_usd=evaluator_turn.cost_usd
                )
                verdict = await _read_verdict(session, evaluator_turn.output)
                verdict.eval_tokens = evaluator_turn.tokens
                verdict.eval_cost_usd = evaluator_turn.cost_usd
                record.verdict = verdict
                record.tokens += evaluator_turn.tokens
                record.cost_usd += evaluator_turn.cost_usd

                # A verify command that this service ran and that failed is a
                # fact the evaluator is not allowed to pass over. Enforced here
                # rather than trusted to the prompt: the whole reason receipts
                # exist is that an agent's claim about a command is not evidence.
                if (
                    verdict.passed
                    and record.receipts is not None
                    and not record.receipts.all_ok
                ):
                    failing = ", ".join(r.command for r in record.receipts.failed[:3])
                    logger.info(
                        "bloy_dev_agent: overriding a pass verdict for run %s — "
                        "receipts failed: %s", run_id, failing,
                    )
                    verdict.verdict = type(verdict.verdict).FAIL
                    verdict.missing = (
                        "Evaluator kết luận PASS nhưng lệnh verify do service "
                        f"tự chạy vẫn trượt: {failing}. Sửa cho các lệnh này "
                        "pass đã, rồi mới kết luận."
                    ) + (f"\n\n{verdict.missing}" if verdict.missing else "")
                    verdict.score = min(verdict.score, 0.5)

                decision = control.after(verdict, ledger)

            if isinstance(decision, ctrl.Done):
                outcome.outcome = decision.outcome
                outcome.detail = decision.detail
                break

            prompt = ev.build_retry_prompt(
                base_prompt, attempt=decision.attempt, feedback=decision.feedback
            )
            step = decision

    outcome.spend = ledger.summary()
    return outcome
