"""Live R2 integration — runs only when R2 credentials are present.

Scratch keys live under test-cas-<pid>/ and are removed afterwards; the real
skills/ prefix is never touched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nm_skills_registry.env import load_env_file
from nm_skills_registry.storage.s3 import S3ObjectStore

_env = Path(__file__).resolve().parents[1] / ".env"
if _env.is_file():
    load_env_file(_env)

HAS_R2 = bool(os.environ.get("R2_BUCKET") and os.environ.get("R2_ACCESS_KEY_ID"))

pytestmark = pytest.mark.skipif(
    not HAS_R2, reason="R2 credentials not present (R2_BUCKET / R2_ACCESS_KEY_ID)"
)


@pytest.fixture()
def store():
    return S3ObjectStore.from_env()


def test_list_and_head(store):
    metas = store.list_prefix("")
    assert isinstance(metas, list)
    if metas:
        meta = store.head(metas[0].key)
        assert meta.key == metas[0].key
        assert meta.size == metas[0].size


def test_cas_roundtrip(store):
    scratch = f"test-cas-{os.getpid()}/probe.txt"
    try:
        meta = store.put(scratch, b"v1", create=True)
        store.put(scratch, b"v2", if_match=meta.etag)
        from nm_skills_registry.storage.base import PreconditionFailedError

        with pytest.raises(PreconditionFailedError):
            store.put(scratch, b"v3", if_match=meta.etag)
        assert store.get_text(scratch) == "v2"
        data, etag = store.get_with_etag(scratch)
        assert data == b"v2" and etag
    finally:
        store.delete_prefix(f"test-cas-{os.getpid()}/")


def test_r2_is_r2(store):
    # sanity: this really is the neuralmatrix bucket over the R2 endpoint
    assert "r2.cloudflarestorage" in (os.environ.get("R2_ENDPOINT_URL") or "")


def test_missing_key_maps_to_object_not_found(store):
    """Regression (v0.1.5): R2's S3 backend raises builtin FileNotFoundError
    on 404 — _map_error must translate it, not leak ObjectStoreError."""
    from nm_skills_registry.storage.base import ObjectNotFoundError
    from nm_skills_registry.storage.s3 import _map_error

    mapped = _map_error(FileNotFoundError("Object at location x not found"), "x")
    assert isinstance(mapped, ObjectNotFoundError)
    assert store.exists("definitely-missing-xyz-v015") is False
    with pytest.raises(ObjectNotFoundError):
        store.head("definitely-missing-xyz-v015")
