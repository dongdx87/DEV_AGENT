"""The staging-control FastAPI app: four routes, nothing else.

Binds to the Docker bridge gateway address, not loopback and not 0.0.0.0.
Every route requires the same bearer token; there is no unauthenticated
endpoint, including the read-only ones, because this whole service exists
specifically to be reachable from a place that reads untrusted ticket text.

A sandbox does NOT reach this service at ``172.17.0.1:8110`` directly. Live
testing in Phase 3 confirmed the egress sidecar's ``dns+nft`` mode blocks a
direct-by-IP connection to an un-allowlisted address exactly as intended — and
``NetworkPolicy``'s ``NetworkRule.target`` only ever accepts an FQDN, never a
bare IP, so this address could never have been put on
``sandbox_runner.STAGING_EGRESS_ALLOW`` in the first place. The service is
instead fronted by its own Cloudflare tunnel hostname
(``dev-dongdx2k3-bloy-staging-control.dev-bsscommerce.com``, added as one more
ingress rule on the same personal-dev tunnel already serving this host's other
domains — see the setup notes this module points to), which forwards straight
back to this same bind address. The bind address and bearer-token requirement
are unchanged; only the path a sandbox's HTTP client actually calls is a
public HTTPS hostname instead of a private IP:port.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Header, HTTPException, Query, Request

from bloy_dev_agent.db import init_db
from bloy_dev_agent.staging_control import actions, apps
from bloy_dev_agent.staging_control.tokens import StagingGrant, purge_expired, resolve

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8110
#: The Docker bridge gateway — reachable from a sandbox on the default bridge
#: network, unreachable from the real network. Never 0.0.0.0.
DEFAULT_HOST = "172.17.0.1"


def port() -> int:
    try:
        return int(os.environ.get("BLOY_STAGING_CONTROL_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def host() -> str:
    return os.environ.get("BLOY_STAGING_CONTROL_HOST", DEFAULT_HOST)


def _authorize(authorization: str) -> StagingGrant:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    grant = resolve(authorization.removeprefix("Bearer ").strip())
    if grant is None:
        raise HTTPException(status_code=401, detail="token không hợp lệ hoặc đã hết hạn")
    return grant


def _app_or_400(key: str) -> apps.StagingApp:
    staging_app = apps.STAGING_APPS.get(key)
    if staging_app is None:
        raise HTTPException(status_code=400, detail=f"app không hợp lệ: {key!r}")
    return staging_app


def create_app() -> FastAPI:
    app = FastAPI(title="BLOY Staging Control", docs_url=None, redoc_url=None)
    init_db()
    purge_expired()

    @app.post("/v1/deploy")
    async def deploy_route(request: Request, authorization: str = Header(default="")):
        grant = _authorize(authorization)
        payload = await request.json()
        staging_app = _app_or_400(str(payload.get("app") or ""))

        from bloy_dev_agent.staging_control.tokens import check_rate_limit, record_deploy

        reason = check_rate_limit(grant.run_id)
        if reason:
            raise HTTPException(status_code=429, detail=reason)

        result = actions.deploy(staging_app, grant)
        record_deploy(grant.run_id)
        if not result.ok:
            raise HTTPException(status_code=409, detail=result.detail)
        return {
            "ok": True,
            "app": staging_app.key,
            "seconds": round(result.seconds, 1),
            "detail": result.detail,
        }

    @app.post("/v1/restart")
    async def restart_route(request: Request, authorization: str = Header(default="")):
        _authorize(authorization)
        payload = await request.json()
        key = str(payload.get("app") or "")
        targets = (
            list(apps.STAGING_APPS.values()) if key == "all" else [_app_or_400(key)]
        )
        restarted = []
        for staging_app in targets:
            result = actions.restart(staging_app)
            restarted.append(
                {"app": staging_app.key, "ok": result.ok, "detail": result.detail}
            )
        return {"restarted": restarted}

    @app.get("/v1/status")
    def status_route(authorization: str = Header(default="")):
        _authorize(authorization)
        return {"apps": actions.status(list(apps.STAGING_APPS.values()))}

    @app.get("/v1/logs")
    def logs_route(
        app_key: str = Query(alias="app"),
        lines: int = 200,
        authorization: str = Header(default=""),
    ):
        _authorize(authorization)
        staging_app = _app_or_400(app_key)
        return {"lines": actions.logs(staging_app, lines)}

    return app


app = create_app()


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
    )
    uvicorn.run(app, host=host(), port=port())


if __name__ == "__main__":
    main()
