"""Tests for the setup page.

Moving the service to another machine failed because ``~/.sandbox.toml`` did
not exist, and the only remedy was a shell session. These tests pin the two
things that make a browser-driven setup trustworthy: it repairs what it claims
to repair, and it never pretends to do what needs root.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from bloy_dev_agent import setup_wizard


@pytest.fixture()
def config(monkeypatch, tmp_path) -> Path:
    path = tmp_path / ".sandbox.toml"
    monkeypatch.setattr(setup_wizard, "SANDBOX_CONFIG", path)
    return path


# ---------------------------------------------------------------------------
# Writing the config
# ---------------------------------------------------------------------------


def test_a_missing_config_is_created_with_a_generated_key(config):
    """An empty api_key makes the server exit when run non-interactively."""
    setup_wizard.write_sandbox_config(["/srv/repos"])

    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert len(data["server"]["api_key"]) >= 32
    assert data["storage"]["allowed_host_paths"] == ["/srv/repos"]


def test_the_config_is_written_owner_only(config):
    """It holds the only secret protecting the sandbox server."""
    setup_wizard.write_sandbox_config(["/srv/repos"])

    assert config.stat().st_mode & 0o077 == 0, "group/other can read the api key"


def test_an_existing_key_is_kept(config):
    """Rotating the key would orphan a running server mid-run."""
    setup_wizard.write_sandbox_config(["/a"])
    first = tomllib.loads(config.read_text())["server"]["api_key"]

    setup_wizard.write_sandbox_config(["/a", "/b"])

    assert tomllib.loads(config.read_text())["server"]["api_key"] == first


def test_repairing_keeps_hand_written_hardening(config):
    """A blunt overwrite would discard settings someone added deliberately."""
    setup_wizard.write_sandbox_config(["/a"])
    text = config.read_text(encoding="utf-8") + '\n[custom]\nkeep_me = "yes"\n'
    config.write_text(text, encoding="utf-8")

    setup_wizard.write_sandbox_config(["/a", "/b"])

    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["custom"]["keep_me"] == "yes"
    assert data["storage"]["allowed_host_paths"] == ["/a", "/b"]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def test_a_missing_config_is_reported_as_fixable(config):
    step = setup_wizard.check_sandbox_config(["/srv"])

    assert step.state == setup_wizard.FAIL
    assert step.fix == "write_sandbox_config"


def test_a_config_missing_a_mount_path_is_fixable(config):
    setup_wizard.write_sandbox_config(["/srv"])

    step = setup_wizard.check_sandbox_config(["/srv", "/new"])

    assert step.state == setup_wizard.FAIL
    assert "/new" in step.detail


def test_a_complete_config_passes(config):
    setup_wizard.write_sandbox_config(["/srv", "/new"])

    assert setup_wizard.check_sandbox_config(["/srv", "/new"]).state == setup_wizard.OK


def test_docker_is_never_offered_as_an_automatic_fix(monkeypatch):
    """Installing it needs root, and sudo here asks for a password."""
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: None)

    step = setup_wizard.check_docker()

    assert step.state == setup_wizard.FAIL
    assert step.fix == "", "the UI must not claim it can install Docker"
    assert "sudo" in step.command


def test_claude_login_is_never_offered_as_an_automatic_fix(monkeypatch, tmp_path):
    """Logging in needs a browser; a form cannot do it."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    step = setup_wizard.check_claude_login()

    assert step.state == setup_wizard.FAIL
    assert step.fix == ""
    assert "claude" in step.command


def test_every_offered_fix_is_in_the_allowlist(config, tmp_path):
    """The HTTP layer dispatches on this name; an unlisted one is unreachable."""
    steps = setup_wizard.diagnose(
        monorepo=tmp_path / "BLOY",
        worktree_root=tmp_path / "wt",
        repos=("shopify-app-loyalty-api",),
        twenty_url="",
        twenty_key="",
    )

    for step in steps:
        if step.fix and step.fix != "form":
            assert step.fix in setup_wizard.FIXES, f"{step.key} offers {step.fix!r}"


def test_diagnose_never_raises(tmp_path):
    """A setup page that 500s hides the very problem it exists to surface."""
    steps = setup_wizard.diagnose(
        monorepo=Path("/nope/does/not/exist"),
        worktree_root=tmp_path / "wt",
        repos=("shopify-app-loyalty-api",),
        twenty_url="http://127.0.0.1:1",
        twenty_key="x",
    )

    assert steps
    assert all(s.state in {setup_wizard.OK, setup_wizard.WARN, setup_wizard.FAIL} for s in steps)
