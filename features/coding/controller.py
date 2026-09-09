"""The loop controller: continue-vs-stop, as pure logic with no I/O.

Given the verdict of the latest attempt and what the run has spent so far, this
decides whether to run another generator attempt — and with what feedback — or
to finish, and with which outcome. It touches no database, no container and no
Twenty API, so every rule below is testable without a model or a sandbox.
:mod:`bloy_dev_agent.features.coding.loop` owns all the I/O and asks this
module what to do next.

Decision rules, and why each one is the way it is:

* ``pass``          → finish ``complete``.
* ``needs_human``   → finish ``needs_human``. Never retried: the evaluator is
  saying a person has to decide something, and a second attempt cannot decide
  it either — it just spends a container to ask the same question again.
* ``fail``          → another attempt, carrying the evaluator's ``missing``
  text as the instruction. This is the whole point of the loop: a rerun with
  the identical prompt (what the old ``sandbox_stage_retries`` did) can only
  reproduce the same result, while a retry that is *told what was wrong* can
  fix it.
* no verdict at all → treated as ``fail`` and the loop continues (**fail
  open**). A broken evaluator must never read as success; the cost of this
  choice is one extra attempt, the cost of the opposite is shipping unverified
  work. See ``verdict.unparsable``.
* attempt cap       → finish ``capped``.
* zero-progress streak → finish ``stalled``, rather than burning every
  remaining attempt against a wall.

Every terminal outcome except ``complete`` routes the ticket to a human. There
is deliberately no "gave up quietly" branch.
"""

from __future__ import annotations

from dataclasses import dataclass

from bloy_dev_agent.features.coding.budget import LoopBudget, LoopLedger
from bloy_dev_agent.features.coding.verdict import LoopVerdict, Verdict, format_missing

#: The ticket is met and verified.
OUTCOME_COMPLETE = "complete"
#: Ran out of attempts while still failing.
OUTCOME_CAPPED = "capped"
#: The evaluator asked for a person.
OUTCOME_NEEDS_HUMAN = "needs_human"
#: Consecutive attempts made no progress at all.
OUTCOME_STALLED = "stalled"
#: A resource cap (tokens/cost/runtime) hard-stopped the run.
OUTCOME_BUDGET = "budget"

#: Outcomes that mean "a human has to look at this now". Everything that is not
#: ``complete`` is in here on purpose — see the module docstring.
HUMAN_OUTCOMES = frozenset(
    {OUTCOME_CAPPED, OUTCOME_NEEDS_HUMAN, OUTCOME_STALLED, OUTCOME_BUDGET}
)

#: Vietnamese, one line per outcome, for the comment posted back to the ticket.
OUTCOME_LABELS = {
    OUTCOME_COMPLETE: "đã xong và được evaluator xác nhận",
    OUTCOME_CAPPED: "hết số lần thử mà evaluator vẫn chưa xác nhận",
    OUTCOME_NEEDS_HUMAN: "evaluator yêu cầu người quyết định",
    OUTCOME_STALLED: "dừng sớm vì nhiều lần thử liền không tiến triển",
    OUTCOME_BUDGET: "dừng vì chạm giới hạn tài nguyên",
}


@dataclass(frozen=True)
class Continue:
    """Run another generator attempt with ``feedback`` prepended to the task."""

    attempt: int
    feedback: str


@dataclass(frozen=True)
class Done:
    """Stop, with a terminal outcome and the reason to report."""

    outcome: str
    detail: str = ""

    @property
    def needs_human(self) -> bool:
        return self.outcome in HUMAN_OUTCOMES


class LoopController:
    """Decides what happens after each attempt. Holds only counters."""

    def __init__(self, budget: LoopBudget | None = None) -> None:
        self._budget = budget or LoopBudget()
        #: Attempts already run (a completed generator + evaluator pair).
        self.attempts = 0
        #: Consecutive attempts that scored nothing.
        self.zero_streak = 0

    @property
    def budget(self) -> LoopBudget:
        return self._budget

    def first(self) -> Continue:
        """The opening attempt. No feedback yet — the ticket is the instruction."""
        self.attempts = 1
        return Continue(attempt=1, feedback="")

    def after(self, verdict: Verdict | None, ledger: LoopLedger) -> Continue | Done:
        """Decide what follows the attempt that produced ``verdict``.

        ``ledger`` is checked *before* the verdict is acted on: a run that has
        already spent its budget must stop even when the attempt it just paid
        for came back fail — otherwise the cap is always exceeded by exactly one
        more attempt.
        """
        self._track_progress(verdict)

        crossed = ledger.exceeded()
        if crossed is not None and not (verdict and verdict.passed):
            return Done(
                OUTCOME_BUDGET,
                f"Chạm giới hạn {crossed} sau {self.attempts} lần thử "
                f"({ledger.summary()}).",
            )

        if verdict is not None and verdict.passed:
            return Done(OUTCOME_COMPLETE)

        if verdict is not None and verdict.verdict is LoopVerdict.NEEDS_HUMAN:
            return Done(
                OUTCOME_NEEDS_HUMAN,
                verdict.missing or "Evaluator cho rằng việc này cần người quyết định.",
            )

        if (
            self._budget.max_zero_streak
            and self.zero_streak >= self._budget.max_zero_streak
        ):
            return Done(
                OUTCOME_STALLED,
                f"{self.zero_streak} lần thử liền không tiến triển "
                f"(score 0). Dừng để không tốn thêm container.",
            )

        if self.attempts >= max(1, self._budget.max_attempts):
            return Done(
                OUTCOME_CAPPED,
                f"Đã thử {self.attempts} lần, evaluator vẫn chưa xác nhận. "
                + (format_missing(verdict) or "Không có mô tả cụ thể còn thiếu gì."),
            )

        self.attempts += 1
        return Continue(
            attempt=self.attempts,
            feedback=format_missing(verdict),
        )

    def _track_progress(self, verdict: Verdict | None) -> None:
        """A scoreless attempt extends the stall streak; any score resets it.

        A missing verdict counts as zero progress, not as unknown: the two
        situations that produce one — the evaluator crashed, or the generator
        turn never finished — are both exactly the wall the stall guard exists
        to detect.
        """
        if verdict is not None and verdict.score > 0:
            self.zero_streak = 0
        else:
            self.zero_streak += 1
