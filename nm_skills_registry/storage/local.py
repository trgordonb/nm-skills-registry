"""LocalDirStore: real files under a root directory.

Serves three roles: the dev/test backend, the materialized mirror inside
CachedStore, and the "no registry" default so consumers behave identically
with SKILLS_REGISTRY unset. Directories are implicit in the key space, but
this backend materializes parent dirs on put (mirroring mkdir(parents=True)).
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from .base import (
    AlreadyExistsError,
    ObjectMeta,
    ObjectNotFoundError,
    PreconditionFailedError,
)


def validate_key(key: str) -> str:
    if not key or key.endswith("/"):
        raise ValueError(f"invalid object key: {key!r}")
    if key.startswith("/") or "\\" in key or ".." in Path(key).parts:
        raise ValueError(f"invalid object key: {key!r}")
    return key


def validate_prefix(prefix: str) -> str:
    """Prefixes may be empty or end with '/' (string-prefix semantics)."""
    if prefix.startswith("/") or "\\" in prefix or ".." in Path(prefix).parts:
        raise ValueError(f"invalid prefix: {prefix!r}")
    return prefix


def _content_etag(path: Path) -> str:
    """Content-addressed ETag (md5 hex) — same semantics as S3 simple PUTs, so
    CAS never passes for changed content regardless of mtime granularity."""
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class LocalDirStore:
    supports_cas = True

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)

    # -- keys ----------------------------------------------------------------

    def _path(self, key: str) -> Path:
        return self.root / validate_key(key)

    def local_path(self, key: str) -> str:
        return str(self._path(key))

    # -- listing -------------------------------------------------------------

    def list_prefix(self, prefix: str = "") -> list[ObjectMeta]:
        pfx = validate_prefix(prefix) if prefix else ""
        out: list[ObjectMeta] = []
        base = self.root / pfx if pfx else self.root
        if not base.is_dir():
            return out
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in filenames:
                full = Path(dirpath) / fn
                key = full.relative_to(self.root).as_posix()
                if pfx and not key.startswith(pfx):
                    continue
                st = full.stat()
                out.append(
                    ObjectMeta(
                        key=key,
                        size=st.st_size,
                        etag=_content_etag(full),
                        mtime=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                    )
                )
        out.sort(key=lambda m: m.key)
        return out

    # -- reads ---------------------------------------------------------------

    def _read_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise ObjectNotFoundError(key)
        return path.read_bytes()

    def get(self, key: str) -> bytes:
        return self._read_bytes(key)

    def get_text(self, key: str) -> str:
        return self._read_bytes(key).decode("utf-8")

    def get_with_etag(self, key: str) -> tuple[bytes, str | None]:
        path = self._path(key)
        if not path.is_file():
            raise ObjectNotFoundError(key)
        return path.read_bytes(), _content_etag(path)

    def head(self, key: str) -> ObjectMeta:
        path = self._path(key)
        if not path.is_file():
            raise ObjectNotFoundError(key)
        st = path.stat()
        return ObjectMeta(
            key=key,
            size=st.st_size,
            etag=_content_etag(path),
            mtime=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
        )

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    # -- writes --------------------------------------------------------------

    def put(
        self,
        key: str,
        data: bytes,
        *,
        if_match: str | None = None,
        create: bool = False,
    ) -> ObjectMeta:
        path = self._path(key)
        exists = path.is_file()
        if create and exists:
            raise AlreadyExistsError(key)
        if if_match is not None:
            if not exists:
                raise PreconditionFailedError(f"key does not exist: {key}")
            current = _content_etag(path)
            if current != if_match:
                raise PreconditionFailedError(
                    f"etag mismatch for {key}: expected {if_match}, got {current}"
                )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        return self.head(key)

    # -- deletes -------------------------------------------------------------

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()

    def delete_prefix(self, prefix: str) -> int:
        metas = self.list_prefix(prefix)
        for meta in metas:
            self._path(meta.key).unlink(missing_ok=True)
        # prune empty dirs bottom-up (a dir with an empty child is not empty),
        # then walk up ancestors — never removing the root itself
        if prefix:
            base = self.root / prefix
            if base.is_file():
                base = base.parent
            for dirpath, _dirnames, _filenames in os.walk(base, topdown=False):
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
            root = self.root.resolve()
            d = base.resolve().parent
            while d != root and root in d.parents:
                try:
                    d.rmdir()
                except OSError:
                    break
                d = d.parent
        return len(metas)
