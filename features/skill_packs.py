"""Read skill packs from the store BAM's own ``skill_packs`` plugin also reads.

BAM already has a UI for importing and versioning skill packs
(``agent-manager/src/plugins/skill_packs``), backed by a directory tree kept
*outside* the BAM repo so its dev autoreloader never restarts on a skill edit.
That tree is the one place skills should be authored — this service must not
grow a second one.

The connection is a **directory layout**, not a Python import. This service
runs as its own process on its own port, precisely so a BAM restart cannot
kill a run in flight; importing ``plugins.skill_packs`` would tie the two
back together and break on a machine where BAM is not even installed. Reading
the same folder gets the shared catalog without the coupling.

Layout matched (mirrors ``plugins/skill_packs/store.py``)::

    <root>/shared/...                — hand-authored, or "Import skill from
                                        file" on BAM's own /skill-packs page
    <root>/sources/<slug>/...        — synced from a git source, or an
                                        uploaded ZIP, via that same page

Discovery mirrors ``LocalDirSkillPackStore._walk_for_packs`` exactly (one
independent breadth-first scan per root — ``shared/`` itself, and each
``sources/<slug>/`` directory in turn — capped at the same depth, stopping at
the first ``SKILL.md`` per branch): BAM's own import accepts a ``SKILL.md``
at any depth up to that cap, not only directly one level down, so scanning
only fixed depths here would silently miss anything imported that way.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

#: Sibling of the agent-manager checkout, same convention as
#: ``plugins/skill_packs/store.py:default_packs_root`` — kept outside the repo
#: so nothing here forces that layout to exist, and this default just happens
#: to match it on this machine.
DEFAULT_SKILLS_ROOT = Path("/home/bss-group/BLOY/agent-manager-skill-packs")

_FENCE = "---"

#: A pack's name ends up in a shell command that copies it into the sandbox
#: (see :mod:`bloy_dev_agent.features.sandbox_runner`). The name comes from a
#: directory someone dropped into a shared store, not from a value this
#: service controls, so it is checked before it ever reaches a shell.
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class SkillPack:
    """One pack found on disk: enough to list it and to copy it into a sandbox."""

    name: str
    description: str
    path: Path  # absolute directory containing SKILL.md


def _parse_frontmatter(text: str) -> dict:
    """Best-effort YAML frontmatter read. A malformed file is skipped, not fatal —

    one broken SKILL.md must not take the whole Skills page down.
    """
    stripped = text.lstrip("﻿").lstrip()
    if not stripped.startswith(_FENCE):
        return {}
    after = stripped[len(_FENCE) :].lstrip("\n")
    end = after.find(f"\n{_FENCE}")
    if end == -1:
        return {}
    try:
        meta = yaml.safe_load(after[:end]) or {}
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _load_one(skill_md: Path) -> SkillPack | None:
    try:
        meta = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    except OSError:
        return None
    name = str(meta.get("name") or skill_md.parent.name).strip()
    if not name or not SAFE_NAME.match(name):
        logger.warning(
            "bloy_dev_agent: skill pack at %s has an unsafe name %r, skipped",
            skill_md, name,
        )
        return None
    return SkillPack(
        name=name,
        description=str(meta.get("description") or "").strip(),
        path=skill_md.parent,
    )


#: BAM's own default; kept in step so a pack it can see is one this sees too.
MAX_SCAN_DEPTH = 4


def _find_skill_md(root: Path, max_depth: int = MAX_SCAN_DEPTH) -> list[Path]:
    """Breadth-first scan for ``SKILL.md`` under one root.

    Stops descending a branch the moment it finds one — packs do not nest —
    and skips dot-directories, same as BAM's own walker.
    """
    found: list[Path] = []
    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    while queue:
        folder, depth = queue.popleft()
        if not folder.is_dir():
            continue
        skill_md = folder / "SKILL.md"
        if skill_md.is_file():
            found.append(skill_md)
            continue
        if depth >= max_depth:
            continue
        try:
            children = sorted(folder.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and not child.name.startswith("."):
                queue.append((child, depth + 1))
    return found


def _roots(packs_root: Path) -> list[Path]:
    """One root per store: ``shared/`` itself, then each ``sources/<slug>/``.

    Every source's checkout is its own independent scan root, matching
    ``resolve_stores`` — a pack cannot nest inside another source's tree, and
    scanning them separately keeps a first-wins clash resolvable by source
    rather than by whichever the top-level glob order happened to favour.
    """
    roots = [packs_root / "shared"]
    sources_dir = packs_root / "sources"
    if sources_dir.is_dir():
        try:
            roots += sorted(
                p for p in sources_dir.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
        except OSError:
            pass
    return roots


def list_packs(root: Path = DEFAULT_SKILLS_ROOT) -> list[SkillPack]:
    """Every pack found under ``root``, alphabetical, first-wins on a duplicate name."""
    if not root.is_dir():
        return []
    found: dict[str, SkillPack] = {}
    for store_root in _roots(root):
        for skill_md in _find_skill_md(store_root):
            pack = _load_one(skill_md)
            if pack is not None and pack.name not in found:
                found[pack.name] = pack
    return sorted(found.values(), key=lambda p: p.name)


def find(name: str, root: Path = DEFAULT_SKILLS_ROOT) -> SkillPack | None:
    return next((p for p in list_packs(root) if p.name == name), None)


def parse_enabled(raw: str) -> list[str]:
    """Comma-separated setting value -> ordered, de-duplicated pack names."""
    seen: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in seen:
            seen.append(name)
    return seen
