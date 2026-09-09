"""Tests for reusing the Claude logins BAM's AI Code Factory provisioned.

The contract under test is a filesystem one, on purpose: this service has to
boot with BAM absent, so it reads the same directories BAM's provisioner writes
rather than BAM's database. See the module's own docstring.
"""

from __future__ import annotations

import pytest

from bloy_dev_agent.features import claude_accounts


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(claude_accounts.BASES_ENV, raising=False)
    monkeypatch.delenv(claude_accounts.OVERRIDE_ENV, raising=False)


def _account(base, name):
    directory = base / name
    directory.mkdir(parents=True)
    (directory / claude_accounts.CREDENTIAL_FILE).write_text("{}", encoding="utf-8")
    return directory


def test_accounts_under_the_configured_base_are_discovered(tmp_path, monkeypatch):
    base = tmp_path / "homes"
    _account(base, "claude-acc-1")
    _account(base, "claude-acc-2")
    monkeypatch.setenv(claude_accounts.BASES_ENV, str(base))

    found = claude_accounts.discover()

    assert [a.name for a in found] == ["claude-acc-1", "claude-acc-2"]


def test_a_directory_without_a_credentials_file_is_not_an_account(tmp_path, monkeypatch):
    base = tmp_path / "homes"
    (base / "not-an-account").mkdir(parents=True)
    _account(base, "real")
    monkeypatch.setenv(claude_accounts.BASES_ENV, str(base))

    assert [a.name for a in claude_accounts.discover()] == ["real"]


def test_a_base_that_is_itself_a_config_dir_is_found(tmp_path, monkeypatch):
    """An operator may point the base straight at one account."""
    base = tmp_path / "just-one"
    base.mkdir()
    (base / claude_accounts.CREDENTIAL_FILE).write_text("{}", encoding="utf-8")
    monkeypatch.setenv(claude_accounts.BASES_ENV, str(base))

    assert [a.config_dir for a in claude_accounts.discover()] == [base]


def test_several_bases_are_scanned(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    _account(first, "one")
    _account(second, "two")
    monkeypatch.setenv(claude_accounts.BASES_ENV, f"{first}:{second}")

    assert {a.name for a in claude_accounts.discover()} == {"one", "two"}


def test_a_nonexistent_base_is_skipped_rather_than_raising(tmp_path, monkeypatch):
    real = tmp_path / "real"
    _account(real, "one")
    monkeypatch.setenv(
        claude_accounts.BASES_ENV, f"{tmp_path / 'absent'}:{real}"
    )

    assert [a.name for a in claude_accounts.discover()] == ["one"]


def test_nothing_discovered_falls_back_to_this_hosts_default(tmp_path, monkeypatch):
    """A host that never used BAM's factory must behave exactly as before."""
    monkeypatch.setenv(claude_accounts.BASES_ENV, str(tmp_path / "empty"))

    account = claude_accounts.resolve("run-1")

    assert account.config_dir == claude_accounts.DEFAULT_CONFIG_DIR


def test_an_explicit_override_wins_over_discovery(tmp_path, monkeypatch):
    base = tmp_path / "homes"
    _account(base, "discovered")
    pinned = _account(tmp_path / "pinned-root", "pinned")
    monkeypatch.setenv(claude_accounts.BASES_ENV, str(base))
    monkeypatch.setenv(claude_accounts.OVERRIDE_ENV, str(pinned))

    assert claude_accounts.resolve("run-1").config_dir == pinned


def test_one_run_always_gets_the_same_account(tmp_path, monkeypatch):
    """Every turn of a run must share a login, or its rate budget is meaningless."""
    base = tmp_path / "homes"
    for name in ("a", "b", "c", "d"):
        _account(base, name)
    monkeypatch.setenv(claude_accounts.BASES_ENV, str(base))

    first = claude_accounts.resolve("run-abc")
    second = claude_accounts.resolve("run-abc")

    assert first.name == second.name


def test_different_runs_spread_across_the_provisioned_logins(tmp_path, monkeypatch):
    base = tmp_path / "homes"
    for name in ("a", "b", "c", "d"):
        _account(base, name)
    monkeypatch.setenv(claude_accounts.BASES_ENV, str(base))

    picked = {claude_accounts.resolve(f"run-{index}").name for index in range(40)}

    assert len(picked) > 1, "a single account would be the rate-limit ceiling"


def test_the_credentials_path_points_at_the_selected_account(tmp_path, monkeypatch):
    base = tmp_path / "homes"
    only = _account(base, "solo")
    monkeypatch.setenv(claude_accounts.BASES_ENV, str(base))

    assert claude_accounts.credentials_path("run-1") == (
        only / claude_accounts.CREDENTIAL_FILE
    )


def test_the_env_var_name_matches_the_one_bam_provisions_with():
    """Shared convention is the whole integration; a rename here breaks it."""
    assert claude_accounts.BASES_ENV == "AI_CODE_CLAUDE_CONFIG_BASES"
    assert claude_accounts.CREDENTIAL_FILE == ".credentials.json"
