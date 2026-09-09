"""The evaluator turn: an independent grade of what the generator just did.

The generator must never grade its own work. Left to itself a coding agent
reports success with remarkable confidence — the old pipeline shipped tickets
whose final answer described code the agent had written itself as code it had
merely *found*, and nothing in the run disagreed. So evaluation is a separate
turn, in the same container (it has to see the diff and run the tests), with a
prompt that tells it to try to *disprove* completion.

Three things make this more than a second opinion from the same model:

1. **No write tools.** The evaluator runs with read + Bash only (see
   ``sandbox_runner.claude_flags(mode="evaluate")``), so it cannot quietly fix
   what it was supposed to judge and then pass it.
2. **Host-observed evidence.** The receipts it reads were produced by this
   service running the commands itself, and the screenshot list comes from what
   actually landed on the host's disk. Neither is the agent's word.
3. **A machine-readable verdict.** It writes JSON to a known file, so the
   decision is a parsed value the controller acts on, not prose somebody has to
   interpret.

Shape borrowed from agent_team's ``loop/evaluator.py``, adapted to this
pipeline: there is no human plan-approval step here, so the acceptance criteria
are the ticket itself plus the configured verify commands, and the evaluator is
the only gate before a person sees a merge request.
"""

from __future__ import annotations

from bloy_dev_agent.features.coding.verdict import VERDICT_SHAPE

#: Where the evaluator writes its verdict, inside the container. Deliberately
#: under ``/tmp`` and NOT in any worktree: everything under a worktree is swept
#: into the merge request by ``git add -A``, and a run once shipped a merge
#: request containing nothing but a leaked ``.playwright-mcp`` file for exactly
#: this reason (see ``sandbox_runner.MCP_OUTPUT_DIR``).
VERDICT_PATH = "/tmp/bloy-verdict.json"

#: Where this service writes the receipts projection the evaluator reads. Same
#: /tmp reasoning as above. The database copy is authoritative; this file exists
#: only because a CLI agent cannot query the service's database.
RECEIPTS_PATH = "/tmp/bloy-receipts.json"

_ROLE = """\
ROLE: INDEPENDENT EVALUATOR — you did NOT write this code.

Another agent attempted the ticket below in this same worktree. Your job is to
decide whether it is genuinely complete. Be skeptical: assume the work is
incomplete or broken until concrete evidence proves otherwise. You have read
and Bash tools but NO edit tools — do not try to fix anything, only judge it.

Do not trust the other agent's summary of what it did. Read the actual diff
(`git status --porcelain` and `git diff`) and the actual files.
"""

_VERDICT_CONTRACT = """\
## How to answer

Write your verdict as a single JSON object to `{verdict_path}` — create the
file, do not append to it — AND repeat the same object at the very end of your
reply. The file is read first; the reply is the fallback.

    {shape}

Field rules:

- `verdict`: "pass" only when the ticket is met AND the evidence supports it.
  "fail" when something concrete is still missing or broken. "needs_human" ONLY
  when a person must decide (the change is risky or the requirement is
  genuinely ambiguous) — not merely because you are unsure.
- `score`: 0.0-1.0, how close this attempt came. A "fail" that got most of the
  way should score well above one that changed nothing. This number decides
  whether the loop keeps trying, so a wrong 0.0 stops work that was nearly
  done.
- `missing`: what still has to happen, specific enough for the next attempt to
  act on without re-deriving it. This text is handed to the next attempt
  VERBATIM and is the only thing it learns from you — "chưa đúng" is useless,
  "thiếu guard cho case status=archived trong translations.service.ts" is not.
  Leave empty only for a pass.
"""

_NO_RECEIPTS = """\
## Verification commands

This service could not produce trusted command receipts for this run. Do NOT
claim that the project's tests or build passed. If the ticket needed them,
return "fail" and say the runner was unavailable.
"""

_WITH_RECEIPTS = """\
## Verification commands (trusted receipts)

This service ran the configured verify commands ITSELF, inside this container,
after the generator finished. Their results are in `{receipts_path}` — read it.

Those receipts are the authoritative record of what ran. You may run further
commands yourself to investigate, but you may NOT replace a receipt with your
own run of the same command, and you may not claim a command passed when its
receipt says otherwise. Cite the receipt ids you relied on in
`evidence.checks`.
"""

_STAGING_EVIDENCE = """\
## Staging verification evidence

The generator had a real staging deploy target and a real browser. These
screenshot files were found on the host's disk after its turn — this list is
observed fact, not something the agent reported:

{screenshots}

Judge whether they actually show the ticket's change working. A screenshot of a
login wall, an error page, or an unrelated screen is NOT evidence of a verified
change: if that is what you see, say so in `missing` and do not pass on it.
"""

_NO_STAGING_EVIDENCE = """\
## Staging verification evidence

The generator had a real staging deploy target and a real browser, but saved NO
screenshots. If this ticket's change is visible in the UI, that absence is
itself a finding — a change that could have been verified on staging and was
not should not pass on the agent's description alone.
"""


def build_prompt(
    *,
    objective: str,
    generator_answer: str,
    workdir: str,
    receipts_available: bool,
    screenshots: list[str] | None = None,
    staging: bool = False,
    verdict_path: str = VERDICT_PATH,
    receipts_path: str = RECEIPTS_PATH,
) -> str:
    """Compose the evaluator turn's prompt.

    ``screenshots`` is what this service found on the host after the generator
    turn — passed in rather than discovered here so the evaluator's view of the
    evidence and the reviewer's are the same list.
    """
    blocks = [
        _ROLE,
        f"The worktree is at {workdir}.",
        "## The ticket\n\n" + (objective.strip() or "(no description)"),
        "## What the previous agent says it did\n\n"
        + (generator_answer.strip() or "(it produced no final answer at all)"),
    ]

    blocks.append(
        _WITH_RECEIPTS.format(receipts_path=receipts_path)
        if receipts_available
        else _NO_RECEIPTS
    )

    if staging:
        names = [name for name in (screenshots or []) if name]
        blocks.append(
            _STAGING_EVIDENCE.format(
                screenshots="\n".join(f"  - {name}" for name in names)
            )
            if names
            else _NO_STAGING_EVIDENCE
        )

    blocks.append(
        _VERDICT_CONTRACT.format(verdict_path=verdict_path, shape=VERDICT_SHAPE)
    )
    return "\n\n".join(block.strip() for block in blocks) + "\n"


_RETRY_HEADER = """\
## This is a RETRY — attempt {attempt}

An independent evaluator reviewed your previous attempt in this same worktree
and did NOT accept it. Your changes are still there; continue from them rather
than starting over, and do not revert work the evaluator did not object to.

What the evaluator said is still missing:

{feedback}

Fix exactly that. If you believe the evaluator is wrong, say so explicitly in
your final answer with the evidence that proves it — do not silently ignore it.
"""


def build_retry_prompt(base_prompt: str, *, attempt: int, feedback: str) -> str:
    """Prepend the evaluator's feedback to the original task prompt.

    The retry carries the *whole* original prompt again, not a bare "fix this":
    each turn is a fresh ``claude -p`` process with no memory of the last one,
    so a prompt that only said what was wrong would omit the ticket, the repo
    layout, the skills and the staging instructions along with it.
    """
    header = _RETRY_HEADER.format(
        attempt=attempt,
        feedback=feedback.strip() or "(evaluator không nêu cụ thể — tự rà lại từ đầu)",
    )
    return f"{header}\n\n{base_prompt}"
