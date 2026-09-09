"""The standalone BLOY Dev Agent service.

Runs as its own process on its own port, with its own database and UI. BAM keeps
a thin plugin that links here and can trigger a pass over HTTP, but nothing in
this file needs BAM to be up.

That separation is the whole point. While the pipeline lived inside BAM, a BAM
restart killed the run mid-flight — BAM's routine reconciler shut down the
scheduler process that was driving it, leaving an orphaned container and an
issue stranded in "In Progress". Here a BAM restart is irrelevant.

Only one pass runs at a time. A pass is minutes of container time and the
concurrency ceiling for this agent is three issues, so a lock plus a background
thread is the whole scheduler it needs.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from bloy_dev_agent import preflight, setup_wizard, store
from bloy_dev_agent.db import database_url, init_db
from bloy_dev_agent.features import (
    agent_log,
    pipeline,
    sandbox_runner,
    skill_packs,
    workspace,
)
from bloy_dev_agent.models import BloyPipelineRun
from bloy_dev_agent.staging_control import tokens as staging_tokens

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))


def _rel(path: str) -> str:
    """A root-absolute path ("/x"), rewritten to resolve against ``<base>``
    instead of the domain root.

    Every in-app link/action/src in these templates must go through this
    (see base.html's ``<base href="{{ base_path }}/">``) rather than being
    written as a literal ``href="/x"`` — a literal one always points at the
    domain root, which is wrong the moment this service is reverse-proxied
    under a subpath (e.g. nginx serving it at ``/dev-agent/``). A URL that is
    already absolute to another origin (an MR link, ``bam_url()``) must NOT
    go through this — those are unaffected by ``<base>`` either way.
    """
    return path[1:] or "."


TEMPLATES.env.globals["rel"] = _rel

DEFAULT_PORT = 8100

#: Where the sub-projects are cloned from when the setup page fills in a
#: missing one. Overridable from that page for a different group or fork.
DEFAULT_GIT_REMOTE = (
    "git@sbc-gitlab.bsscommerce.com:sa-division/tc-team/shopify-app-loyalty"
)
PAGE_TITLE = "BLOY Dev Agent"


#: Files searched for Twenty credentials, first hit wins. The service is
#: launched by PM2, cron and a shell in turn, and none of those reliably carry
#: an exported environment — so it loads its own rather than trusting the
#: launcher. BAM's ``.env`` is included so the credentials live in one place.
ENV_CANDIDATES = (
    PROJECT_ROOT / ".env",
    PROJECT_ROOT.parent / "agent-manager" / ".env",
)


def load_env() -> str:
    """Fill missing variables from the first env file found; return its path.

    Never overwrites a variable already set: an explicit value on the command
    line or in the PM2 config must win over a file on disk.
    """
    configured = os.environ.get("BLOY_AGENT_ENV_FILE", "").strip()
    candidates = (Path(configured),) if configured else ENV_CANDIDATES

    for path in candidates:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")
        return str(path)
    return ""


def port() -> int:
    try:
        return int(os.environ.get("BLOY_AGENT_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def bam_url() -> str:
    """BAM's address for THIS PROCESS to call — server-to-server, never seen
    by a browser. Correct as ``http://localhost:8000`` whenever this service
    and BAM run on the same host, which is the common case; see
    ``bam_public_url()`` for the separate address a browser needs.
    """
    return os.environ.get("BAM_URL", "http://localhost:8000").rstrip("/")


def bam_public_url() -> str:
    """BAM's address for the BROWSER to follow — the "BAM console"/"Agent
    Routine" links in the sidebar, and the same link inside the stale-trigger
    banner (see trigger_health()).

    Deliberately separate from bam_url(): that one is correct as
    ``http://localhost:8000`` for this service's own server-to-server calls
    into BAM on the same host, and exactly as wrong for a browser link as
    ``DEFAULT_SERVICE_URL`` was for this service's own sidebar entry in
    plugin.py — same bug, same fix, just the other direction (this service
    linking OUT to BAM instead of BAM linking IN to this service). Defaults
    to ``bam_url()`` so a deployment that never sets this stays byte-identical
    to today.
    """
    return os.environ.get("BAM_PUBLIC_URL", bam_url()).rstrip("/")


def base_path() -> str:
    """URL prefix this service is reverse-proxied under, if any.

    Empty by default (served at its own domain root). Set ``BLOY_AGENT_URL``
    on the BAM side to include the same prefix, and set this env var here so
    the pages this service renders know to write their own links relative to
    it — see ``_rel()`` and base.html's ``<base>`` tag.
    """
    return os.environ.get("BLOY_AGENT_BASE_PATH", "").rstrip("/")


# ---------------------------------------------------------------------------
# One pass at a time
# ---------------------------------------------------------------------------


class PassRunner:
    """Serialises pipeline passes and reports whether one is in flight.

    ``try_start`` returns False rather than queueing when a pass is already
    running. A queue would let a minute-by-minute routine pile up hours of work
    it can never catch up on; refusing is both honest and cheaper.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started_at: datetime | None = None
        self._last_summary: dict | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    @property
    def last_summary(self) -> dict | None:
        return self._last_summary

    def try_start(self, config: dict) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._started_at = datetime.now(UTC)
            self._thread = threading.Thread(
                target=self._run, args=(config,), name="bloy-pass", daemon=True
            )
            self._thread.start()
            return True

    def _run(self, config: dict) -> None:
        """Drive one pass of the requested kind.

        The two entry points are called by name here rather than looked up in a
        table of function objects: a table built at import time captures the
        originals, so patching ``service.run_pass_from_config`` — which is how
        this class is tested at all — would silently have no effect.
        """
        mode = str(config.get("mode") or "new")
        if mode not in ("new", "feedback"):
            self._last_summary = {"error": f"mode không hợp lệ: {mode!r}"}
            return
        try:
            self._last_summary = (
                run_feedback_from_config(config)
                if mode == "feedback"
                else run_pass_from_config(config)
            )
        except Exception as exc:  # noqa: BLE001 — a crash must not kill the service
            logger.exception("bloy_dev_agent: pass %r failed", mode)
            self._last_summary = {"error": str(exc)}


RUNNER = PassRunner()


def _reap_on_boot() -> None:
    """Clean up after a killed process: close its runs and free their issues.

    Without this, a restart leaves the issue sitting in the working column, where
    the source-column query can never see it again — which is exactly how BLOY-2
    disappeared from the board twice.
    """
    stale = store.reap_stale_runs()

    # A sandbox outlives the process that made it: killing the service mid-run
    # leaves the container burning CPU until its own timeout. Nothing is in
    # flight at boot, so every sandbox of ours is an orphan.
    try:
        killed = sandbox_runner.reap_orphan_sandboxes(active_run_ids=set())
        if killed:
            logger.warning(
                "bloy_dev_agent: đã dọn %d sandbox mồ côi: %s",
                len(killed), ", ".join(killed),
            )
    except Exception:  # noqa: BLE001 — boot must not fail on the sweep
        logger.exception("bloy_dev_agent: orphan sandbox sweep failed")

    # Same reasoning as the run/sandbox reap above: only this process ever
    # runs a pass, so any staging token still unrevoked at boot belongs to a
    # run a killed process never finished.
    try:
        revoked = staging_tokens.revoke_all_active()
        if revoked:
            logger.warning(
                "bloy_dev_agent: đã revoke %d staging token còn sống từ trước", revoked
            )
    except Exception:  # noqa: BLE001 — boot must not fail on this sweep either
        logger.exception("bloy_dev_agent: staging token reap failed")

    saved = store.get_settings()
    project_id = saved.get(store.SETTING_PROJECT_ID) or ""

    try:
        client = _twenty_client()
    except RuntimeError as exc:
        logger.warning(
            "bloy_dev_agent: %d run(s) reaped but Twenty is unreachable (%s); "
            "their issues stay in the working column",
            len(stale),
            exc,
        )
        return

    # Sweep the working column itself, not just the rows we happen to have.
    # An issue can end up stranded there without any run row to match — a
    # timed-out claim used to do exactly that — so the column is the source of
    # truth for "is anything actually working on this".
    if project_id:
        try:
            released = pipeline.release_stranded_issues(
                client,
                project_id=project_id,
                working_status=(
                    saved.get(store.SETTING_WORKING_STATUS)
                    or pipeline.DEFAULT_WORKING_STATUS
                ),
                source_status=(
                    saved.get(store.SETTING_SOURCE_STATUS)
                    or pipeline.DEFAULT_SOURCE_STATUS
                ),
            )
            if released:
                logger.warning(
                    "bloy_dev_agent: đã trả %d issue kẹt về cột nguồn: %s",
                    len(released),
                    ", ".join(released),
                )
        except Exception:  # noqa: BLE001 — boot must not fail on the sweep
            logger.exception("bloy_dev_agent: stranded-issue sweep failed")


def twenty_credentials() -> tuple[str, str]:
    """``(base_url, api_key)`` from the environment, else from Settings.

    The environment wins so a deployment that injects secrets keeps working,
    but Settings means a fresh machine can be configured entirely from the
    browser instead of by hand-editing a file.
    """
    saved = store.get_settings()
    base = (
        os.environ.get("BLOY_TWENTY_BASE_URL", "").strip()
        or saved.get(store.SETTING_TWENTY_URL, "").strip()
    )
    key = (
        os.environ.get("BLOY_TWENTY_API_KEY", "").strip()
        or saved.get(store.SETTING_TWENTY_KEY, "").strip()
    )
    return base, key


def _twenty_client():
    from bloy_dev_agent.features.twenty.client import TwentyClient

    base, key = twenty_credentials()
    if not base or not key:
        raise RuntimeError("Twenty chưa được cấu hình — điền ở trang Setup")
    return TwentyClient(base_url=base, api_key=key)


def run_pass_from_config(config: dict) -> dict:
    """Run one pass, filling anything the caller left out from settings."""
    saved = store.get_settings()

    def pick(key: str, fallback: str) -> str:
        return str(config.get(key) or saved.get(key) or fallback)

    project_id = pick(store.SETTING_PROJECT_ID, "")
    if not project_id:
        return {"error": "project_id chưa được cấu hình"}

    monorepo = Path(pick(store.SETTING_MONOREPO, str(pipeline.default_monorepo())))
    if not monorepo.is_dir():
        return {"error": f"Repository path không tồn tại: {monorepo}"}

    try:
        timeout = int(pick(store.SETTING_TIMEOUT_MINUTES, "0") or 0)
    except ValueError:
        timeout = 0

    skills_root = Path(pick(store.SETTING_SKILLS_ROOT, str(skill_packs.DEFAULT_SKILLS_ROOT)))
    enabled_names = tuple(
        skill_packs.parse_enabled(saved.get(store.SETTING_ENABLED_SKILLS) or "")
    )

    return pipeline.run_pass(
        _twenty_client(),
        project_id=project_id,
        monorepo=monorepo,
        target_repo=pick(store.SETTING_TARGET_REPO, pipeline.DEFAULT_TARGET_REPO),
        source_status=pick(store.SETTING_SOURCE_STATUS, pipeline.DEFAULT_SOURCE_STATUS),
        working_status=pick(store.SETTING_WORKING_STATUS, pipeline.DEFAULT_WORKING_STATUS),
        done_status=pick(store.SETTING_DONE_STATUS, pipeline.DEFAULT_DONE_STATUS),
        error_status=pick(store.SETTING_ERROR_STATUS, pipeline.DEFAULT_ERROR_STATUS),
        blocked_status=pick(store.SETTING_BLOCKED_STATUS, pipeline.DEFAULT_BLOCKED_STATUS),
        max_issues=int(config.get("max_issues") or 1),
        timeout_minutes=timeout or 30,
        skill_packs_root=skills_root,
        enabled_skill_names=enabled_names,
    )


def run_feedback_from_config(config: dict) -> dict:
    """Scan the review columns for reviewer feedback and act on it.

    Same shape as :func:`run_pass_from_config` and deliberately a *separate*
    pass rather than a step inside it: a feedback round costs a container just
    like new work does, and an operator who wants only one of the two (say,
    finishing the review queue before picking up anything new) has to be able
    to run them apart. Both go through the same single-flight
    :class:`PassRunner`, so they never share a host with each other either.
    """
    saved = store.get_settings()

    def pick(key: str, fallback: str) -> str:
        return str(config.get(key) or saved.get(key) or fallback)

    project_id = pick(store.SETTING_PROJECT_ID, "")
    if not project_id:
        return {"error": "project_id chưa được cấu hình"}

    monorepo = Path(pick(store.SETTING_MONOREPO, str(pipeline.default_monorepo())))
    if not monorepo.is_dir():
        return {"error": f"Repository path không tồn tại: {monorepo}"}

    try:
        timeout = int(pick(store.SETTING_TIMEOUT_MINUTES, "0") or 0)
    except ValueError:
        timeout = 0

    skills_root = Path(
        pick(store.SETTING_SKILLS_ROOT, str(skill_packs.DEFAULT_SKILLS_ROOT))
    )
    enabled_names = tuple(
        skill_packs.parse_enabled(saved.get(store.SETTING_ENABLED_SKILLS) or "")
    )

    done_status = pick(store.SETTING_DONE_STATUS, pipeline.DEFAULT_DONE_STATUS)
    blocked_status = pick(store.SETTING_BLOCKED_STATUS, pipeline.DEFAULT_BLOCKED_STATUS)

    return pipeline.run_feedback_pass(
        _twenty_client(),
        project_id=project_id,
        monorepo=monorepo,
        target_repo=pick(store.SETTING_TARGET_REPO, pipeline.DEFAULT_TARGET_REPO),
        # Both columns a reported ticket can be sitting in. A reviewer
        # correcting work the evaluator rejected is as much a revision request
        # as one correcting work it accepted — arguably more.
        review_statuses=tuple(dict.fromkeys((done_status, blocked_status))),
        working_status=pick(store.SETTING_WORKING_STATUS, pipeline.DEFAULT_WORKING_STATUS),
        done_status=done_status,
        error_status=pick(store.SETTING_ERROR_STATUS, pipeline.DEFAULT_ERROR_STATUS),
        blocked_status=blocked_status,
        max_issues=int(config.get("max_issues") or 1),
        timeout_minutes=timeout or 30,
        skill_packs_root=skills_root,
        enabled_skill_names=enabled_names,
    )


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------


def _elapsed(run: BloyPipelineRun) -> str:
    start = run.started_at
    if start is None:
        return "—"
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    end = run.finished_at or datetime.now(UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    seconds = max(0, int((end - start).total_seconds()))
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


def _merge_requests(run: BloyPipelineRun) -> list[tuple[str, str]]:
    """Every merge request the run opened, oldest schema tolerated.

    Rows written before multi-repo support carry only ``merge_request_url``;
    they still have to render, so the single URL is presented as one unnamed
    entry rather than vanishing from the history table.
    """
    raw = getattr(run, "merge_requests_json", None)
    if raw:
        try:
            return [(str(a), str(b)) for a, b in json.loads(raw)]
        except (ValueError, TypeError):
            logger.warning("bloy_dev_agent: unreadable merge_requests_json on %s", run.id)
    return [("", run.merge_request_url)] if run.merge_request_url else []


def _run_view(run: BloyPipelineRun) -> dict:
    return {
        "id": run.id,
        "issue_id": run.issue_id,
        "issue_key": run.issue_key,
        "issue_title": run.issue_title or "",
        "attempt": run.attempt,
        "state": run.state,
        "stage": run.stage or "",
        "branch": run.branch or "",
        "merge_request_url": run.merge_request_url or "",
        "merge_requests": _merge_requests(run),
        "sandbox_id": run.sandbox_id or "",
        "target_repo": run.target_repo or "",
        "changed": run.changed or "",
        "detail": run.detail or "",
        "output": run.output or "",
        "elapsed": _elapsed(run),
        "stages": list(BloyPipelineRun.STAGES),
        "stage_index": (
            BloyPipelineRun.STAGES.index(run.stage)
            if run.stage in BloyPipelineRun.STAGES
            else -1
        ),
    }


#: How long the service may sit untriggered before the page says so. BAM's
#: routine fires every two minutes, so anything past this means its scheduler is
#: gone — which happens on every BAM restart and used to be invisible.
STALE_TRIGGER_MINUTES = 15


def trigger_health() -> dict:
    """Whether anything is still calling this service, and how to fix it."""
    idle = store.minutes_since_last_pass()
    stale = idle is not None and idle >= STALE_TRIGGER_MINUTES
    return {
        "idle_minutes": None if idle is None else int(idle),
        "stale": bool(stale),
        "routine_url": f"{bam_public_url()}/agent-routine",
    }


def _redirect(path: str, status_code: int = 303) -> RedirectResponse:
    """Like ``RedirectResponse`` but ``path`` gets the same subpath prefix as
    every in-page link (see ``_rel()``) — a bare ``Location: /settings``
    header is just as wrong behind a reverse proxy as a bare ``href`` would
    be, since the browser resolves it against the domain root either way.
    """
    return RedirectResponse(url=f"{base_path()}{path}", status_code=status_code)


def _shell(request: Request, **context) -> dict:
    """Context every page needs for the sidebar."""
    return {
        "request": request,
        "port": port(),
        "bam_url": bam_public_url(),
        "base_path": base_path(),
        **context,
    }


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(title=PAGE_TITLE, docs_url="/api/docs", redoc_url=None)
    env_file = load_env()
    init_db()
    logger.info(
        "bloy_dev_agent: database at %s, env from %s",
        database_url(),
        env_file or "process environment only",
    )
    _reap_on_boot()

    # ---------------- pages ----------------

    @app.get("/")
    def dashboard(request: Request):
        checks = preflight.run_checks()
        return TEMPLATES.TemplateResponse(
            request=request,
            name="bloy_dashboard.html",
            context=_shell(
                request,
                title=PAGE_TITLE,
                active=[_run_view(r) for r in store.active_runs()],
                history=[_run_view(r) for r in store.recent_runs(limit=25)],
                blocked=[_run_view(r) for r in store.blocked_issues()],
                counts=preflight.summarise(checks),
                max_attempts=store.max_attempts(),
                busy=RUNNER.busy,
                trigger=trigger_health(),
            ),
        )

    @app.get("/runs/{run_id}")
    def run_detail(request: Request, run_id: str):
        run = store.get_run(run_id)
        if run is None:
            return _redirect("/")
        events = agent_log.parse(Path(run.log_path)) if run.log_path else []
        artifacts = agent_log.list_artifacts(workspace.default_worktree_root(), run_id)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="bloy_run.html",
            context=_shell(
                request,
                title=f"{run.issue_key} · {PAGE_TITLE}",
                run=_run_view(run),
                events=[asdict(e) for e in events],
                live=run.state == BloyPipelineRun.STATE_RUNNING,
                artifacts=artifacts,
                # The loop's own record. This is the page a reviewer lands on
                # before deciding whether to trust a merge request, and the
                # receipts are the only part of it the agent did not author —
                # so they belong here, not only in the Twenty comment.
                attempts=store.attempts_for(run_id),
                receipts=store.receipts_for(run_id),
            ),
        )

    @app.get("/runs/{run_id}/artifacts/{filename}")
    def run_artifact(run_id: str, filename: str):
        """One staging-verify screenshot, or a 404 — never a raw path join.

        The filename is checked against the whitelist BEFORE it ever reaches a
        path, matching the discipline elsewhere in this codebase (see
        staging_control/apps.py's docstring): a crafted value must never reach
        an arbitrary file, and FastAPI's own path-segment routing already
        refuses a "/" in ``filename``, so a ".." cannot escape the directory
        even before the regex runs.
        """
        if not agent_log.ARTIFACT_NAME.match(filename):
            raise HTTPException(status_code=400, detail="tên file không hợp lệ")
        path = agent_log.host_artifacts_dir(workspace.default_worktree_root(), run_id) / filename
        if not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="image/png")

    @app.get("/settings")
    def settings_page(request: Request):
        checks = preflight.run_checks()
        return TEMPLATES.TemplateResponse(
            request=request,
            name="bloy_settings.html",
            context=_shell(
                request,
                title=f"Settings · {PAGE_TITLE}",
                settings=store.get_settings(),
                max_attempts=store.max_attempts(),
                loop_enabled=store.loop_enabled(),
                feedback_enabled=store.feedback_enabled(),
                loop_budget=store.loop_budget(),
                verify_commands=store.verify_commands_raw(),
                repos=list(workspace.KNOWN_REPOS),
                counts=preflight.summarise(checks),
            ),
        )

    @app.post("/settings")
    def save_settings_route(
        max_attempts: str = Form(default=""),
        project_id: str = Form(default=""),
        target_repo: str = Form(default=""),
        source_status: str = Form(default=""),
        working_status: str = Form(default=""),
        done_status: str = Form(default=""),
        error_status: str = Form(default=""),
        blocked_status: str = Form(default=""),
        timeout_minutes: str = Form(default=""),
        comments_disabled: str = Form(default=""),
        loop_enabled: str = Form(default=""),
        loop_max_attempts: str = Form(default=""),
        loop_max_tokens: str = Form(default=""),
        loop_max_cost_usd: str = Form(default=""),
        verify_commands: str = Form(default=""),
        feedback_enabled: str = Form(default=""),
    ):
        store.save_settings(
            {
                store.SETTING_MAX_ATTEMPTS: max_attempts.strip(),
                store.SETTING_PROJECT_ID: project_id.strip(),
                store.SETTING_TARGET_REPO: target_repo.strip(),
                store.SETTING_SOURCE_STATUS: source_status.strip(),
                store.SETTING_WORKING_STATUS: working_status.strip(),
                store.SETTING_DONE_STATUS: done_status.strip(),
                store.SETTING_ERROR_STATUS: error_status.strip(),
                store.SETTING_BLOCKED_STATUS: blocked_status.strip(),
                store.SETTING_TIMEOUT_MINUTES: timeout_minutes.strip(),
                # An unchecked checkbox submits nothing at all, not "off" —
                # the stored value must still change to "" on that submit, or
                # turning the toggle back off from the form would do nothing.
                store.SETTING_COMMENTS_DISABLED: "on" if comments_disabled.strip() else "",
                store.SETTING_LOOP_MAX_ATTEMPTS: loop_max_attempts.strip(),
                store.SETTING_LOOP_MAX_TOKENS: loop_max_tokens.strip(),
                store.SETTING_LOOP_MAX_COST_USD: loop_max_cost_usd.strip(),
                # Kept verbatim, newlines and all: this is the operator's
                # verify command list, and the parser that has to reject a
                # malformed line lives with the runner (see
                # features/coding/receipts.py) so a mistake is loud there
                # rather than silently trimmed here.
                store.SETTING_VERIFY_COMMANDS: verify_commands.strip(),
                # These two default to ON, so unlike comments_disabled above
                # the *absent* checkbox has to be stored as an explicit "off"
                # rather than as "" — an empty value would read back as the
                # default, and the toggle could never be turned off.
                store.SETTING_LOOP_ENABLED: "on" if loop_enabled.strip() else "off",
                store.SETTING_FEEDBACK_ENABLED: (
                    "on" if feedback_enabled.strip() else "off"
                ),
            }
        )
        return _redirect("/settings")

    # ---------------- skills ----------------
    #
    # This is a *selector*, not an editor. Authoring happens in the shared
    # skill-pack store BAM's own Skill Packs page already manages (import from
    # git, sync, versioning); duplicating that here would give the team two
    # places to look for the same thing. This page only reads that store off
    # disk and remembers which of its packs this agent should use.

    def _skills_root() -> Path:
        saved = store.get_settings()
        return Path(
            saved.get(store.SETTING_SKILLS_ROOT) or str(skill_packs.DEFAULT_SKILLS_ROOT)
        )

    @app.get("/skills")
    def skills_page(request: Request):
        saved = store.get_settings()
        root = _skills_root()
        enabled = set(skill_packs.parse_enabled(saved.get(store.SETTING_ENABLED_SKILLS) or ""))
        packs = skill_packs.list_packs(root)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="bloy_skills.html",
            context=_shell(
                request,
                title=f"Skills · {PAGE_TITLE}",
                skills_root=str(root),
                root_exists=root.is_dir(),
                packs=[
                    {"name": p.name, "description": p.description, "path": str(p.path)}
                    for p in packs
                ],
                enabled=enabled,
            ),
        )

    @app.post("/skills")
    def save_skills_route(
        skills_root: str = Form(default=""),
        skill: list[str] = Form(default=[]),
    ):
        # Every submitted name is checked against the actual catalog rather
        # than trusted as-is, so a crafted field cannot enable something that
        # names no real pack.
        root = Path(skills_root.strip()) if skills_root.strip() else _skills_root()
        known = {p.name for p in skill_packs.list_packs(root)}
        chosen = [name for name in skill if name in known]
        values = {store.SETTING_ENABLED_SKILLS: ",".join(chosen)}
        # A blank field means "leave the root alone", not "erase it" — the
        # form always renders the current root as the input's value, so a
        # blank submit here only happens if a caller strips it deliberately.
        if skills_root.strip():
            values[store.SETTING_SKILLS_ROOT] = skills_root.strip()
        store.save_settings(values)
        return _redirect("/skills")

    # ---------------- setup ----------------

    def _setup_context(request: Request, message: str = "") -> dict:
        saved = store.get_settings()
        url, key = twenty_credentials()
        monorepo = Path(saved.get(store.SETTING_MONOREPO) or pipeline.default_monorepo())
        steps = setup_wizard.diagnose(
            monorepo=monorepo,
            worktree_root=workspace.default_worktree_root(),
            repos=workspace.KNOWN_REPOS,
            twenty_url=url,
            twenty_key=key,
            skill_packs_root=Path(
                saved.get(store.SETTING_SKILLS_ROOT) or skill_packs.DEFAULT_SKILLS_ROOT
            ),
            monorepo_mirror=sandbox_runner.DEFAULT_MONOREPO_MIRROR,
            shopify_auth_dir=sandbox_runner.SHOPIFY_AUTH_DIR,
            agent_repos_root=workspace.default_agent_repos_root(),
        )
        counts: dict[str, int] = {}
        for step in steps:
            counts[step.state] = counts.get(step.state, 0) + 1
        return _shell(
            request,
            title=f"Setup · {PAGE_TITLE}",
            steps=steps,
            counts=counts,
            ready=counts.get(setup_wizard.FAIL, 0) == 0,
            message=message,
            twenty_url=url,
            twenty_key=key,
            monorepo=str(monorepo),
            git_remote=saved.get(store.SETTING_GIT_REMOTE) or DEFAULT_GIT_REMOTE,
        )

    @app.get("/setup")
    def setup_page(request: Request):
        return TEMPLATES.TemplateResponse(
            request=request, name="bloy_setup.html", context=_setup_context(request)
        )

    @app.post("/setup")
    def save_setup(
        twenty_base_url: str = Form(default=""),
        twenty_api_key: str = Form(default=""),
        monorepo: str = Form(default=""),
        git_remote: str = Form(default=""),
    ):
        values = {
            store.SETTING_TWENTY_URL: twenty_base_url.strip(),
            store.SETTING_MONOREPO: monorepo.strip(),
            store.SETTING_GIT_REMOTE: git_remote.strip(),
        }
        # An empty key field means "leave it alone", not "erase it" — the form
        # renders the stored key as a password input and a blank submit would
        # otherwise silently wipe a working credential.
        if twenty_api_key.strip():
            values[store.SETTING_TWENTY_KEY] = twenty_api_key.strip()
        store.save_settings(values)
        return _redirect("/setup")

    @app.post("/setup/fix/{action}")
    def apply_fix(request: Request, action: str):
        """Run one named repair. Only the fixed set is reachable."""
        if action not in setup_wizard.FIXES:
            return _redirect("/setup")

        saved = store.get_settings()
        monorepo = Path(saved.get(store.SETTING_MONOREPO) or pipeline.default_monorepo())
        try:
            if action == "write_sandbox_config":
                # Built from the same function _setup_context() feeds its own
                # check with (setup_wizard.required_host_paths) — this call
                # used to keep its own hand-written copy of this list, which
                # silently fell out of sync and dropped agent_repos_root, so
                # the fix button rewrote the file still missing the one path
                # the check was actually complaining about. See that
                # function's own docstring.
                message = setup_wizard.write_sandbox_config(
                    setup_wizard.required_host_paths(
                        monorepo=monorepo,
                        worktree_root=workspace.default_worktree_root(),
                        skill_packs_root=Path(
                            saved.get(store.SETTING_SKILLS_ROOT)
                            or str(skill_packs.DEFAULT_SKILLS_ROOT)
                        ),
                        monorepo_mirror=sandbox_runner.DEFAULT_MONOREPO_MIRROR,
                        shopify_auth_dir=sandbox_runner.SHOPIFY_AUTH_DIR,
                        agent_repos_root=workspace.default_agent_repos_root(),
                    )
                )
            elif action == "write_egress_mode":
                message = setup_wizard.write_egress_mode()
            elif action == "start_sandbox_server":
                message = setup_wizard.start_sandbox_server()
            elif action == "clone_agent_repos_mirror":
                message = setup_wizard.clone_agent_repos_mirror(
                    workspace.default_agent_repos_root(),
                    workspace.KNOWN_REPOS,
                    saved.get(store.SETTING_GIT_REMOTE) or DEFAULT_GIT_REMOTE,
                )
            else:
                message = setup_wizard.clone_repos(
                    monorepo,
                    workspace.KNOWN_REPOS,
                    saved.get(store.SETTING_GIT_REMOTE) or DEFAULT_GIT_REMOTE,
                )
        except Exception as exc:  # noqa: BLE001 — report, never 500 the setup page
            logger.exception("bloy_dev_agent: setup fix %s failed", action)
            message = f"{action} lỗi: {type(exc).__name__}: {exc}"

        return TEMPLATES.TemplateResponse(
            request=request,
            name="bloy_setup.html",
            context=_setup_context(request, message=message),
        )

    @app.get("/api/setup")
    def setup_json():
        url, key = twenty_credentials()
        saved = store.get_settings()
        steps = setup_wizard.diagnose(
            monorepo=Path(saved.get(store.SETTING_MONOREPO) or pipeline.default_monorepo()),
            worktree_root=workspace.default_worktree_root(),
            repos=workspace.KNOWN_REPOS,
            twenty_url=url,
            twenty_key=key,
            skill_packs_root=Path(
                saved.get(store.SETTING_SKILLS_ROOT) or skill_packs.DEFAULT_SKILLS_ROOT
            ),
            monorepo_mirror=sandbox_runner.DEFAULT_MONOREPO_MIRROR,
            shopify_auth_dir=sandbox_runner.SHOPIFY_AUTH_DIR,
            agent_repos_root=workspace.default_agent_repos_root(),
        )
        return {
            "ready": all(s.state != setup_wizard.FAIL for s in steps),
            "steps": [asdict(s) for s in steps],
        }

    @app.get("/preflight")
    def preflight_page(request: Request):
        checks = preflight.run_checks()
        return TEMPLATES.TemplateResponse(
            request=request,
            name="bloy_preflight.html",
            context=_shell(
                request,
                title=f"Preflight · {PAGE_TITLE}",
                checks=checks,
                counts=preflight.summarise(checks),
            ),
        )

    @app.post("/attempts/{issue_id}/reset")
    def reset_attempts_route(issue_id: str):
        """Release an issue the cap has blocked — deliberately a human action."""
        store.reset_attempts(issue_id)
        return _redirect("/")

    @app.post("/run")
    def run_now(background: BackgroundTasks, max_issues: str = Form(default="1")):
        try:
            count = max(1, min(3, int(max_issues)))
        except ValueError:
            count = 1
        RUNNER.try_start({"max_issues": count})
        return _redirect("/")

    @app.post("/run-feedback")
    def run_feedback_now(max_issues: str = Form(default="1")):
        """Work the review queue: reviewer comments become revision rounds."""
        try:
            count = max(1, min(3, int(max_issues)))
        except ValueError:
            count = 1
        RUNNER.try_start({"mode": "feedback", "max_issues": count})
        return _redirect("/")

    # ---------------- API ----------------

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "bloy_dev_agent", "busy": RUNNER.busy}

    @app.get("/api/preflight")
    def preflight_json():
        checks = preflight.run_checks()
        return {
            "counts": preflight.summarise(checks),
            "checks": [asdict(c) for c in checks],
        }

    @app.get("/api/runs/active")
    def active_json():
        return {
            "max_attempts": store.max_attempts(),
            "busy": RUNNER.busy,
            "trigger": trigger_health(),
            "active": [_run_view(r) for r in store.active_runs()],
        }

    @app.get("/api/runs/{run_id}")
    def run_json(run_id: str, after: int = 0):
        run = store.get_run(run_id)
        if run is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        events = agent_log.parse(Path(run.log_path)) if run.log_path else []
        return {
            "run": _run_view(run),
            "live": run.state == BloyPipelineRun.STATE_RUNNING,
            "total": len(events),
            "events": [asdict(e) for e in events[max(0, after) :]],
        }

    @app.post("/api/pipeline/run")
    def trigger(payload: dict | None = None):
        """Start a pass. This is what BAM's routine action calls.

        Returns immediately: a pass takes minutes and the caller is a scheduler
        with its own timeout. ``accepted: false`` means one was already running,
        which is a normal answer, not an error.
        """
        config = payload or {}
        accepted = RUNNER.try_start(config)
        return {
            "accepted": accepted,
            "busy": RUNNER.busy,
            "detail": "" if accepted else "một pass đang chạy, bỏ qua lượt này",
            "active": [_run_view(r) for r in store.active_runs()],
        }

    @app.post("/api/pipeline/feedback")
    def trigger_feedback(payload: dict | None = None):
        """Start a feedback pass. The BAM routine action calls this too.

        Same single-flight rule and same immediate answer as
        ``/api/pipeline/run``: ``accepted: false`` means a pass of either kind
        was already running, which is a normal answer rather than an error.
        """
        config = dict(payload or {})
        config["mode"] = "feedback"
        accepted = RUNNER.try_start(config)
        return {
            "accepted": accepted,
            "busy": RUNNER.busy,
            "detail": "" if accepted else "một pass đang chạy, bỏ qua lượt này",
            "active": [_run_view(r) for r in store.active_runs()],
        }

    @app.get("/api/pipeline/status")
    def pipeline_status():
        return {
            "busy": RUNNER.busy,
            "started_at": RUNNER.started_at.isoformat() if RUNNER.started_at else "",
            "last_summary": RUNNER.last_summary,
            "active": [_run_view(r) for r in store.active_runs()],
        }

    return app


app = create_app()


def main() -> None:
    """Entry point for ``python -m bloy_dev_agent.service``."""
    import uvicorn

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
    )
    uvicorn.run(app, host=os.environ.get("BLOY_AGENT_HOST", "127.0.0.1"), port=port())


if __name__ == "__main__":
    main()
