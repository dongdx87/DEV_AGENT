"""Reuse the Claude logins BAM's AI Code Factory already provisioned.

BAM has a whole surface for this: its ``ai_code`` plugin installs the Claude
CLI, runs the headless ``claude auth login`` round-trip, and stores each
resulting account as a **config directory** — the folder ``CLAUDE_CONFIG_DIR``
points at, holding ``.credentials.json``. Maintaining a second account registry
here would mean a second place to log in, a second place for a subscription to
expire, and two answers to "which account ran this ticket".

The connection is the **on-disk contract**, not a Python import, exactly like
:mod:`bloy_dev_agent.features.skill_packs` does for the skill store:

    AI_CODE_CLAUDE_CONFIG_BASES   colon-separated roots a config dir may sit
                                  under (BAM's own env var, same default: $HOME)
    <base>/<account>/             one provisioned account
    <base>/<account>/.credentials.json   its login

Why not read BAM's database, which is where the pool's ``enabled`` flag and
``weight`` actually live? Because this service must boot and keep running with
BAM absent or restarting — that is the entire reason it is a separate process
with its own database (see the package docstring, and the guard test
``test_the_service_imports_nothing_from_bam``). Reading BAM's tables would put
that guarantee behind BAM's schema and uptime. So the pool's *selection policy*
stays in BAM and is not reproduced here; what this module reuses is the part
that is a stable filesystem fact.

Consequence worth stating plainly: an account an operator disabled in BAM's UI
is still a directory on disk, and this module will use it. If that matters,
point ``BLOY_CLAUDE_CONFIG_DIR`` at exactly the one account this pipeline
should use.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: BAM's own env var (``plugins.ai_code.provisioning.claude.bases_env``), read
#: from the ``.env`` this service already loads from the agent-manager
#: checkout, so both processes agree on where accounts live without either one
#: importing the other.
BASES_ENV = "AI_CODE_CLAUDE_CONFIG_BASES"

#: Pin this pipeline to one account, bypassing discovery entirely. The escape
#: hatch for "BAM says that account is disabled" — see the module docstring.
OVERRIDE_ENV = "BLOY_CLAUDE_CONFIG_DIR"

#: The file BAM's provisioner writes into a config dir
#: (``ClaudeProvisioner.credential_file``). Its presence is what makes a
#: directory an *account* rather than just a directory.
CREDENTIAL_FILE = ".credentials.json"

#: Where the CLI keeps its login with no ``CLAUDE_CONFIG_DIR`` set. Always the
#: last resort, so a host that never used BAM's factory behaves exactly as this
#: pipeline did before this module existed.
DEFAULT_CONFIG_DIR = Path.home() / ".claude"


@dataclass(frozen=True)
class ClaudeAccount:
    """One provisioned login this service may use."""

    name: str
    config_dir: Path

    @property
    def credentials(self) -> Path:
        return self.config_dir / CREDENTIAL_FILE


def _bases() -> list[Path]:
    """Roots to scan, from BAM's env var; ``$HOME`` when it is unset.

    Resolved before use so a ``..`` or a symlink in the setting cannot point
    the scan somewhere the operator did not mean — the same containment rule
    BAM's own provisioner applies to this value.
    """
    raw = (os.environ.get(BASES_ENV) or "").strip()
    candidates = raw.split(":") if raw else [str(Path.home())]
    bases: list[Path] = []
    for candidate in candidates:
        text = candidate.strip()
        if not text:
            continue
        try:
            resolved = Path(text).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.is_dir() and resolved not in bases:
            bases.append(resolved)
    return bases


def discover() -> list[ClaudeAccount]:
    """Every account directory found under the configured bases, name-ordered.

    A base is *itself* checked first: BAM leaves ``config_dir`` blank to mean
    "the CLI default", which on this host is ``$HOME/.claude`` — a child of the
    base, found by the ordinary scan — but an operator who set the base
    directly to a config dir would otherwise get nothing.

    Only one level down is scanned. Deeper nesting is not part of BAM's layout,
    and recursing would eventually walk a whole home directory.
    """
    found: dict[str, ClaudeAccount] = {}
    for base in _bases():
        if (base / CREDENTIAL_FILE).is_file():
            found.setdefault(base.name, ClaudeAccount(base.name, base))
            continue
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or child.name.startswith(".."):
                continue
            if (child / CREDENTIAL_FILE).is_file():
                found.setdefault(child.name, ClaudeAccount(child.name, child))
    return [found[name] for name in sorted(found)]


def resolve(run_id: str = "") -> ClaudeAccount:
    """The account this run should use.

    Selection is a hash of ``run_id`` across the discovered accounts, not a
    counter: this service runs up to three tickets at once *and* restarts
    without warning, so a counter would either need persisting or would reset
    to the same account every boot. Hashing spreads concurrent runs across the
    provisioned logins — which is the point, since an unattended loop now
    spends several turns per ticket and a single subscription's rate limit is a
    real ceiling — while keeping one run's account stable across its own turns
    and reproducible when someone asks which login ran a ticket.

    Falls back to :data:`DEFAULT_CONFIG_DIR` when nothing is discovered, so a
    host with no factory-provisioned account behaves as before.
    """
    override = (os.environ.get(OVERRIDE_ENV) or "").strip()
    if override:
        path = Path(override).expanduser()
        return ClaudeAccount(path.name or "override", path)

    accounts = discover()
    if not accounts:
        return ClaudeAccount(DEFAULT_CONFIG_DIR.name, DEFAULT_CONFIG_DIR)
    if len(accounts) == 1 or not run_id:
        return accounts[0]
    digest = hashlib.sha256(run_id.encode("utf-8")).digest()
    return accounts[digest[0] % len(accounts)]


def credentials_path(run_id: str = "") -> Path:
    """The ``.credentials.json`` a run should read its Claude login out of."""
    return resolve(run_id).credentials
