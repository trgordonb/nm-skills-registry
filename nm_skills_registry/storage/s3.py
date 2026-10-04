"""S3ObjectStore: any S3-compatible backend (AWS S3, Cloudflare R2, MinIO,
Wasabi, B2-S3, ...) via obstore (Rust object_store bindings).

Conditional PUT maps to PutMode.Update (If-Match); create-mode maps to
PutMode.Create. R2, AWS S3, and MinIO support both; backends that reject
conditional writes surface as errors from the store and are surfaced to the
caller — the registry's CRUD falls back to last-writer-wins only when the
caller allows it.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import obstore
from obstore.exceptions import (
    AlreadyExistsError as _ObAlreadyExists,
)
from obstore.exceptions import (
    NotFoundError as _ObNotFound,
)
from obstore.exceptions import (
    PreconditionError as _ObPrecondition,
)
from obstore.store import S3Store

from .base import (
    AlreadyExistsError,
    ObjectMeta,
    ObjectNotFoundError,
    ObjectStoreError,
    PreconditionFailedError,
)

_DELETE_BATCH = 500


def _map_error(e: Exception, key: str) -> Exception:
    if isinstance(e, _ObNotFound):
        return ObjectNotFoundError(key)
    if isinstance(e, _ObPrecondition):
        return PreconditionFailedError(f"etag mismatch for {key}: {e}")
    if isinstance(e, _ObAlreadyExists):
        return AlreadyExistsError(key)
    return ObjectStoreError(f"{type(e).__name__}: {e}")


def _meta(path: str, size: int, etag: str | None, last_modified) -> ObjectMeta:
    mtime = last_modified if isinstance(last_modified, datetime) else None
    if mtime is not None and mtime.tzinfo is None:
        mtime = mtime.replace(tzinfo=timezone.utc)
    return ObjectMeta(key=path, size=size, etag=etag, mtime=mtime)


class S3ObjectStore:
    supports_cas = True

    def __init__(self, store: S3Store):
        self._store = store

    @classmethod
    def from_config(
        cls,
        bucket: str,
        *,
        endpoint: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        region: str = "auto",
    ) -> "S3ObjectStore":
        kwargs: dict = {"bucket": bucket, "region": region}
        if endpoint:
            kwargs["endpoint"] = endpoint
        if access_key_id:
            kwargs["access_key_id"] = access_key_id
        if secret_access_key:
            kwargs["secret_access_key"] = secret_access_key
        return cls(S3Store(**kwargs))

    @classmethod
    def from_env(cls, prefix: str = "R2_") -> "S3ObjectStore":
        """Build from env vars: ``{prefix}BUCKET``, ``{prefix}ENDPOINT_URL``,
        ``{prefix}ACCESS_KEY_ID``, ``{prefix}SECRET_ACCESS_KEY``, optional
        ``{prefix}REGION``. Falls back to the standard AWS_* names when the
        prefixed ones are absent."""

        def get(*names: str) -> str | None:
            for n in names:
                if os.environ.get(n):
                    return os.environ[n]
            return None

        bucket = get(f"{prefix}BUCKET", "AWS_BUCKET", "AWS_S3_BUCKET")
        if not bucket:
            raise ObjectStoreError(
                "missing bucket: set R2_BUCKET (or AWS_BUCKET) in the environment"
            )
        return cls.from_config(
            bucket,
            endpoint=get(f"{prefix}ENDPOINT_URL", "AWS_ENDPOINT_URL"),
            access_key_id=get(f"{prefix}ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"),
            secret_access_key=get(f"{prefix}SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"),
            region=get(f"{prefix}REGION", "AWS_REGION") or "auto",
        )

    # -- keys ----------------------------------------------------------------

    def local_path(self, key: str) -> str:
        return key

    # -- listing -------------------------------------------------------------

    def list_prefix(self, prefix: str = "") -> list[ObjectMeta]:
        out: list[ObjectMeta] = []
        try:
            for chunk in obstore.list(self._store, prefix or None):
                for m in chunk:
                    out.append(
                        _meta(m["path"], m["size"], m.get("e_tag"), m.get("last_modified"))
                    )
        except Exception as e:  # list errors carry no single key
            raise ObjectStoreError(f"list failed: {e}") from e
        out.sort(key=lambda m: m.key)
        return out

    # -- reads ---------------------------------------------------------------

    def get(self, key: str) -> bytes:
        try:
            return bytes(obstore.get(self._store, key).bytes())
        except Exception as e:
            raise _map_error(e, key) from e

    def get_text(self, key: str) -> str:
        return self.get(key).decode("utf-8")

    def get_with_etag(self, key: str) -> tuple[bytes, str | None]:
        try:
            result = obstore.get(self._store, key)
            meta = result.meta
            data = bytes(result.bytes())
            return data, meta.get("e_tag")
        except Exception as e:
            raise _map_error(e, key) from e

    def head(self, key: str) -> ObjectMeta:
        try:
            m = obstore.head(self._store, key)
        except Exception as e:
            raise _map_error(e, key) from e
        return _meta(m["path"], m["size"], m.get("e_tag"), m.get("last_modified"))

    def exists(self, key: str) -> bool:
        try:
            self.head(key)
            return True
        except ObjectNotFoundError:
            return False

    # -- writes --------------------------------------------------------------

    def put(
        self,
        key: str,
        data: bytes,
        *,
        if_match: str | None = None,
        create: bool = False,
    ) -> ObjectMeta:
        if if_match is not None:
            mode: object = {"e_tag": if_match}
        elif create:
            mode = "create"
        else:
            mode = "overwrite"
        try:
            res = obstore.put(self._store, key, data, mode=mode)
        except Exception as e:
            raise _map_error(e, key) from e
        etag = res.get("e_tag") if isinstance(res, dict) else None
        return ObjectMeta(key=key, size=len(data), etag=etag, mtime=None)

    # -- deletes -------------------------------------------------------------

    def delete(self, key: str) -> None:
        try:
            obstore.delete(self._store, key)
        except _ObNotFound:
            return
        except Exception as e:
            raise _map_error(e, key) from e

    def delete_prefix(self, prefix: str) -> int:
        keys = [m.key for m in self.list_prefix(prefix)]
        for i in range(0, len(keys), _DELETE_BATCH):
            batch = keys[i : i + _DELETE_BATCH]
            try:
                obstore.delete(self._store, batch)
            except Exception as e:
                raise ObjectStoreError(f"batch delete failed: {e}") from e
        return len(keys)
