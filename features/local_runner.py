"""Run the sandbox's shell assembly on the host, for tests.

The container path is covered only by mocks: every pipeline test replaces
``run_in_sandbox`` wholesale, so the part that actually breaks in production —
the setup script, the redirect, the way the prompt is handed over — is never
executed by the suite. Three live failures came from exactly that gap:

* ``useradd: UID 1000 is not unique`` — the base image already had the account.
* ``~/.claude.json not found`` — the file sits beside ``~/.claude``, not in it.
* the prompt arrived indented like a code block.

Every one is a shell-assembly bug that a unit test could have caught, and none
of them needed Docker to reproduce. This module runs the same scripts under
``bash`` in a temp directory, with a stub ``claude`` on PATH, so the assembly
can be asserted without a container and without calling a model.

Borrowed in spirit from agent_team's ``LocalSandbox`` — a host runtime kept
beside the real one purely so tests exercise the real shape. It is NOT an
isolation boundary and must never run a real ticket: there is no containment
here at all.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: What the stub `claude` writes when asked for a stream. Enough for the log
#: parser to find a result, so the whole path can be asserted end to end.
STUB_CLAUDE = """#!/usr/bin/env bash
prompt="$(cat)"
printf '%s\\n' '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"stub"}]}}'
json_escape() { python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'; }
echoed="$(printf '%s' "$prompt" | head -c 200 | json_escape)"
printf '{"type":"result","subtype":"success","is_error":false,"result":%s}\\n' "$echoed"
"""


@dataclass
class LocalResult:
    ok: bool
    setup_output: str
    run_output: str
    home: Path


def run_locally(
    setup_script: str,
    inner_command: str,
    *,
    workdir: Path,
    home: Path,
    claude_stub: str = STUB_CLAUDE,
) -> LocalResult:
    """Execute the two scripts the sandbox would run, on this host.

    ``setup_script`` and ``inner_command`` are the very strings sent to the
    container, with the container-only parts (``su``, absolute mount paths)
    left to the caller to substitute.
    """
    home.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)

    stub_dir = home / "stub-bin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "claude"
    stub.write_text(claude_stub, encoding="utf-8")
    stub.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
    }

    setup = subprocess.run(
        ["bash", "-c", setup_script],
        cwd=str(workdir), env=env, capture_output=True, text=True, timeout=60,
    )
    if setup.returncode != 0:
        return LocalResult(False, setup.stderr or setup.stdout, "", home)

    run = subprocess.run(
        ["bash", "-c", inner_command],
        cwd=str(workdir), env=env, capture_output=True, text=True, timeout=120,
    )
    return LocalResult(
        run.returncode == 0,
        setup.stdout,
        (run.stdout or "") + (run.stderr or ""),
        home,
    )
