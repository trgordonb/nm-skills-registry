"""SkillState: per-user enable/disable map (Hermes-style toggles).

One small JSON object per user under ``state/users/<user_id>/skills.json``,
holding the *disabled* list only — enabled-by-default, so new skills are
never silently swallowed by an enablement flag. Written with ETag CAS.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..storage.base import ObjectStore, ObjectStoreError, PreconditionFailedError


class SkillState:
    def __init__(self, store: ObjectStore, user_id: str = "default"):
        self.store = store
        self.user_id = user_id
        self.key = f"state/users/{user_id}/skills.json"

    # -- read ----------------------------------------------------------------

    def _load(self) -> tuple[dict, str | None]:
        try:
            raw, etag = self.store.get_with_etag(self.key)
        except KeyError:
            return {}, None
        except ObjectStoreError:
            # unreachable authority: fail open (all enabled) — the mirror keeps
            # the agent working and toggles simply can't be read offline
            return {}, None
        try:
            doc = json.loads(raw.decode("utf-8"))
        except ValueError:
            doc = {}
        return (doc if isinstance(doc, dict) else {}), etag

    def disabled(self) -> list[str]:
        doc, _ = self._load()
        v = doc.get("disabled", [])
        return [str(x) for x in v] if isinstance(v, list) else []

    def is_disabled(self, rel_dir: str) -> bool:
        return rel_dir in set(self.disabled())

    # -- write ---------------------------------------------------------------

    def _save(self, doc: dict, etag: str | None) -> None:
        doc["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        data = json.dumps(doc, indent=1, sort_keys=True).encode("utf-8")
        if etag is None:
            self.store.put(self.key, data, create=True)
        else:
            self.store.put(self.key, data, if_match=etag)

    def set_disabled(self, rel_dir: str, disabled: bool) -> None:
        """Toggle one skill; CAS with retry — a concurrent toggle loses at most
        one retry round, never a write."""
        for _ in range(3):
            doc, etag = self._load()
            current = set(doc.get("disabled", []))
            if disabled:
                current.add(rel_dir)
            else:
                current.discard(rel_dir)
            doc["disabled"] = sorted(current)
            try:
                self._save(doc, etag)
                return
            except PreconditionFailedError:
                continue
        raise PreconditionFailedError(
            f"could not update skill state {self.key!r} after 3 attempts"
        )
