"""Tests for reading the shared skill-pack store.

This module is a directory-layout reader, not an importer of BAM's
``plugins.skill_packs`` — the service runs as its own process and must not
depend on BAM being importable. These tests pin the layout contract (``shared/
<pack>/SKILL.md`` and ``sources/<slug>/<pack>/SKILL.md``) and the places a
malformed or hostile pack must not be allowed to break the caller.
"""

from __future__ import annotations

from bloy_dev_agent.features import skill_packs


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_a_missing_root_is_simply_an_empty_catalog(tmp_path):
    assert skill_packs.list_packs(tmp_path / "nope") == []


def test_finds_a_pack_under_shared(tmp_path):
    _write(
        tmp_path / "shared" / "demo" / "SKILL.md",
        "---\nname: demo\ndescription: a demo pack\n---\nbody",
    )

    packs = skill_packs.list_packs(tmp_path)

    assert [p.name for p in packs] == ["demo"]
    assert packs[0].description == "a demo pack"
    assert packs[0].path == tmp_path / "shared" / "demo"


def test_finds_a_pack_under_a_git_source(tmp_path):
    """``sources/<slug>/<pack>/SKILL.md`` — one directory deeper than ``shared``."""
    _write(
        tmp_path / "sources" / "some-repo" / "demo" / "SKILL.md",
        "---\nname: demo\ndescription: from a source\n---\nbody",
    )

    packs = skill_packs.list_packs(tmp_path)

    assert [p.name for p in packs] == ["demo"]
    assert packs[0].description == "from a source"


def test_a_skill_md_directly_at_a_source_root_is_found(tmp_path):
    """A single-pack ZIP uploaded through BAM's own /skill-packs page can land
    the SKILL.md straight at the source root — not necessarily one level
    deeper — and this reader has to see it either way.
    """
    _write(tmp_path / "sources" / "one-pack-zip" / "SKILL.md", "---\nname: demo\n---\n")

    packs = skill_packs.list_packs(tmp_path)

    assert [p.name for p in packs] == ["demo"]


def test_a_deeply_nested_pack_within_the_depth_cap_is_still_found(tmp_path):
    _write(
        tmp_path / "shared" / "a" / "b" / "c" / "deep" / "SKILL.md",
        "---\nname: deep\n---\n",
    )

    packs = skill_packs.list_packs(tmp_path)

    assert [p.name for p in packs] == ["deep"]


def test_a_dot_directory_is_skipped_during_the_scan(tmp_path):
    _write(tmp_path / "shared" / ".git" / "SKILL.md", "---\nname: hidden\n---\n")

    assert skill_packs.list_packs(tmp_path) == []


def test_a_dot_named_source_directory_is_skipped(tmp_path):
    _write(tmp_path / "sources" / ".stale-tmp" / "SKILL.md", "---\nname: hidden\n---\n")

    assert skill_packs.list_packs(tmp_path) == []


def test_a_pack_past_the_depth_cap_is_not_found():
    """Mirrors BAM's own cap — a pack this deep would be invisible there too,
    so staying consistent (rather than scanning further) is the correct match.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        too_deep = root / "shared"
        for _ in range(skill_packs.MAX_SCAN_DEPTH + 1):
            too_deep = too_deep / "d"
        _write(too_deep / "SKILL.md", "---\nname: buried\n---\n")

        assert skill_packs.list_packs(root) == []


def test_a_directory_without_skill_md_is_ignored(tmp_path):
    (tmp_path / "shared" / "not-a-pack").mkdir(parents=True)
    (tmp_path / "shared" / "not-a-pack" / "notes.md").write_text("x", encoding="utf-8")

    assert skill_packs.list_packs(tmp_path) == []


def test_a_pack_missing_a_name_falls_back_to_its_directory(tmp_path):
    _write(
        tmp_path / "shared" / "fallback-name" / "SKILL.md",
        "---\ndescription: no name field\n---\nbody",
    )

    packs = skill_packs.list_packs(tmp_path)

    assert packs[0].name == "fallback-name"


def test_malformed_frontmatter_is_skipped_not_raised(tmp_path):
    """A broken SKILL.md must not take the whole Skills page down."""
    _write(tmp_path / "shared" / "broken" / "SKILL.md", "---\nname: [unterminated\nbody")

    packs = skill_packs.list_packs(tmp_path)

    assert packs[0].name == "broken"  # falls back to the directory name
    assert packs[0].description == ""


def test_a_pack_with_no_frontmatter_at_all_still_lists(tmp_path):
    _write(tmp_path / "shared" / "plain" / "SKILL.md", "just a heading, no frontmatter")

    packs = skill_packs.list_packs(tmp_path)

    assert packs[0].name == "plain"


def test_results_are_alphabetical_and_deduplicated_by_name(tmp_path):
    _write(tmp_path / "shared" / "zeta" / "SKILL.md", "---\nname: zeta\n---\n")
    _write(tmp_path / "shared" / "alpha" / "SKILL.md", "---\nname: alpha\n---\n")
    # A same-named pack under sources/ must not create a second entry —
    # first-wins keeps the catalog one name -> one pack.
    _write(
        tmp_path / "sources" / "mirror" / "alpha" / "SKILL.md",
        "---\nname: alpha\ndescription: duplicate\n---\n",
    )

    packs = skill_packs.list_packs(tmp_path)

    assert [p.name for p in packs] == ["alpha", "zeta"]
    assert packs[0].description == ""  # the shared/ one won, not the duplicate


def test_a_name_with_shell_metacharacters_is_dropped():
    """The name later reaches a shell command in sandbox_runner; unsafe names
    must not survive to that point.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "shared" / "evil" / "SKILL.md", '---\nname: "evil; rm -rf /"\n---\n')

        assert skill_packs.list_packs(root) == []


def test_find_returns_none_for_an_unknown_name(tmp_path):
    assert skill_packs.find("nope", tmp_path) is None


def test_find_returns_the_matching_pack(tmp_path):
    _write(tmp_path / "shared" / "demo" / "SKILL.md", "---\nname: demo\n---\n")

    found = skill_packs.find("demo", tmp_path)

    assert found is not None
    assert found.name == "demo"


# ---------------------------------------------------------------------------
# Enabled-list parsing
# ---------------------------------------------------------------------------


def test_parse_enabled_splits_and_strips():
    assert skill_packs.parse_enabled("a, b , c") == ["a", "b", "c"]


def test_parse_enabled_drops_empties_and_dedupes_preserving_order():
    assert skill_packs.parse_enabled("b,,a,b,") == ["b", "a"]


def test_parse_enabled_of_blank_is_empty():
    assert skill_packs.parse_enabled("") == []
