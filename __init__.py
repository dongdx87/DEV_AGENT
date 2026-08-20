"""BLOY Dev Agent.

Pulls BLOY development tasks from Twenty, runs each one through a coding
agent inside an isolated sandbox, and opens a merge request — as a
standalone service (:mod:`bloy_dev_agent.service`) with its own process,
port and database.

``plugin.py`` is the thin BAM-facing shim: it contributes a sidebar link to
the service and a routine action that triggers a pass over HTTP. Nothing
else in this package may import BAM internals (``core.*``) — see
``tests/test_bloy_dev_agent.py::test_the_service_imports_nothing_from_bam``.

Layout:
    service.py       FastAPI app: dashboard, run detail, settings, API
    features/twenty/  Twenty REST/GraphQL client, mapping
    features/         pipeline, sandbox runner, workspace, agent log
    plugin.py         BAM plugin shim (menu item + routine action)
    trigger_action.py BAM routine action that calls the service over HTTP
"""
