"""Turn Twenty issue records into something the agent layer can consume.

Kept free of HTTP and database access so it can be unit-tested against
recorded payloads. Field names come from the live workspace rather than a
guess: the issue object exposes ``issueKey``, ``title``, ``description``
(rich text), ``status`` (a relation to ``issueStatuses``) and ``projectId``.
"""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass, field


@dataclass
class NormalizedIssue:
    """The parts of a Twenty issue this plugin actually uses."""

    id: str
    #: The key humans use — taken from the title prefix when present, otherwise
    #: Twenty's own. Drives branch names, commits and comments.
    key: str
    #: Twenty's own sequential key, kept so a record stays traceable in the UI.
    record_key: str
    title: str
    body: str
    status_name: str
    status_id: str
    project_id: str
    raw: dict = field(default_factory=dict, repr=False)


def blocknote_to_text(value: object) -> str:
    """Flatten Twenty's rich-text field into plain text.

    The value arrives as ``{"blocknote": "<json string>"}`` — a JSON document
    encoded inside a JSON string — so it takes two decodes before any text is
    reachable. Anything unexpected degrades to a string rather than raising:
    a malformed description should not stop a run.
    """
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return "" if value is None else str(value)

    raw = value.get("blocknote")
    if not isinstance(raw, str):
        return ""
    try:
        blocks = json.loads(raw)
    except ValueError:
        return raw

    lines: list[str] = []

    def walk(nodes: object) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            lines.append(
                "".join(
                    item.get("text", "")
                    for item in node.get("content") or []
                    if isinstance(item, dict)
                )
            )
            walk(node.get("children"))

    walk(blocks)
    return "\n".join(lines).strip()


def text_to_blocknote(text: str) -> dict:
    """Build the rich-text payload Twenty expects when writing a comment."""
    blocks = [
        {
            "id": f"p{index}",
            "type": "paragraph",
            "props": {},
            "content": [{"type": "text", "text": line, "styles": {}}] if line else [],
            "children": [],
        }
        for index, line in enumerate(text.splitlines() or [""])
    ]
    return {"blocknote": json.dumps(blocks)}


#: A ticket key written at the start of a title, e.g. "BLS-1064: …".
#:
#: Twenty mints its own sequential key per project (BLOY-4, BLOY-5, …), but the
#: ticket people actually refer to is the upstream one, and it is carried in the
#: title. Branches, commits and comments should use the name humans use, so this
#: key wins over Twenty's when present.
_TITLE_KEY = re.compile(r"^\s*([A-Z][A-Z0-9]{1,9}-\d+)\s*[:\-–]\s*")


def split_title_key(title: str) -> tuple[str, str]:
    """Return ``(key, remaining_title)``; key is empty when the title has none."""
    match = _TITLE_KEY.match(title or "")
    if not match:
        return "", (title or "").strip()
    return match.group(1), title[match.end() :].strip()


def normalize_issue(record: dict) -> NormalizedIssue:
    """Map one raw issue record onto :class:`NormalizedIssue`."""
    status = record.get("status") or {}
    raw_title = str(record.get("title") or "")
    title_key, title = split_title_key(raw_title)
    return NormalizedIssue(
        id=str(record.get("id") or ""),
        key=title_key or str(record.get("issueKey") or ""),
        record_key=str(record.get("issueKey") or ""),
        title=title or raw_title,
        body=blocknote_to_text(record.get("description")),
        status_name=str(status.get("name") or ""),
        status_id=str(record.get("statusId") or status.get("id") or ""),
        project_id=str(record.get("projectId") or ""),
        raw=record,
    )


#: What the agent is asked to do when it may not modify the repository.
ANALYSIS_INSTRUCTIONS = """\
Produce an implementation plan, in Vietnamese:
1. Which files and modules must change, with real paths you verified by reading
   the repository. Do not guess paths.
2. The order of work, and which parts can run in parallel.
3. Risks and edge cases the ticket does not cover.
4. What you could not determine from the repository alone.

Do not modify any file."""

#: Marker a ticket carries when the deliverable is a snippet for a human to
#: apply, not a change to this repository. Cosmetic, per-merchant requests are
#: like this: editing the app's own CSS would change every merchant's page,
#: while the customer only wants their own theme adjusted.
ADVICE_MARKER = "deliverable: snippet"

#: Marker a ticket carries when the fix spans more than one sub-project, e.g.
#: ``Repos: shopify-app-loyalty-api, shopify-app-loyalty-cms``. A customer-facing
#: change often needs both — the rule in the API and the screen in the CMS — and
#: a run confined to one repo can only do half of it, or worse, guess.
REPOS_MARKER = re.compile(r"^\s*repos?\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def wanted_repos(issue: NormalizedIssue, known: tuple[str, ...]) -> list[str]:
    """Sub-projects the ticket names, in the order given; empty when it names none."""
    match = REPOS_MARKER.search(issue.body or "")
    if not match:
        return []
    named = [part.strip() for part in re.split(r"[,;]", match.group(1))]
    return [repo for repo in named if repo in known]


def wants_advice(issue: NormalizedIssue) -> bool:
    """True when the ticket asks for a snippet rather than a repository change."""
    return ADVICE_MARKER in (issue.body or "").lower()


#: What the agent is asked to do when the answer *is* the deliverable.
ADVICE_INSTRUCTIONS = """\
Do NOT edit any file in this repository. The deliverable is code a developer will
paste into the merchant's own theme, so changing the app here would apply it to
every merchant.

Answer in Vietnamese, in this order:
1. The exact CSS/JS selectors involved, read from the repository — quote the file
   and line you found them in. Do not invent class names.
2. The snippet to hand over, ready to paste, complete and self-contained.
3. Which part of the request the snippet does NOT cover, if any.
4. Anything that could break: theme overrides, specificity, responsive states."""


#: What the agent is asked to do when it is allowed to write code.
IMPLEMENT_INSTRUCTIONS = """\
Implement the ticket, in this order:
1. Read the repository and confirm the real paths before editing anything.
2. Make the smallest change that satisfies the ticket.
3. Run the relevant tests and report their actual output.
4. List what you changed and what you deliberately left out.

Work only inside this repository. Do not push, deploy, or touch production.

Never modify a dependency lockfile (package-lock.json, yarn.lock, pnpm-lock.yaml
and the like), and avoid commands that rewrite one — prefer `npm ci` over
`npm install`, or read the dependency's source instead of installing it. A
lockfile diff buries the change a reviewer came to read: one ticket shipped a
single meaningful line beside 179 lines of lockfile churn. Any lockfile change
is reverted before committing, so touching one only wastes the run."""

#: Product documentation, mounted with the monorepo. 77 files grouped by feature
#: (earning, redeeming, vip-tiers, referrals, promotions, storefront,
#: use-cases-and-faqs, reference). The agent could always reach it and never did,
#: because nothing pointed at it.
DOCS_DIR = "docs-fts-bloy-loyalty/docs"

#: The reasoning the report must show. Added after a ticket where the code fix
#: was mechanically plausible and still wrong: a POS "Custom amount" gift card
#: arrives with product_id AND variant_id null, so matching ids can never reach
#: it — while the business rule (buying a gift card moves money between tenders,
#: so it is not a purchase and never earns points) gives a fix that always works.
#: The agent had asserted "the line item still carries the product_id" as fact.
BUSINESS_INSTRUCTIONS = """\
Before writing code, work out the BUSINESS rule, not just the code path:

A. What is the product supposed to do here, and why? Read
   {docs}/ — product documentation grouped by feature (earning, redeeming,
   vip-tiers, referrals, promotions, storefront, use-cases-and-faqs). Say which
   file you relied on, or say plainly that the docs do not cover it.
B. State the rule in one sentence a support agent would recognise.
C. Prefer a fix at the level of that rule over one that depends on how a
   merchant happened to configure their settings. A fix that only works when
   the merchant already ticked the right box has not fixed the bug.

Then, in the report, under the exact heading "GIẢ ĐỊNH CHƯA XÁC MINH", list
every claim your change depends on that you could NOT verify by reading this
repository — payload shapes from Shopify, values only present in production
data, third-party behaviour. Write "không có" if there are none.

State these as open questions, not as facts. A confident wrong premise is how
one ticket shipped a fix that could never work, and its tests passed because
they were written against the same wrong premise."""


_PROMPT_TEMPLATE = textwrap.dedent("""\
    You are working on a development ticket for the BLOY Shopify loyalty app.

    Ticket {key}: {title}
    Status: {status}

    Description:
    {body}

    {orientation}

    {business}

    {task}

    Be concrete and short. Do not restate the ticket.
    """)


def build_prompt(
    issue: NormalizedIssue,
    repo: str,
    *,
    implement: bool = False,
    monorepo: str = "",
    advice: bool = False,
    extra_workdirs: list[str] | None = None,
) -> str:
    """Compose the instruction sent to the coding agent.

    ``monorepo`` is the read-only path where the whole monorepo is visible. It
    matters because ``CLAUDE.md`` — the map of which sub-project owns what —
    lives at the monorepo root, outside every sub-project. Without it the agent
    reported "the repo has no CLAUDE.md" and worked blind.
    """
    body = issue.body or "(no description)"
    if advice:
        task = ADVICE_INSTRUCTIONS
    elif implement:
        task = IMPLEMENT_INSTRUCTIONS
    else:
        task = ANALYSIS_INSTRUCTIONS

    if advice and monorepo:
        orientation = textwrap.dedent(f"""\
            {repo} is this ticket's checkout and {monorepo} is the whole monorepo,
            both to be treated as READ-ONLY. Read {monorepo}/CLAUDE.md first: it
            maps which sub-project owns what.

            Find the real selectors in whichever sub-project owns the surface the
            ticket describes — it is often not the same one as {repo}.""")
    elif monorepo and extra_workdirs:
        every = "\n".join(f"  - {path}" for path in [repo, *extra_workdirs])
        orientation = textwrap.dedent(f"""\
            This ticket spans several sub-projects. You may WRITE in any of these
            worktrees, and only these:
            {{every}}

            Start in {repo}. Each is a separate git repository and becomes its own
            merge request, so keep each change self-contained and reviewable on its
            own — do not leave one half depending on an unmerged change in the other.

            The whole monorepo is readable at {monorepo} (read-only). Read
            {monorepo}/CLAUDE.md first: it maps which sub-project owns what.

            If a sub-project turns out not to need changing, leave it untouched —
            an empty merge request is worse than none.""").replace("{every}", every)
    elif monorepo:
        orientation = textwrap.dedent(f"""\
            You may only WRITE inside {repo} — that is this ticket's git worktree.

            The whole monorepo is readable at {monorepo} (read-only). Read
            {monorepo}/CLAUDE.md first: it maps which sub-project owns what.

            If the code this ticket needs lives in a DIFFERENT sub-project than
            {repo}, stop and say so plainly instead of writing something in the
            wrong place — name the sub-project and the files you found.""")
    else:
        orientation = f"The repository is at {repo}."

    # Dedent the template *before* interpolating. Doing it after leaves every
    # framing line indented by eight spaces: the ticket body contains lines that
    # start at column zero, so the common prefix across the whole string is
    # empty and ``dedent`` strips nothing. The model then receives what looks
    # like an indented code block instead of instructions.
    business = (
        BUSINESS_INSTRUCTIONS.format(docs=f"{monorepo}/{DOCS_DIR}") if monorepo else ""
    )
    return _PROMPT_TEMPLATE.format(
        key=issue.key,
        business=business,
        title=issue.title,
        status=issue.status_name,
        body=body,
        orientation=orientation,
        task=task,
    )
