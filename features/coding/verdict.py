"""The evaluator's verdict, and a tolerant parser for an agent-produced one.

An evaluator turn ends with a JSON object stating whether the attempt actually
met the ticket. Two independent delivery paths, in priority order:

1. a file the evaluator writes inside the container, and
2. the same object echoed in its reply text.

The file is read first because a CLI agent's stdout is noisy — ``claude -p``
interleaves the answer with whatever the agent narrated on the way — while a
file it wrote is exactly what it meant to write. The text parser exists for the
case where the agent answered but never managed the file, and is deliberately
forgiving: it takes the *last* JSON object in the text, so an evaluator that
wraps its verdict in prose (or emits a draft and then a final one) still yields
a usable grade.

Borrowed in shape from agent_team's ``loop/verdict.py``, which solved the same
problem for the same reason. Kept as its own copy rather than an import: this
service must boot without BAM (see the package docstring and
``tests/test_bloy_dev_agent.py::test_the_service_imports_nothing_from_bam``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum


class LoopVerdict(StrEnum):
    """Whether an attempt met the ticket."""

    PASS = "pass"
    FAIL = "fail"
    #: The work cannot be judged here, or a person must decide (risky change,
    #: ambiguous requirement). Never retried automatically — retrying a
    #: question nobody answered just burns the budget.
    NEEDS_HUMAN = "needs_human"


#: The JSON shape both the file and the echoed reply must use. Interpolated
#: into the evaluator prompt so the contract is stated exactly once.
VERDICT_SHAPE = (
    '{"verdict": "pass|fail|needs_human", "score": 0.0-1.0, '
    '"missing": "những gì còn phải làm (rỗng nếu pass)", '
    '"evidence": {"checks": "đã chạy gì và thấy gì"}}'
)


@dataclass
class Verdict:
    """An independent grade of one attempt."""

    verdict: LoopVerdict
    score: float = 0.0
    #: What still has to happen for the ticket to be met. Fed back to the
    #: generator verbatim on the next attempt — this string is the whole
    #: reason a retry is better than a rerun.
    missing: str = ""
    evidence: dict = field(default_factory=dict)
    #: Resource use of the *evaluator turn itself*. The evaluator is a real
    #: agent run (it reads the diff and may run the project's tests), so its
    #: spend counts against the loop budget exactly like the generator's.
    eval_tokens: int = 0
    eval_cost_usd: float = 0.0
    #: True when neither the file nor the text carried a usable verdict. The
    #: controller treats this as a fail and keeps going (fail open) rather
    #: than letting a broken evaluator read as success.
    unparsed: bool = False

    @property
    def passed(self) -> bool:
        return self.verdict is LoopVerdict.PASS


def _coerce_score(raw: object) -> float:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, value))


def _coerce_verdict(raw: object) -> LoopVerdict | None:
    text = str(raw or "").strip().lower()
    for candidate in LoopVerdict:
        if text == candidate.value:
            return candidate
    return None


def _last_json_object(text: str) -> dict | None:
    """The last top-level balanced ``{...}`` in ``text`` that is a JSON object.

    One forward pass tracking brace depth, with string literals (and their
    backslash escapes) skipped so a brace inside ``"missing"`` cannot close the
    object early. Every balanced top-level span is tried and the last one that
    decodes wins, so an evaluator that emits a draft object before its final
    one still grades on the final one.
    """
    found: dict | None = None
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text or ""):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth == 0:
                continue  # stray closer, e.g. prose with an unmatched brace
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    parsed = json.loads(text[start : index + 1])
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    found = parsed
                start = -1
    return found


def parse_verdict(text: str) -> Verdict | None:
    """Read a verdict out of free-form evaluator output, or ``None``.

    ``None`` means "no verdict was stated", which is not the same as a fail —
    the caller decides what to do with an evaluator that produced nothing.
    """
    payload = _last_json_object(text or "")
    if payload is None:
        return None
    return verdict_from_payload(payload)


def verdict_from_payload(payload: dict) -> Verdict | None:
    """Build a :class:`Verdict` from an already-decoded JSON object."""
    if not isinstance(payload, dict):
        return None
    decided = _coerce_verdict(payload.get("verdict"))
    if decided is None:
        return None
    evidence = payload.get("evidence")
    return Verdict(
        verdict=decided,
        score=_coerce_score(payload.get("score")),
        missing=str(payload.get("missing") or "").strip(),
        evidence=evidence if isinstance(evidence, dict) else {},
    )


def unparsable(reason: str) -> Verdict:
    """A stand-in verdict for "the evaluator did not grade this attempt".

    Deliberately a ``fail`` and not a ``pass``: an evaluator that crashed, ran
    out of context or answered in prose must never be able to mark work
    complete. See ``controller`` — a fail keeps the loop going, so the cost of
    this choice is one more attempt, while the cost of the opposite is shipping
    unverified work.
    """
    return Verdict(
        verdict=LoopVerdict.FAIL,
        score=0.0,
        missing=(
            "Evaluator không trả về verdict đọc được "
            f"({reason}). Coi như CHƯA xong và làm tiếp."
        ),
        unparsed=True,
    )


def format_missing(verdict: Verdict | None) -> str:
    """The feedback line handed to the next generator attempt."""
    if verdict is None:
        return ""
    parts = [verdict.missing.strip()] if verdict.missing.strip() else []
    checks = str((verdict.evidence or {}).get("checks") or "").strip()
    if checks:
        parts.append(f"Evaluator đã kiểm tra: {checks}")
    return "\n\n".join(parts)
