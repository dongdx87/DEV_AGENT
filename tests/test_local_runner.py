"""Exercise the sandbox's shell assembly without a container.

Every pipeline test mocks ``run_in_sandbox``, so the scripts that actually run
inside the container were never executed by the suite. Three production
failures lived exactly there. These tests run the same strings under bash.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from bloy_dev_agent.features import agent_log, local_runner, sandbox_runner

#: Lines that only make sense as root inside a fresh image. Everything else —
#: where the credential and the prompt land — is what production got wrong.
_CONTAINER_ONLY = (
    "u=", "if [", "fi", "h=", "[ -n",
    "getent", "groupadd", "useradd", "chown",
)


def _host_setup(prompt: str, home: Path) -> str:
    """The real setup script with the root-only user juggling replaced.

    ``$h`` is pinned to a temp HOME instead of being resolved from the passwd
    database, so the remaining lines run unprivileged and unchanged.
    """
    script = sandbox_runner._setup_script(prompt)
    kept = [
        line
        for line in script.splitlines()
        if not line.strip().startswith(_CONTAINER_ONLY)
    ]
    return "\n".join([f"h={shlex.quote(str(home))}", *kept])


def test_the_prompt_arrives_byte_for_byte(tmp_path):
    """It is base64'd precisely so quotes and newlines survive the shell."""
    prompt = 'Ticket "BLS-1" — `backtick`, $(whoami)\nDòng hai\n'

    result = local_runner.run_locally(
        _host_setup(prompt, tmp_path / "home"),
        f"cat {sandbox_runner.PROMPT_PATH}",
        workdir=tmp_path / "wt",
        home=tmp_path / "home",
    )

    assert result.ok, result.setup_output
    assert result.run_output.rstrip("\n") == prompt.rstrip("\n")


def test_a_hostile_ticket_body_cannot_run_commands(tmp_path):
    """The ticket tracker must not be a shell-injection surface."""
    marker = tmp_path / "pwned.txt"
    prompt = f'x"; touch {marker} #'

    local_runner.run_locally(
        _host_setup(prompt, tmp_path / "home"),
        f"cat {sandbox_runner.PROMPT_PATH}",
        workdir=tmp_path / "wt",
        home=tmp_path / "home",
    )

    assert not marker.exists(), "the ticket body executed as a command"


def test_the_claude_login_lands_beside_the_config_dir(tmp_path):
    """``~/.claude.json`` sits next to ``~/.claude``; missing it, the CLI exits."""
    home = tmp_path / "home"

    result = local_runner.run_locally(
        _host_setup("hello", home),
        'test -s "$HOME/.claude.json" && echo FOUND || echo MISSING',
        workdir=tmp_path / "wt",
        home=home,
    )

    assert result.ok
    assert "FOUND" in result.run_output
    assert "NO_CREDENTIALS" not in result.setup_output


def test_the_stream_redirect_produces_a_parsable_log(tmp_path):
    """The UI tails this file; if the redirect is wrong there is nothing to read."""
    home = tmp_path / "home"
    logs = tmp_path / "wt" / ".bloy-logs"
    logs.mkdir(parents=True)
    log = logs / "run.jsonl"

    inner = (
        f"cat {sandbox_runner.PROMPT_PATH} | claude -p "
        f"{sandbox_runner.claude_flags(True)} "
        f"--output-format stream-json --verbose > {shlex.quote(str(log))} 2>/dev/null"
    )
    result = local_runner.run_locally(
        _host_setup("sửa lỗi tính điểm", home),
        inner,
        workdir=tmp_path / "wt",
        home=home,
    )

    assert result.ok, result.run_output
    events = agent_log.parse(log)
    assert [e.kind for e in events] == ["thinking", "result"]
    assert "sửa lỗi tính điểm" in agent_log.final_text(log)


def test_stdout_stays_clean_json(tmp_path):
    """Merging stderr into stdout would break the parser; they must stay split."""
    home = tmp_path / "home"
    logs = tmp_path / "wt" / ".bloy-logs"
    logs.mkdir(parents=True)
    log = logs / "run.jsonl"

    stub = local_runner.STUB_CLAUDE + '\necho "cảnh báo lạc vào stderr" >&2\n'
    inner = (
        f"cat {sandbox_runner.PROMPT_PATH} | claude -p --output-format stream-json "
        f"> {shlex.quote(str(log))} 2> {shlex.quote(str(tmp_path / 'err.txt'))}"
    )
    local_runner.run_locally(
        _host_setup("x", home), inner,
        workdir=tmp_path / "wt", home=home, claude_stub=stub,
    )

    for line in log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            json.loads(line)  # raises if stderr leaked into the stream
    assert "cảnh báo" in (tmp_path / "err.txt").read_text(encoding="utf-8")
