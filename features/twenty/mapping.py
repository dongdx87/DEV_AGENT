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


#: Repos whose changes can plausibly need a look at a real rendered page.
#: Deliberately a structural check (which repo the ticket will touch, already
#: known before the sandbox even starts) rather than anything read out of the
#: ticket's own text — a ticket body is untrusted input, and letting it decide
#: whether its own sandbox gets network access and a deploy token would be
#: letting anyone who can write a ticket grant that to themselves. The finer
#: judgment — does this SPECIFIC change actually affect the UI, or does it
#: just happen to live in this repo — is left to the agent itself once it can
#: see its own diff; see STAGING_INSTRUCTIONS below.
UI_REPOS = {"shopify-app-loyalty-cms"}


def touches_ui_repo(repos: list[str]) -> bool:
    """True when the ticket will touch a repo that can render a UI."""
    return bool(UI_REPOS & set(repos))


#: The exact heading the agent must end its answer with when it decides a
#: ticket is better solved as a snippet than a real code change — see
#: is_snippet_deliverable(). No ticket marker triggers this anymore: the
#: sandbox always has write access (a read-only run that later "changed its
#: mind" about wanting to write code would simply be stuck), and which path
#: was taken is detected AFTER the run from what the agent itself declares.
SNIPPET_DELIVERABLE_HEADING = "DELIVERABLE: SNIPPET"

#: Anchored, not a bare substring check — the ticket body used to be the only
#: thing scanned for the old marker, and trusted human-authored text tolerates
#: a loose `in` check. The agent's own free-form output does not: it might
#: discuss, quote, or paraphrase this exact heading without meaning to declare
#: it. Requires the heading alone on its own line (optionally under a markdown
#: `#`-prefix), not buried mid-paragraph.
_SNIPPET_HEADING_RE = re.compile(
    r"^\s{0,3}#{0,6}\s*DELIVERABLE:\s*SNIPPET\s*$", re.IGNORECASE | re.MULTILINE
)


def is_snippet_deliverable(output: str) -> bool:
    """True when the agent declared its answer a ready-to-paste snippet
    instead of a real repository change — checked against its OWN final
    output, never the ticket body (nothing reads ticket text for this
    decision anymore)."""
    return bool(_SNIPPET_HEADING_RE.search(output or ""))


#: A third real outcome, found live: a ticket can turn out to need NEITHER a
#: code change NOR a snippet, because the ask is already fully solvable with
#: an existing product feature (an Admin setting, an existing API) the agent
#: found by reading the code. Without this, that correct, valuable answer was
#: scored as a plain no-change failure — punishing the agent for correctly
#: noticing no engineering work was needed at all.
NO_CHANGE_NEEDED_HEADING = "DELIVERABLE: NO CHANGE NEEDED"

_NO_CHANGE_NEEDED_RE = re.compile(
    r"^\s{0,3}#{0,6}\s*DELIVERABLE:\s*NO\s+CHANGE\s+NEEDED\s*$", re.IGNORECASE | re.MULTILINE
)


def is_no_change_needed(output: str) -> bool:
    """True when the agent declared the ask already achievable with an
    existing feature — no code, no snippet, just an explanation of how."""
    return bool(_NO_CHANGE_NEEDED_RE.search(output or ""))


#: What the agent is asked to do when it is allowed to write code — which is
#: always, now. The decision this used to gate on a ticket marker (real app
#: change vs. a snippet for one merchant) is now the agent's own first call,
#: made from the ticket's content, not a marker.
IMPLEMENT_INSTRUCTIONS = """\
Before anything else, decide which of these three this ticket actually is:

A. Already achievable with an EXISTING feature (an Admin setting, an existing
   API) — no code, no snippet needed at all. Confirm this by reading the
   actual code/config that proves it works today, not by assuming. Explain
   exactly how (the concrete steps/setting), and end your final answer with
   this exact heading on its own line:

   DELIVERABLE: NO CHANGE NEEDED

B. Satisfiable ENTIRELY by a snippet of code (liquid/CSS/JS) pasted into the
   merchant's OWN theme — with no new logic needed in this app/extension at
   all. Do NOT edit any file in this repository, even if other merchants
   might want something similar. Write the ready-to-paste snippet, complete
   and self-contained, and end your final answer with this exact heading on
   its own line:

   DELIVERABLE: SNIPPET

   For (A) and (B): never use either heading if you actually changed a file,
   and never use both — pick exactly one outcome. Before writing the final
   heading, run `git status --porcelain` yourself in every repo you touched
   and read its ACTUAL output — do not decide from memory of what you meant
   to do. Found live: an agent wrote real code mid-run (a backend field, a
   CSS progress bar), then in its final answer described that exact code —
   quoting its own comment word for word — as something it had merely
   *found* already existing, and picked (A) anyway. It never re-checked its
   own diff before concluding. If `git status` shows anything, you are in
   case C below, no matter what you remember deciding earlier in the run.
   For (B) also include: the exact CSS/JS selectors involved (quote the file
   and line you found them in — do not invent class names), what the snippet
   does NOT cover if anything, and what could break it (theme overrides,
   specificity, responsive states).

C. Genuinely needs logic that only belongs in this app/extension (shared
   state, a helper multiple components need, data only the app can read) —
   even if the request came from just one merchant, and even if no existing
   feature covers it. Implement it, in this order:
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
is reverted before committing, so touching one only wastes the run.

Any user-facing text you add or change in shopify-app-loyalty-cms must be
translated into EVERY locale the app already ships, not left English-only.
List the real folders under web/frontend/locales/ yourself — do not assume a
count or a fixed list, it changes over time — and add the same key to each
one's JSON file, matching the phrasing/placeholder style of the nearest
existing key already in that same file. This is not optional follow-up work:
a run that ships a new string in en/common.json alone and reports the other
locales as "left out" has not finished the ticket."""

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


@dataclass(frozen=True)
class StagingContext:
    """What the prompt needs to describe staging-verify capability — nothing
    the agent needs to reach the host, a secret, or any other endpoint with.
    """

    #: Base URL of staging_control — the ONLY thing this run may talk to
    #: beyond api.anthropic.com/registry.npmjs.org/the two staging app domains
    #: already fixed in sandbox_runner.STAGING_EGRESS_ALLOW.
    control_base_url: str
    #: Where inside the container to save screenshots — see agent_log's
    #: container_artifacts_dir(); a run_id-scoped path under the read-write
    #: worktree mount, so files written there survive the container's death.
    artifacts_dir: str
    apps: tuple[str, ...] = ("api", "cms")
    #: The real dev storefront, for changes visible to a SHOPPER rather than
    #: an Admin. Left blank means "not configured" — STOREFRONT_VERIFY_INSTRUCTIONS
    #: is only appended when this is truthy, same graceful-absence pattern as
    #: skill_packs_root/monorepo_mirror elsewhere in this codebase.
    storefront_url: str = ""
    #: A public dev-storefront password gate, not a real credential — see
    #: STOREFRONT_VERIFY_INSTRUCTIONS for why it is safe to hand to the agent
    #: directly rather than treating it like the Admin session cookie.
    storefront_password: str = ""


#: Appended after _PROMPT_TEMPLATE's own output, never interpolated into it —
#: this is what keeps build_prompt(staging=None) byte-identical to today for
#: every ticket that never touches this path.
STAGING_INSTRUCTIONS = textwrap.dedent("""\

    ## Staging verify

    This ticket touches a UI-capable repo, so this run ALSO has a real staging
    deploy target and a headless Chromium (via the Playwright MCP tool) to look
    at the real Shopify Admin embedded app after deploying your change.

    You have exactly these 4 endpoints, and nothing else on the host — no
    shell, no SSH, no filesystem outside this worktree and $HOME:

      POST {base}/v1/deploy   {{"app": "api" | "cms"}}
      POST {base}/v1/restart  {{"app": "api" | "cms" | "all"}}
      GET  {base}/v1/status
      GET  {base}/v1/logs?app=api|cms&lines=200

    Send `Authorization: Bearer $BLOY_STAGING_TOKEN` (already in your shell
    environment) on every request.

    This session has no notification mechanism and will not be resumed:
    unlike the outer conversation that started this run, there is no external
    event that wakes this run back up once it goes idle. If `/v1/deploy`
    answers 409 (another deploy in flight) or 429 (rate-limited), retry it
    yourself, synchronously, in the SAME turn — a `sleep 20 && curl ...` loop,
    or repeated foreground calls — until it actually finishes or clearly
    fails. Never launch it as a detached background task and end your turn
    "waiting for its completion" or "waiting for a notification" — found
    live: an agent did exactly that, correctly reasoning the deploy would
    eventually finish, but nothing in this sandbox was ever going to notify
    it, so the run ended right there with the screenshot never taken, even
    though the code change itself was already safely committed and pushed. A
    real `shopify app deploy` here can take a couple of minutes to build
    every extension — budget time for that within your own turn, don't treat
    it as something you can defer to later.

    Decide for yourself whether this is worth doing: only actually deploy and
    take screenshots if what you changed can plausibly affect what a merchant
    or admin SEES. If your change turned out to be purely internal logic in
    this repo with no visible effect at all (e.g. a backend job, a type, a
    helper nothing renders from), say so in your report and skip this — that
    is a correct outcome, not a failure.

    That is the ONLY valid reason to skip. "I can't reproduce the exact bug
    condition (specific data, a specific merchant config) to demonstrate the
    fix" is NOT a reason to skip — it is a reason to do a plain regression
    check instead of a demonstration: deploy, open the actual screen/widget
    your diff touches, and screenshot whatever state you CAN reach (even the
    ordinary, un-broken case). That still proves the deploy didn't break
    normal rendering, which a diff alone never proves. Say plainly in your
    report that you could not reproduce the specific condition — do not
    present the regression screenshot as if it demonstrated the fix — but
    take it regardless.

    How to reach the app — this order matters: navigate to
    `https://admin.shopify.com` FIRST (nothing else — no store slug, no app
    path). If a session is loaded, this redirects you straight into the real
    Admin dashboard, already logged in. From there, find and click the app
    itself (its display name is the `name` field in the target repo's
    `shopify.app.toml` — read it if you don't already know it) using Admin's
    own UI (the "Apps" entry in the left sidebar, or the search bar) to open
    it, then navigate inside it to the screen you actually need. NEVER
    construct or guess a direct URL into the app's own embedded route
    (e.g. anything under `/apps/<handle>/...`) — Shopify embedded apps need a
    session-token handshake that only happens when Admin's own UI opens them;
    jumping straight to that URL fails with `no_cookie_session` even with a
    perfectly valid saved session, and that failure means nothing about
    whether your code change is correct.

    If you do verify: save every screenshot under {artifacts_dir}/, named like
    `[A-Za-z0-9_-]+\\.png` — any other name will not be servable afterward. If
    the browser opens to a Shopify login screen instead of the app (no saved
    session, or it expired), stop there, note it plainly in your report, and
    move on — this is expected sometimes and must NEVER be treated as a
    failure of the ticket itself; your code change still stands on its own.

    Only if you actually deployed AND took a real screenshot, your final
    report MUST include the exact heading "ĐÃ VERIFY TRÊN STAGING", listing
    which app(s) you deployed and which screenshot(s) you took. Do not use
    that heading otherwise.
    """)


#: Appended after STAGING_INSTRUCTIONS, only when a StagingContext carries a
#: storefront_url — most tickets touching this repo affect the Admin embedded
#: app above, not the storefront, so this stays absent by default rather than
#: padding every prompt with an irrelevant section.
STOREFRONT_VERIFY_INSTRUCTIONS = textwrap.dedent("""\

    ## Storefront verify

    You can also reach the real storefront directly at {storefront_url} — use
    this when your change affects what a SHOPPER sees (the loyalty widget, a
    popup, the cart drawer...), not the Admin screen above.

    Navigate there directly. If you land on a page asking for a store
    password, it is `{storefront_password}` — this is NOT a real credential,
    just a temporary dev-store gate, safe to enter and submit. Then interact
    with whatever you actually need to check (open a product, add it to cart,
    etc.) and save screenshots the same way, under {artifacts_dir}/.
    """)


_PROMPT_TEMPLATE = textwrap.dedent("""\
    You are working on a development ticket for the BLOY Shopify loyalty app.

    Ticket {key}: {title}
    Status: {status}

    Description:
    {body}

    {orientation}

    {business}

    {skills}

    {task}

    Be concrete and short. Do not restate the ticket.
    """)


def _skills_block(enabled_skills: list[tuple[str, str]] | None) -> str:
    """A catalog of enabled skills, not their content.

    Only the name and description reach the prompt — the same shape agent_team
    uses (``cli_context.py``'s manifest) — because the agent can read a
    ``SKILL.md`` itself once it recognises it applies. Nailing every skill's
    full text into the prompt would grow it on every ticket whether or not the
    skill is relevant.
    """
    if not enabled_skills:
        return ""
    lines = [
        "## Available skills",
        "",
        "Each is a folder at ~/.claude/skills/<name>/SKILL.md. Read one when it "
        "matches what you are doing — it is not loaded into this prompt for you.",
        "",
    ]
    lines += [f"- `{name}`: {description}" for name, description in enabled_skills]
    return "\n".join(lines)


def build_prompt(
    issue: NormalizedIssue,
    repo: str,
    *,
    implement: bool = False,
    monorepo: str = "",
    extra_workdirs: list[str] | None = None,
    enabled_skills: list[tuple[str, str]] | None = None,
    staging: StagingContext | None = None,
) -> str:
    """Compose the instruction sent to the coding agent.

    ``monorepo`` is the read-only path where the whole monorepo is visible. It
    matters because ``CLAUDE.md`` — the map of which sub-project owns what —
    lives at the monorepo root, outside every sub-project. Without it the agent
    reported "the repo has no CLAUDE.md" and worked blind.

    ``staging=None`` (every ticket that never touches a UI-capable repo) must
    produce byte-identical output to before this parameter existed — the
    staging block is only ever appended to the end, never woven into
    ``_PROMPT_TEMPLATE`` itself.
    """
    body = issue.body or "(no description)"
    task = IMPLEMENT_INSTRUCTIONS if implement else ANALYSIS_INSTRUCTIONS

    if monorepo and extra_workdirs:
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
    text = _PROMPT_TEMPLATE.format(
        key=issue.key,
        business=business,
        skills=_skills_block(enabled_skills),
        title=issue.title,
        status=issue.status_name,
        body=body,
        orientation=orientation,
        task=task,
    )
    if staging is None:
        return text
    text += STAGING_INSTRUCTIONS.format(
        base=staging.control_base_url, artifacts_dir=staging.artifacts_dir
    )
    if staging.storefront_url:
        text += STOREFRONT_VERIFY_INSTRUCTIONS.format(
            storefront_url=staging.storefront_url,
            storefront_password=staging.storefront_password,
            artifacts_dir=staging.artifacts_dir,
        )
    return text
