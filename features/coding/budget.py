"""Guardrails for an unattended coding loop: attempts, tokens, cost, wall-clock.

Why this exists at all: the loop retries on its own, with no human gate in
front of it, so the only thing standing between "one more attempt" and an
unbounded bill is a cap somebody wrote down. The old pipeline had exactly one
bound — the container's own ``timeout_minutes`` — which caps a *single* agent
session and says nothing about how many sessions a ticket may consume.

:class:`LoopBudget` is the static policy; :class:`LoopLedger` accumulates what
a run has actually spent and names the first cap it crossed. Hitting any cap is
a hard stop that reports back to the ticket, never a silent finish: a run that
quietly gave up looks identical to a run that succeeded, and that is how a
ticket sits "done" with no merge request behind it.

Shape borrowed from agent_team's ``loop/budget.py``; kept as its own copy for
the no-BAM-import reason in the package docstring.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

#: Attempts a single run may spend before it stops and reports. Two, not one,
#: because the single most common failure is a near-miss the evaluator can
#: describe precisely ("thiếu test cho case X") — and not five, because an
#: agent that is still wrong after three tries is usually wrong about the
#: ticket, not about the code, and a person reads that faster than a fourth
#: container does.
DEFAULT_MAX_ATTEMPTS = 3

#: Consecutive zero-progress attempts that trip the stall guard. An attempt
#: scores 0 when the evaluator graded it worthless or the generator turn failed
#: outright (a provider rate limit, a wedged container). Tolerates one blip;
#: stops fast against a real wall instead of burning the whole attempt cap.
DEFAULT_MAX_ZERO_STREAK = 2


@dataclass(frozen=True)
class LoopBudget:
    """Resource caps for one run. ``None``/``0`` means unbounded."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_wall_seconds: int | None = None
    max_zero_streak: int = DEFAULT_MAX_ZERO_STREAK


@dataclass
class LoopLedger:
    """Running totals for a run, checked against a :class:`LoopBudget`."""

    budget: LoopBudget = field(default_factory=LoopBudget)
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    #: Monotonic, not wall time: a host clock adjustment mid-run must not
    #: retroactively look like an hour of runtime.
    started_at: float = field(default_factory=time.monotonic)

    def add(self, *, tokens: int = 0, cost_usd: float = 0.0) -> None:
        """Fold one turn's resource use into the totals.

        Negative values are clamped rather than trusted: these numbers come out
        of a CLI's JSON stream, and a missing field decoded as ``-1`` must not
        be able to *lower* the running total and buy extra attempts.
        """
        self.total_tokens += max(0, int(tokens or 0))
        self.total_cost_usd += max(0.0, float(cost_usd or 0.0))

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def exceeded(self) -> str | None:
        """Name of the first cap crossed, or ``None`` while within budget."""
        budget = self.budget
        if budget.max_tokens and self.total_tokens >= budget.max_tokens:
            return "tokens"
        if budget.max_cost_usd and self.total_cost_usd >= budget.max_cost_usd:
            return "cost"
        if budget.max_wall_seconds and self.elapsed_seconds() >= budget.max_wall_seconds:
            return "runtime"
        return None

    def summary(self) -> str:
        """One human-readable line for the report posted back to the ticket."""
        minutes = self.elapsed_seconds() / 60.0
        parts = [f"{self.total_tokens:,} token", f"{minutes:.1f} phút"]
        if self.total_cost_usd > 0:
            parts.insert(1, f"${self.total_cost_usd:.2f}")
        return ", ".join(parts)
