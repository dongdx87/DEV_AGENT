"""Human feedback on a finished ticket, and the revision round it starts.

The pipeline is unattended: it claims a ticket, works it, and reports. A person
enters at exactly one point — reading the merge request at the end. This module
is what happens when that person disagrees.

The mechanism is deliberately the one reviewers already use: **they comment on
the ticket**. No new UI, no button, no status they have to remember to set. A
pass over the review column looks for a comment written by a human *after* the
agent's own last report, and treats it as the authoritative instruction for a
new attempt — continuing on the same branch, pushing to the same merge request.

Why "after the agent's last report" and not "any comment": a ticket accumulates
years of discussion, and the migration imported all of it (see
``mapping.NormalizedComment``). Only what a person said in response to the
agent's own report is feedback *on the agent's work*; everything before it was
already in the prompt as context.

Two rules keep this from looping:

* A round is recorded against the comment id that triggered it, so the same
  comment can never start a second round — checked before any work, because the
  poll runs every few minutes and a run takes many of them.
* The agent's own reports are recognised and skipped. Without that, the report
  the agent posts at the end of a revision round would itself look like new
  feedback, and the ticket would work itself forever.
"""

from __future__ import annotations

import logging
import re

from bloy_dev_agent.features.twenty import mapping

logger = logging.getLogger(__name__)

#: Stamped on every comment this service posts, on its own line at the end.
#: The reliable way to tell our own reports from a human's reply: the Twenty API
#: key is bound to a bot workspace member, but a comment's author is not always
#: resolvable (migrated comments carry their real author in the body instead),
#: so identity is not something to depend on here. A marker in text we control
#: always is.
REPORT_MARKER = "<!-- bloy-dev-agent:report -->"

#: Reports posted before :data:`REPORT_MARKER` existed. Every one of them opens
#: with this, so an old ticket's history is still classified correctly instead
#: of the agent's own last report reading as human feedback the first time this
#: pass runs on it.
_LEGACY_REPORT_PREFIXES = (
    "Dev Agent đã xử lý",
    "Dev Agent đã phân tích",
    "Dev Agent không hoàn thành được",
    "Dev Agent ĐÃ DỪNG",
)

#: Authors whose comments are never treated as feedback. ``bloy_token`` is the
#: migration bot that bulk-imported Jira history — thousands of comments that
#: are context, not instructions (see ``mapping.NormalizedComment``).
_BOT_AUTHORS = frozenset({"bloy_token"})

#: A comment that is only a reaction carries no instruction, and starting a
#: container to act on "ok thanks" is pure waste. Matched whole, after
#: stripping, so a real instruction that merely contains "ok" is unaffected.
_NOISE = re.compile(
    r"^(ok|oke|okay|okie|thanks|thank you|cảm ơn|cám ơn|tks|thx|"
    r"đã xem|đã rõ|good|nice|\+1|👍|✅)[.!\s]*$",
    re.IGNORECASE,
)


def is_bot_report(comment: mapping.NormalizedComment) -> bool:
    """True when this comment is one of this service's own reports."""
    body = (comment.body or "").strip()
    if REPORT_MARKER in body:
        return True
    return any(body.startswith(prefix) for prefix in _LEGACY_REPORT_PREFIXES)


def is_actionable(comment: mapping.NormalizedComment) -> bool:
    """True when a human wrote something worth starting a revision for."""
    if is_bot_report(comment):
        return False
    if (comment.author or "").strip() in _BOT_AUTHORS:
        return False
    body = (comment.body or "").strip()
    if not body or _NOISE.match(body):
        return False
    return True


def pending(
    comments: list[mapping.NormalizedComment],
) -> mapping.NormalizedComment | None:
    """The newest human comment written after the agent's last report.

    ``None`` when the agent never reported (nothing to give feedback on yet),
    when the agent's report is still the last word, or when everything since it
    is noise.

    The *newest* rather than the oldest unread one, and only one: a reviewer who
    wrote three follow-ups meant the last one, and replaying the first would
    work from an instruction they have already superseded — the same reasoning
    ``normalize_comments`` documents for the ticket description itself.
    """
    last_report = -1
    for index, comment in enumerate(comments):
        if is_bot_report(comment):
            last_report = index
    if last_report < 0:
        return None
    for comment in reversed(comments[last_report + 1 :]):
        if is_actionable(comment):
            return comment
    return None


def stamp(text: str) -> str:
    """Append the marker to a report this service is about to post."""
    body = (text or "").rstrip()
    if REPORT_MARKER in body:
        return body
    return f"{body}\n\n{REPORT_MARKER}"


_REVISION_INSTRUCTIONS = """\
## THIS IS A REVISION ROUND — a human reviewed your work and asked for changes

You already worked this ticket. Your branch and your changes are still in this
worktree; a merge request is already open for them. A reviewer has now read it
and written the feedback below.

THE FEEDBACK IS THE AUTHORITATIVE INSTRUCTION. Where it conflicts with the
ticket description or with what you decided last time, follow the feedback — a
person looked at the actual diff, which is more than the description could tell
you.

Reviewer feedback ({author}, {created_at}):

{feedback}

How to work this round:

1. Read your own existing diff first (`git status --porcelain` and `git diff`)
   so you know what you already did. Do NOT start over and do NOT revert work
   the reviewer did not object to — the merge request's history is what they
   will read next.
2. Make the change the feedback asks for, and only that plus what it strictly
   requires.
3. If the feedback asks for something you believe is wrong or impossible, do
   NOT silently skip it: make whatever part is valid, and state plainly in your
   final answer which part you did not do and the concrete evidence why.
4. If the feedback is about something outside these worktrees (a theme snippet,
   a Shopify setting, another sub-project), say so explicitly rather than
   editing the wrong place.
"""


def build_revision_block(comment: mapping.NormalizedComment) -> str:
    """The instruction block prepended to a revision round's prompt."""
    return _REVISION_INSTRUCTIONS.format(
        author=comment.author or "không rõ người",
        created_at=comment.created_at or "không rõ thời điểm",
        feedback=(comment.body or "").strip() or "(trống)",
    )


def revision_prompt(base_prompt: str, comment: mapping.NormalizedComment) -> str:
    """Feedback first, then the whole original prompt.

    The original prompt is repeated in full rather than replaced: each turn is a
    fresh ``claude -p`` with no memory of the last run, so a prompt carrying only
    the feedback would drop the ticket, the repo layout, the enabled skills and
    the staging instructions along with it.
    """
    return f"{build_revision_block(comment)}\n\n{base_prompt}"
