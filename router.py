"""HTTP routes for the BLOY Dev Agent plugin."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Request

from bloy_dev_agent import preflight
from core.template_env import get_templates

router = APIRouter(prefix="/bloy-dev-agent", tags=["bloy-dev-agent"])


@router.get("")
def preflight_page(request: Request):
    """Render the environment checks as the plugin's landing page."""
    checks = preflight.run_checks()
    templates = get_templates()
    return templates.TemplateResponse(
        "bloy_preflight.html",
        {
            "request": request,
            "title": "BLOY Dev Agent",
            "checks": checks,
            "counts": preflight.summarise(checks),
        },
    )


@router.get("/api/preflight")
def preflight_json():
    """Same checks as JSON, for scripts and smoke tests."""
    checks = preflight.run_checks()
    return {
        "counts": preflight.summarise(checks),
        "checks": [asdict(check) for check in checks],
    }
