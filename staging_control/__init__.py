"""Host-side control surface for the shared staging deployment.

Runs as its own PM2 process, bound to the Docker bridge gateway
(``172.17.0.1``) rather than loopback, because it must be reachable from
*inside* an opted-in sandbox — the one place a ticket's own untrusted text can
eventually reach. Everything in this package is written with that threat model
in mind: a fixed, tiny action surface (deploy one of two named apps, restart,
read status/logs), never a caller-supplied path, branch, or shell command.

See :mod:`bloy_dev_agent.features.sandbox_runner`'s module docstring for the
sandbox side of this boundary, and ``service.py`` for the actual FastAPI app.
"""
