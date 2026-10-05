"""CLI end-to-end over local: URLs (no network)."""

from __future__ import annotations

import json

import pytest

from nm_skills_registry.cli import main
from nm_skills_registry.storage.local import LocalDirStore


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    src = tmp_path / "src" / "skills"
    (src / "alpha").mkdir(parents=True)
    (src / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: first skill\nversion: 1.0.0\n---\n\nA.\n",
        encoding="utf-8",
    )
    (src / "alpha" / "__pycache__" / "junk.pyc").parent.mkdir()
    (src / "alpha" / "__pycache__" / "junk.pyc").write_bytes(b"\x00")
    (src / "alpha" / "references" / "n.md").parent.mkdir()
    (src / "alpha" / "references" / "n.md").write_text("note\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKILLS_REGISTRY", raising=False)
    return tmp_path, src


def test_import_pull_list_toggle_roundtrip(workspace, capsys, monkeypatch):
    tmp_path, src = workspace
    url = f"local:{tmp_path / 'bucket'}"

    assert main(["import", str(src), "--url", url]) == 0
    # __pycache__ contents excluded from import
    listing = [m.key for m in LocalDirStore(tmp_path / "bucket").list_prefix()]
    assert "skills/alpha/SKILL.md" in listing
    assert not any("__pycache__" in k for k in listing)

    # pull into a fresh mirror
    mirror = tmp_path / "mirror" / "skills"
    mirror.mkdir(parents=True)
    assert main(["pull", str(mirror), "--url", url]) == 0
    assert (mirror / "alpha" / "SKILL.md").is_file()

    # list + toggle against the registry
    monkeypatch.setenv("SKILLS_REGISTRY", url)
    try:
        assert main(["list"]) == 0
        out = capsys.readouterr().out
        assert "alpha" in out and "✓" in out
        assert main(["disable", "alpha"]) == 0
        assert "disabled" in capsys.readouterr().out
        assert main(["list"]) == 0
        assert "✗ alpha" in capsys.readouterr().out
        # state object landed in the bucket
        state = json.loads(
            LocalDirStore(tmp_path / "bucket").get_text("state/users/default/skills.json")
        )
        assert state["disabled"] == ["alpha"]
        assert main(["enable", "alpha"]) == 0
        assert main(["doctor"]) == 0
    finally:
        monkeypatch.delenv("SKILLS_REGISTRY")


def test_import_requires_url(workspace, monkeypatch):
    monkeypatch.delenv("SKILLS_REGISTRY", raising=False)
    with pytest.raises(SystemExit):
        main(["import", str(workspace[1])])


def test_push_uploads_same_size_edits(workspace, capsys):
    """Same-size content edits must be detected (etag compare, not size)."""
    tmp_path, src = workspace
    url = f"local:{tmp_path / 'bucket'}"
    assert main(["import", str(src), "--url", url]) == 0
    capsys.readouterr()

    skill_md = src / "alpha" / "SKILL.md"
    original = skill_md.read_text()
    skill_md.write_text(original.replace("A.", "B."))  # same size, different content

    assert main(["push", str(src), "--url", url]) == 0
    assert "1 uploaded" in capsys.readouterr().out
    bucket = LocalDirStore(tmp_path / "bucket")
    assert b"B." in bucket.get("skills/alpha/SKILL.md")
    assert b"A." not in bucket.get("skills/alpha/SKILL.md")

    # unchanged tree → next push is a no-op
    assert main(["push", str(src), "--url", url]) == 0
    assert "0 uploaded" in capsys.readouterr().out
