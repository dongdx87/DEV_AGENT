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

import logging
import os
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from bloy_dev_agent import preflight, store
from bloy_dev_agent.db import database_url, init_db
from bloy_dev_agent.features import agent_log, pipeline, workspace
from bloy_dev_agent.models import BloyPipelineRun

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))

DEFAULT_PORT = 8100
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
    return os.environ.get("BAM_URL", "http://localhost:8000").rstrip("/")


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
        try:
            self._last_summary = run_pass_from_config(config)
        except Exception as exc:  # noqa: BLE001 — a crash must not kill the service
            logger.exception("bloy_dev_agent: pass failed")
            self._last_summary = {"error": str(exc)}


RUNNER = PassRunner()


def _reap_on_boot() -> None:
    """Clean up after a killed process: close its runs and free their issues.

    Without this, a restart leaves the issue sitting in the working column, where
    the source-column query can never see it again — which is exactly how BLOY-2
    disappeared from the board twice.
    """
    stale = store.reap_stale_runs()
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


def _twenty_client():
    from bloy_dev_agent.features.twenty.client import TwentyClient

    base = os.environ.get("BLOY_TWENTY_BASE_URL", "").strip()
    key = os.environ.get("BLOY_TWENTY_API_KEY", "").strip()
    if not base or not key:
        raise RuntimeError("BLOY_TWENTY_BASE_URL and BLOY_TWENTY_API_KEY are not set")
    return TwentyClient(base_url=base, api_key=key)


def run_pass_from_config(config: dict) -> dict:
    """Run one pass, filling anything the caller left out from settings."""
    saved = store.get_settings()

    def pick(key: str, fallback: str) -> str:
        return str(config.get(key) or saved.get(key) or fallback)

    project_id = pick(store.SETTING_PROJECT_ID, "")
    if not project_id:
        return {"error": "project_id chưa được cấu hình"}

    monorepo = Path(pick(store.SETTING_MONOREPO, str(pipeline.DEFAULT_MONOREPO)))
    if not monorepo.is_dir():
        return {"error": f"Repository path không tồn tại: {monorepo}"}

    try:
        timeout = int(pick(store.SETTING_TIMEOUT_MINUTES, "0") or 0)
    except ValueError:
        timeout = 0

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
        "routine_url": f"{bam_url()}/agent-routine",
    }


def _shell(request: Request, **context) -> dict:
    """Context every page needs for the sidebar."""
    return {"request": request, "port": port(), "bam_url": bam_url(), **context}


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
            return RedirectResponse(url="/", status_code=303)
        events = agent_log.parse(Path(run.log_path)) if run.log_path else []
        return TEMPLATES.TemplateResponse(
            request=request,
            name="bloy_run.html",
            context=_shell(
                request,
                title=f"{run.issue_key} · {PAGE_TITLE}",
                run=_run_view(run),
                events=[asdict(e) for e in events],
                live=run.state == BloyPipelineRun.STATE_RUNNING,
            ),
        )

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
            }
        )
        return RedirectResponse(url="/settings", status_code=303)

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
        return RedirectResponse(url="/", status_code=303)

    @app.post("/run")
    def run_now(background: BackgroundTasks, max_issues: str = Form(default="1")):
        try:
            count = max(1, min(3, int(max_issues)))
        except ValueError:
            count = 1
        RUNNER.try_start({"max_issues": count})
        return RedirectResponse(url="/", status_code=303)

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
