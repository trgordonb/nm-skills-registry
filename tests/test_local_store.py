"""ObjectStore protocol conformance for LocalDirStore."""

from __future__ import annotations

import pytest

from nm_skills_registry.storage.base import (
    AlreadyExistsError,
    ObjectNotFoundError,
    PreconditionFailedError,
)
from nm_skills_registry.storage.local import LocalDirStore


@pytest.fixture()
def store(tmp_path):
    return LocalDirStore(tmp_path / "root")


def test_put_get_roundtrip(store):
    store.put("a/b/c.txt", b"hello")
    assert store.get("a/b/c.txt") == b"hello"
    assert store.get_text("a/b/c.txt") == "hello"
    assert store.exists("a/b/c.txt")
    assert not store.exists("a/b/missing.txt")


def test_get_missing_raises(store):
    with pytest.raises(ObjectNotFoundError):
        store.get("nope.txt")


def test_list_prefix_sorted_and_filtered(store):
    store.put("b/2.txt", b"2")
    store.put("a/1.txt", b"1")
    store.put("a/sub/3.txt", b"3")
    keys = [m.key for m in store.list_prefix()]
    assert keys == ["a/1.txt", "a/sub/3.txt", "b/2.txt"]
    assert [m.key for m in store.list_prefix("a/")] == ["a/1.txt", "a/sub/3.txt"]


def test_head_meta(store):
    store.put("x.txt", b"12345")
    meta = store.head("x.txt")
    assert meta.key == "x.txt"
    assert meta.size == 5
    assert meta.etag  # pseudo-etag present for CAS


def test_put_create_mode(store):
    store.put("k.txt", b"v1", create=True)
    with pytest.raises(AlreadyExistsError):
        store.put("k.txt", b"v2", create=True)
    assert store.get("k.txt") == b"v1"


def test_put_cas(store):
    meta = store.put("k.txt", b"v1")
    store.put("k.txt", b"v2", if_match=meta.etag)
    with pytest.raises(PreconditionFailedError):
        store.put("k.txt", b"v3", if_match=meta.etag)
    assert store.get("k.txt") == b"v2"


def test_put_cas_missing_key(store):
    with pytest.raises(PreconditionFailedError):
        store.put("ghost.txt", b"v", if_match="whatever")


def test_get_with_etag(store):
    meta = store.put("k.txt", b"v1")
    data, etag = store.get_with_etag("k.txt")
    assert data == b"v1"
    assert etag == meta.etag
    store.put("k.txt", b"v2")
    _, etag2 = store.get_with_etag("k.txt")
    assert etag2 != etag


def test_delete_idempotent(store):
    store.put("k.txt", b"v")
    store.delete("k.txt")
    store.delete("k.txt")  # no-op
    assert not store.exists("k.txt")


def test_delete_prefix_removes_subtree_and_empty_dirs(store):
    store.put("cat/skill1/SKILL.md", b"a")
    store.put("cat/skill1/references/x.md", b"b")
    store.put("cat/skill2/SKILL.md", b"c")
    n = store.delete_prefix("cat/skill1/")
    assert n == 2
    assert not store.exists("cat/skill1/SKILL.md")
    assert store.exists("cat/skill2/SKILL.md")
    assert not (store.root / "cat" / "skill1").exists()  # pruned


def test_local_path(store, tmp_path):
    assert store.local_path("a/b.txt") == str(tmp_path / "root" / "a" / "b.txt")


@pytest.mark.parametrize(
    "bad", ["", "/", "x/", "/abs.txt", "../escape.txt", "a/../../b", "a\\b"]
)
def test_key_validation(store, bad):
    with pytest.raises(ValueError):
        store.put(bad, b"v")
    with pytest.raises(ValueError):
        store.list_prefix(bad) if bad == "/" else store.get(bad)
