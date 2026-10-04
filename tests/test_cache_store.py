"""CachedStore: write-through mirror semantics over any authority."""

from __future__ import annotations

import pytest

from nm_skills_registry.storage.cache import CachedStore
from nm_skills_registry.storage.local import LocalDirStore


@pytest.fixture()
def setup(tmp_path):
    authority_root = tmp_path / "authority"
    mirror_root = tmp_path / "mirror"
    authority_root.mkdir()
    authority = LocalDirStore(authority_root)
    cached = CachedStore(authority, mirror_root)
    return authority, cached, authority_root, mirror_root


def test_sync_pulls_and_mirrors(setup):
    authority, cached, aroot, mroot = setup
    authority.put("skills/a/SKILL.md", b"a-content")
    authority.put("skills/b/SKILL.md", b"b-content")
    report = cached.sync("skills/")
    assert sorted(report.pulled) == ["skills/a/SKILL.md", "skills/b/SKILL.md"]
    assert (mroot / "skills" / "a" / "SKILL.md").read_bytes() == b"a-content"
    assert report.unchanged == 0

    # second sync: everything unchanged
    report2 = cached.sync("skills/")
    assert report2.pulled == [] and report2.unchanged == 2


def test_sync_detects_changes_and_removals(setup):
    authority, cached, aroot, mroot = setup
    authority.put("skills/a/SKILL.md", b"v1")
    cached.sync("skills/")
    authority.put("skills/a/SKILL.md", b"v2")
    authority.put("skills/a/references/x.md", b"ref")
    authority.delete("skills/b/SKILL.md")  # was never pulled; ignore
    authority.put("skills/doomed/SKILL.md", b"die")
    report = cached.sync("skills/")
    assert "skills/a/SKILL.md" in report.pulled
    assert "skills/a/references/x.md" in report.pulled
    assert (mroot / "skills" / "doomed" / "SKILL.md").is_file()  # pulled first
    # now the authority deletes it → next sync removes it from the mirror
    authority.delete_prefix("skills/doomed/")
    report2 = cached.sync("skills/")
    assert "skills/doomed/SKILL.md" in report2.removed
    assert not (mroot / "skills" / "doomed").exists()
    # and a follow-up sync is stable
    report3 = cached.sync("skills/")
    assert report3.pulled == [] and report3.removed == []


def test_reads_served_from_mirror_after_sync(setup, monkeypatch):
    authority, cached, aroot, mroot = setup
    authority.put("skills/a/SKILL.md", b"v1")
    cached.sync("skills/")
    # authority unreachable from here on: reads must still work (offline mode)
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(authority, "get", boom)
    monkeypatch.setattr(authority, "list_prefix", boom)
    assert cached.get_text("skills/a/SKILL.md") == "v1"


def test_get_pulls_missing_key(setup):
    authority, cached, aroot, mroot = setup
    authority.put("skills/a/SKILL.md", b"lazy")
    assert cached.get_text("skills/a/SKILL.md") == "lazy"
    assert (mroot / "skills" / "a" / "SKILL.md").is_file()


def test_write_through_updates_mirror_and_index(setup):
    authority, cached, aroot, mroot = setup
    cached.put("skills/new/SKILL.md", b"fresh", create=True)
    assert authority.get("skills/new/SKILL.md") == b"fresh"
    assert (mroot / "skills" / "new" / "SKILL.md").read_bytes() == b"fresh"
    # and a subsequent sync does not re-pull it
    report = cached.sync("skills/")
    assert "skills/new/SKILL.md" not in report.pulled


def test_delete_through(setup):
    authority, cached, aroot, mroot = setup
    cached.put("skills/x/SKILL.md", b"v")
    cached.delete("skills/x/SKILL.md")
    assert not authority.exists("skills/x/SKILL.md")
    assert not (mroot / "skills" / "x" / "SKILL.md").exists()


def test_cas_passthrough(setup):
    authority, cached, aroot, mroot = setup
    meta = cached.put("k.txt", b"v1")
    with pytest.raises(Exception):
        cached.put("k.txt", b"v2", if_match="bogus")
    cached.put("k.txt", b"v2", if_match=meta.etag)
    assert cached.get("k.txt") == b"v2"


def test_cas_recovers_from_external_write(setup):
    """Regression: another process writing the key between our sync and the
    CAS write must not poison get_with_etag with a stale index ETag (the
    browser-toggle 500 of 2026-10-04)."""
    authority, cached, aroot, mroot = setup
    cached.put("state/x.json", b'{"v":1}')
    # external writer bypasses the cache's mirror + index entirely
    authority.put("state/x.json", b'{"v":2}')
    data, etag = cached.get_with_etag("state/x.json")
    assert data == b'{"v":2}'  # fresh content, not the stale mirror
    cached.put("state/x.json", b'{"v":3}', if_match=etag)  # must not raise
    assert authority.get("state/x.json") == b'{"v":3}'


def test_drift_reports_out_of_band_edits(setup):
    authority, cached, aroot, mroot = setup
    cached.put("skills/a/SKILL.md", b"v1")
    cached.sync("skills/")
    (mroot / "skills" / "a" / "SKILL.md").write_bytes(b"tampered")
    assert cached.drift() == ["skills/a/SKILL.md"]


def test_local_path_matches_mirror(setup):
    authority, cached, aroot, mroot = setup
    assert cached.local_path("skills/a/SKILL.md") == str(mroot / "skills" / "a" / "SKILL.md")
