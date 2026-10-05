"""CachedStore: S3 (or any ObjectStore) as authority + a local disk mirror.

Design (from the feasibility study):
- Reads are served from the mirror after ``sync()``; ``get`` pulls individual
  missing keys on demand. A steady-state session start costs one list request
  plus conditional GETs, not one GET per file.
- Writes are write-through: authority PUT first (with CAS when requested),
  mirror updated only on success.
- The mirror is durable across restarts → offline mode: if the authority is
  unreachable, callers can serve from the mirror (see ``drift``/``last_sync``).
- Single-writer assumption (matches the agent today); a lock guards
  cross-thread use from LangGraph tool executors.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .base import ObjectMeta, ObjectStore
from .local import LocalDirStore, validate_key

INDEX_FILE = ".nm-skills-registry-index.json"


@dataclass
class SyncReport:
    pulled: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0


class CachedStore:
    supports_cas = True

    def __init__(self, authority: ObjectStore, mirror: str | Path):
        self.authority = authority
        self.mirror = Path(mirror)
        self._mirror_store = LocalDirStore(self.mirror)
        self._lock = threading.RLock()
        self.last_sync: dict[str, int] = {}  # prefix -> object count

    # -- index ---------------------------------------------------------------

    def _index_path(self) -> Path:
        return self.mirror / INDEX_FILE

    def _load_index(self) -> dict[str, dict]:
        try:
            return json.loads(self._index_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_index(self, index: dict[str, dict]) -> None:
        self.mirror.mkdir(parents=True, exist_ok=True)
        self._index_path().write_text(
            json.dumps(index, indent=0, sort_keys=True), encoding="utf-8"
        )

    def _mirror_path(self, key: str) -> Path:
        rel = validate_key(key)
        target = self.mirror / rel
        # containment check on resolved paths (handles "." and ".." mirrors);
        # return the unresolved join so relative mirrors stay relative and
        # local_path() matches the original library's path strings
        root = self.mirror.resolve()
        resolved = target.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"key escapes mirror root: {key!r}")
        return target

    # -- sync ----------------------------------------------------------------

    def sync(self, prefix: str = "") -> SyncReport:
        """Pull the authority into the mirror; prune removed keys. One list
        request + parallel GETs for changed keys only. When the mirror already
        holds a file whose content MD5 matches the authority ETag (the
        import-then-pull pattern), it is adopted without a download."""
        from concurrent.futures import ThreadPoolExecutor

        from .local import content_md5

        with self._lock:
            report = SyncReport()
            index = self._load_index()
            prefix_base = prefix.rstrip("/")
            authority_metas = {
                m.key: m
                for m in self.authority.list_prefix(prefix)
                # skip directory markers: R2 returns 0-byte objects at the
                # prefix itself for console/manual uploads, and GETting them 404s
                if m.key != prefix_base and not m.key.endswith("/")
            }

            def fetch(item):
                key, _meta = item
                return key, self.authority.get(key)

            changed: list[tuple[str, ObjectMeta]] = []
            for key, meta in authority_metas.items():
                entry = index.get(key)
                if (
                    entry is not None
                    and entry.get("etag") == meta.etag
                    and self._mirror_path(key).is_file()
                ):
                    report.unchanged += 1
                    continue
                path = self._mirror_path(key)
                if path.is_file() and meta.etag and content_md5(path) == meta.etag.strip('"'):
                    # local content already matches the authority — adopt it
                    # (skip the download; import-then-pull pays zero GETs)
                    index[key] = {"etag": meta.etag, "size": meta.size}
                    report.unchanged += 1
                    continue
                changed.append((key, meta))

            if changed:
                # parallel GETs: obstore handles concurrent use; writes target
                # distinct files, and the index is updated in this thread only
                with ThreadPoolExecutor(max_workers=8) as pool:
                    for key, data in pool.map(fetch, changed):
                        meta = authority_metas[key]
                        self._write_mirror(key, data)
                        index[key] = {"etag": meta.etag, "size": len(data)}
                        report.pulled.append(key)

            for key in [k for k in index if k.startswith(prefix) and k not in authority_metas]:
                self._remove_key(key, index)
                report.removed.append(key)
            self._save_index(index)
            self.last_sync[prefix] = len(authority_metas)
            return report

    def _write_mirror(self, key: str, data: bytes) -> None:
        path = self._mirror_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _pull_key(self, key: str, meta: ObjectMeta, index: dict[str, dict]) -> None:
        data = self.authority.get(key)
        path = self._mirror_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        index[key] = {"etag": meta.etag, "size": len(data)}

    def _remove_key(self, key: str, index: dict[str, dict]) -> None:
        path = self._mirror_path(key)
        if path.is_file():
            path.unlink()
        index.pop(key, None)
        parent = path.parent
        root = self.mirror.resolve()
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    # -- reads ---------------------------------------------------------------

    def get(self, key: str) -> bytes:
        with self._lock:
            path = self._mirror_path(key)
            if not path.is_file():
                index = self._load_index()
                self._pull_key(key, self.authority.head(key), index)
                self._save_index(index)
            return path.read_bytes()

    def get_text(self, key: str) -> str:
        return self.get(key).decode("utf-8")

    def get_with_etag(self, key: str) -> tuple[bytes, str | None]:
        # Revalidate against a fresh HEAD: another process (CLI, second agent)
        # may have written this key since our last sync, and a stale index
        # ETag would make CAS retries loop on a precondition failure forever.
        # Only used on rare read-modify-write paths, so the HEAD is fine.
        with self._lock:
            meta = self.authority.head(key)
            path = self._mirror_path(key)
            index = self._load_index()
            if not path.is_file() or index.get(key, {}).get("etag") != meta.etag:
                self._pull_key(key, meta, index)
                self._save_index(index)
            return path.read_bytes(), meta.etag

    def head(self, key: str) -> ObjectMeta:
        return self.authority.head(key)

    def exists(self, key: str) -> bool:
        return self.authority.exists(key)

    def list_prefix(self, prefix: str = "") -> list[ObjectMeta]:
        return self.authority.list_prefix(prefix)

    def local_path(self, key: str) -> str:
        return str(self._mirror_path(key))

    # -- writes --------------------------------------------------------------

    def put(
        self,
        key: str,
        data: bytes,
        *,
        if_match: str | None = None,
        create: bool = False,
    ) -> ObjectMeta:
        with self._lock:
            meta = self.authority.put(key, data, if_match=if_match, create=create)
            path = self._mirror_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            index = self._load_index()
            index[key] = {"etag": meta.etag, "size": len(data)}
            self._save_index(index)
            return meta

    def delete(self, key: str) -> None:
        with self._lock:
            self.authority.delete(key)
            index = self._load_index()
            self._remove_key(key, index)
            self._save_index(index)

    def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            n = self.authority.delete_prefix(prefix)
            index = self._load_index()
            for key in [k for k in index if k.startswith(prefix)]:
                self._remove_key(key, index)
            self._save_index(index)
            return n

    # -- diagnostics ---------------------------------------------------------

    def drift(self, prefix: str = "") -> list[str]:
        """Mirror files under ``prefix`` that differ from the last synced index
        (edited outside the registry, or edited while offline). Defaults to
        every prefix this store has synced."""
        prefixes = [prefix] if prefix else (list(self.last_sync) or [""])
        index = self._load_index()
        out = []
        for pfx in prefixes:
            for meta in self._mirror_store.list_prefix(pfx):
                if meta.key == INDEX_FILE or "__pycache__" in meta.key or meta.key.endswith(".pyc"):
                    continue
                entry = index.get(meta.key)
                if entry is None or entry.get("size") != meta.size:
                    out.append(meta.key)
        return out
