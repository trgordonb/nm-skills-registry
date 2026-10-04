"""ObjectStore protocol: the whole filesystem surface the registry needs.

Derived from the feasibility inventory (2026-10-03): the vault-facing code in
nm-memory-layer and wiki_engine needs exactly these primitives — recursive
list, whole-object get/put, delete (object + prefix), head/exists — and
nothing else. No append, no locking, no fsync, no rename on content.

Conditional PUT (``if_match``) is optional per backend: backends that support
ETag CAS (S3-compatible stores via PutMode.Update) make the registry's
read-modify-write paths safe; backends without it degrade to last-writer-wins
and say so via ``supports_cas``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


class ObjectStoreError(Exception):
    """Base class for object-store errors."""


class ObjectNotFoundError(ObjectStoreError, KeyError):
    """The requested key does not exist."""


class PreconditionFailedError(ObjectStoreError):
    """A conditional PUT failed: the stored ETag does not match ``if_match``."""


class AlreadyExistsError(ObjectStoreError):
    """A create-mode PUT failed: the key already exists."""


@dataclass(frozen=True)
class ObjectMeta:
    key: str
    size: int
    etag: str | None = None
    mtime: datetime | None = None


@runtime_checkable
class ObjectStore(Protocol):
    """Minimal object-storage surface. Keys are POSIX-style relative paths."""

    supports_cas: bool

    def list_prefix(self, prefix: str = "") -> list[ObjectMeta]:
        """All objects under ``prefix``, sorted by key."""
        ...

    def get(self, key: str) -> bytes:
        """Whole-object read. Raises ObjectNotFoundError when missing."""
        ...

    def get_text(self, key: str) -> str:
        """Whole-object read decoded as UTF-8."""
        ...

    def get_with_etag(self, key: str) -> tuple[bytes, str | None]:
        """Read plus the current ETag, for CAS read-modify-write cycles."""
        ...

    def put(
        self,
        key: str,
        data: bytes,
        *,
        if_match: str | None = None,
        create: bool = False,
    ) -> ObjectMeta:
        """Write a whole object.

        ``if_match``: CAS — fail with PreconditionFailedError unless the stored
        ETag matches. ``create``: fail with AlreadyExistsError if the key
        exists. Both raise when the backend lacks CAS support and a condition
        was requested.
        """
        ...

    def delete(self, key: str) -> None:
        """Delete one object. Idempotent: deleting a missing key is a no-op."""
        ...

    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under ``prefix``. Returns the count deleted."""
        ...

    def head(self, key: str) -> ObjectMeta:
        """Metadata for one object. Raises ObjectNotFoundError when missing."""
        ...

    def exists(self, key: str) -> bool: ...

    def local_path(self, key: str) -> str:
        """Best-effort local filesystem path for a key (mirror path, or the
        key itself for backends with no local presence)."""
        ...
