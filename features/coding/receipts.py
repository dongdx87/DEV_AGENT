"""Trusted execution of verification commands — the service runs them, not the agent.

The problem this exists to solve, observed in this pipeline: an agent is
allowed to *interpret* results, but it must not be trusted to claim that a
command ran at all. Nothing stops a coding agent from writing "✅ tests pass"
into its final answer, and the old pipeline's report matched on exactly that
kind of self-authored string ("ĐÃ VERIFY TRÊN STAGING") — so a run that never
executed a test and a run that executed and passed one produced an identical
report.

The fix is not a better prompt. It is that **this service** runs the commands,
inside the run's own container, after the generator's turn, and stores what
happened. The agent never chooses the command list and never reports the
result; the evaluator only gets to read receipts it could not have written.

Two boundaries worth stating:

* The command list comes from a **setting an operator configured once**, never
  from the ticket and never from the agent. There is no per-ticket human
  approval step in this pipeline (that is deliberate — see the loop's own
  docstring), so the allow-list has to be pre-agreed rather than negotiated per
  run. A command the operator never wrote down simply does not run.
* The database rows are authoritative. The JSON file pushed into the container
  is a **projection** for the evaluator to read, because a CLI agent cannot
  query this service's database. Nothing reads that file back to decide an
  outcome.

Borrowed in shape from agent_team's ``loop/verification_runner.py``, which drew
the same line for the same reason.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Per-command ceiling. A verify command is a test/lint/build, not the agent's
#: own exploration — one that runs longer than this is wedged, and letting it
#: run holds the whole container (and the staging slot) hostage.
DEFAULT_COMMAND_TIMEOUT_SECONDS = 15 * 60

#: How much of a command's output is kept. Enough for a failing test's actual
#: assertion, small enough that a run's receipts do not become the biggest rows
#: in the database. The full output's hash is stored either way, so a truncated
#: receipt can still be shown to be the one that was produced.
MAX_OUTPUT_CHARS = 20_000

#: Commands are rejected rather than escaped if they carry shell metacharacters
#: that would let one configured entry become several. The list is written by an
#: operator, not an attacker — but a stray ``&&`` silently turning one receipt
#: into two commands, only one of which is recorded, is a correctness bug in the
#: evidence trail regardless of intent.
_FORBIDDEN = (";", "&&", "||", "|", "`", "$(", "\n", ">", "<")


class VerifyContractError(ValueError):
    """A configured verify command cannot be executed safely as written."""


@dataclass(frozen=True)
class VerifyCommand:
    """One command an operator approved for one repository."""

    repo: str
    command: str

    def argv_preview(self) -> list[str]:
        """The command as a token list, for logging. Never used to execute."""
        try:
            return shlex.split(self.command)
        except ValueError:
            return [self.command]


@dataclass
class Receipt:
    """What happened when this service ran one verify command."""

    id: str
    repo: str
    command: str
    exit_code: int
    ok: bool
    duration_ms: int
    output: str
    output_sha256: str
    truncated: bool = False
    #: Set when the command never produced an exit code — the container died,
    #: the call timed out, the SDK raised. Distinct from a non-zero exit: one
    #: means "the check failed", the other "the check did not happen".
    error: str = ""

    @property
    def ran(self) -> bool:
        return not self.error


@dataclass
class ReceiptBatch:
    """Every receipt for one attempt, plus the id the evaluator must cite."""

    batch_id: str
    receipts: list[Receipt] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        """True only when every command ran AND every one of them passed."""
        return bool(self.receipts) and all(r.ran and r.ok for r in self.receipts)

    @property
    def failed(self) -> list[Receipt]:
        return [r for r in self.receipts if not (r.ran and r.ok)]

    def summary(self) -> str:
        """One Vietnamese line for the report a reviewer reads on the ticket."""
        if not self.receipts:
            return "không có lệnh verify nào được cấu hình"
        passed = sum(1 for r in self.receipts if r.ran and r.ok)
        total = len(self.receipts)
        if passed == total:
            return f"{passed}/{total} lệnh verify PASS (service tự chạy)"
        names = ", ".join(r.command for r in self.failed[:3])
        return f"{passed}/{total} lệnh verify pass — trượt: {names}"


def parse_commands(raw: str) -> list[VerifyCommand]:
    """Read the operator's setting into an ordered command list.

    Format is one ``<repo>: <command>`` per line, repo repeated for several
    commands. Blank lines and ``#`` comments are ignored, so the setting can be
    annotated with why a command is there.

    A line with no colon, or whose command carries shell metacharacters, raises
    :class:`VerifyContractError` — the caller reports the run as having no
    trusted runner rather than silently running a subset, because "some of the
    checks you configured ran" is the one outcome nobody can act on.
    """
    commands: list[VerifyCommand] = []
    for number, line in enumerate((raw or "").splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        repo, separator, command = text.partition(":")
        if not separator:
            raise VerifyContractError(
                f"dòng {number} thiếu dấu ':' — cần '<repo>: <lệnh>'"
            )
        repo = repo.strip()
        command = command.strip()
        if not repo or not command:
            raise VerifyContractError(f"dòng {number} thiếu repo hoặc lệnh")
        for token in _FORBIDDEN:
            if token in command:
                raise VerifyContractError(
                    f"dòng {number}: lệnh chứa {token!r} — mỗi dòng phải là một "
                    f"lệnh đơn, tách thành nhiều dòng nếu cần nhiều lệnh"
                )
        commands.append(VerifyCommand(repo=repo, command=command))
    return commands


def commands_for(raw: str, repos: list[str]) -> list[VerifyCommand]:
    """Only the configured commands whose repo this run actually prepared.

    A ticket rarely touches every sub-project. Running the CMS test suite for an
    API-only change costs minutes and can only produce noise, so the filter is
    by what the run has a worktree for — and the caller narrows it further to
    the repos that actually changed.
    """
    wanted = {name for name in repos if name}
    return [command for command in parse_commands(raw) if command.repo in wanted]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def make_receipt(
    command: VerifyCommand,
    *,
    exit_code: int,
    output: str,
    duration_ms: int,
    error: str = "",
) -> Receipt:
    """Turn one command's raw result into a receipt.

    Deliberately separate from running it: the executor is async (it talks to a
    container) while all the bookkeeping here — hashing, truncation, deciding
    that a non-zero exit is a *failed check* but a raised exception is a check
    that *never happened* — is pure and worth testing on its own.
    """
    full = output or ""
    truncated = len(full) > MAX_OUTPUT_CHARS
    return Receipt(
        id=uuid.uuid4().hex[:16],
        repo=command.repo,
        command=command.command,
        exit_code=int(exit_code),
        ok=(error == "" and int(exit_code) == 0),
        duration_ms=max(0, int(duration_ms)),
        output=full[-MAX_OUTPUT_CHARS:] if truncated else full,
        output_sha256=_digest(full),
        truncated=truncated,
        error=error,
    )


def new_batch(batch_id: str = "") -> ReceiptBatch:
    """An empty batch with an id the evaluator can be told to cite."""
    return ReceiptBatch(batch_id=batch_id or uuid.uuid4().hex[:16])


def run_batch(
    commands: list[VerifyCommand],
    *,
    execute: Callable[[VerifyCommand], tuple[int, str]],
    batch_id: str = "",
) -> ReceiptBatch:
    """Synchronous convenience wrapper: run every command and record each result.

    Not used by the async loop (which times and records each command itself via
    :func:`make_receipt`), but kept because it is the shape a test can drive
    with a fake executor and because a future synchronous caller should not have
    to re-derive the timing/exception handling.

    An exception from ``execute`` is recorded and the batch continues: one
    wedged command must not erase the evidence from the ones that did run.
    """
    batch = new_batch(batch_id)
    for command in commands:
        started = time.monotonic()
        error = ""
        exit_code = -1
        output = ""
        try:
            exit_code, output = execute(command)
        except Exception as exc:  # noqa: BLE001 — a failed check is data, not a crash
            error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "bloy_dev_agent: verify command %r on %s did not run: %s",
                command.command, command.repo, error,
            )
        batch.receipts.append(
            make_receipt(
                command,
                exit_code=exit_code,
                output=output,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=error,
            )
        )
    return batch


def projection(batch: ReceiptBatch) -> str:
    """The JSON the evaluator reads inside the container.

    Carries the receipt ids (so the evaluator can cite them), what ran, and the
    outcome — but only the *tail* of each output, because the whole point is for
    the evaluator to read a verdict-relevant summary rather than re-litigate
    megabytes of build log. The authoritative copy stays in the database.
    """
    return json.dumps(
        {
            "batch_id": batch.batch_id,
            "produced_by": "bloy_dev_agent service (not the agent)",
            "all_ok": batch.all_ok,
            "receipts": [
                {
                    "id": receipt.id,
                    "repo": receipt.repo,
                    "command": receipt.command,
                    "ran": receipt.ran,
                    "ok": receipt.ok,
                    "exit_code": receipt.exit_code,
                    "duration_ms": receipt.duration_ms,
                    "error": receipt.error,
                    "output_sha256": receipt.output_sha256,
                    "output_truncated": receipt.truncated,
                    "output_tail": receipt.output[-4000:],
                }
                for receipt in batch.receipts
            ],
        },
        indent=2,
        ensure_ascii=False,
    )
